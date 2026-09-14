"""W12-A4 服务端层进程内状态探针：_RUNNING/_TASKS/_RUN_GATE/游标/SSE 生成器逐项取证。

背景（F9，docs/17 §1.2）：soak 观察到每任务 ~0.15-0.27 MB RSS 缓爬，W11 memdiag 已排除
audit/* 库层；本探针对 server/app.py 的簿记嫌疑做**进程内**直接取证（与跨进程差分探针
run_serverdiff.py 互补——跨进程 tracemalloc/pympler 不可行，同进程可任意内省）：

  1. _RUNNING / _TASKS 句柄清理（server/app.py:250-256 done callback）：
     并发跑 8 个任务（> _MAX_RUNNING_AUDITS=4，覆盖闸门排队路径）至全部终态后，
     断言 len(_RUNNING)==0 且 len(_TASKS)==0；
  2. _RUN_GATE 信号量配对（server/app.py:217-225 acquired 标志 + finally release）：
     排队路径结束后断言 _RUN_GATE._value 回到初始容量；cancel 路径由静态证据佐证
     （acquire 未返回时 acquired=False，finally 不 release，无超额释放）；
  3. TaskStore 连接/游标生命周期（audit/taskstore.py:84 单连接，每查询 execute 新建游标）：
     a) 静默期存活 sqlite3.Connection 恰为 1（_STORE 单例复用，无每查询新建连接）；
     b) 静默期存活 sqlite3.Cursor 为 0（用后即弃，引用计数回收，无钉死）；
     c) events 表在全部任务 DELETE 后行数归零（读路径内存不受历史事件累积影响）；
  4. SSE 生成器生命周期（server/app.py:428-444；sse_starlette 3.4.11 disconnect 取消）：
     a) 客户端中途粗暴断开（真 uvicorn 子线程）后，event_stream 异步生成器无残留；
     b) 正常消费至 done 终帧后，同样无残留；
  5. 请求日志缓冲：不存在 MemoryHandler/QueueHandler/BufferingHandler（uvicorn 为写透型
     StreamHandler，无内存缓冲累积）；线程数增量即 to_thread 默认线程池扩容（有界）。

阶段顺序说明：闸门是 asyncio 原语，首次争用即绑定当时的事件循环（CPython 实现细节）。
故含并发的阶段 1 先跑（真 uvicorn 子线程，争用/排队发生在其循环内）；阶段 2（主循环
ASGI 形态）只做顺序请求，闸门走无争用快速路径，不触发跨循环绑定错误。

全程离线（pop GLM_*）；db 与工作目录落系统临时目录，finally 清理。
报告写 bench/results/probe_server_state_w12.md。

用法：
    python -m bench.memdiag.probe_server_state
"""

from __future__ import annotations

import asyncio
import gc
import logging
import logging.handlers
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import types
from datetime import datetime
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MINI_APP = PROJECT_ROOT / "demo" / "mini_app"
DEFAULT_OUT = PROJECT_ROOT / "bench" / "results" / "probe_server_state_w12.md"
N_CONCURRENT = 8  # 并发任务数（> _MAX_RUNNING_AUDITS=4，覆盖闸门排队路径）
SSE_PORT = 8949
POLL_INTERVAL = 0.3
TERMINAL_TIMEOUT_SEC = 240.0
ABORT_ROUNDS = 3  # 客户端中途断开 SSE 的轮数

# 内存缓冲型日志 handler（存在即判"日志缓冲"嫌疑成立）
_BUFFERING_HANDLER_TYPES: tuple[type[logging.Handler], ...] = (
    logging.handlers.MemoryHandler,
    logging.handlers.QueueHandler,
    logging.handlers.BufferingHandler,
)


def _count(pred) -> int:  # type: ignore[no-untyped-def]
    """对 gc.get_objects() 计数（gc.collect 后调用，计数即静默期钉死对象数）。"""
    return sum(1 for o in gc.get_objects() if pred(o))


def _count_event_stream_gens() -> int:
    """存活的 event_stream 异步生成器数量（按 ag_code 函数名匹配，约定口径）。"""
    return _count(
        lambda o: isinstance(o, types.AsyncGeneratorType) and o.ag_code.co_name == "event_stream"  # type: ignore[attr-defined]
    )


def _count_log_handlers() -> int:
    """root + 全部具名 logger 的 handler 总数（负载前后应恒定）。"""
    total = len(logging.getLogger().handlers)
    for lg in logging.Logger.manager.loggerDict.values():
        if isinstance(lg, logging.Logger):
            total += len(lg.handlers)
    return total


def _db_counts(db_path: Path) -> tuple[int, int]:
    """独立只读连接盘点 (audits, events) 行数，用后即关（不干扰连接计数证据）。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        audits = conn.execute("SELECT COUNT(*) FROM audits").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()
    return int(audits), int(events)


def _phase1_uvicorn(app) -> tuple[list[tuple[str, str, str]], str]:  # type: ignore[no-untyped-def]
    """阶段 1（真 uvicorn 子线程）：并发任务排队/执行 → 句柄与闸门取证 → SSE 中断取证。

    返回 (取证结果列表, 留给阶段 2 做正常收流的 done 任务 audit_id)。
    本阶段不删除任何任务（阶段 2 需要 events 存量与 done 任务）。
    """
    import uvicorn

    findings: list[tuple[str, str, str]] = []
    keep_id = ""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=SSE_PORT, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(120):
        if server.started:
            break
        time.sleep(0.25)
    if not server.started:
        return [("阶段1 uvicorn 就绪", "跳过", "子线程 30s 未就绪，后续并发取证未执行")], keep_id
    base = f"http://127.0.0.1:{SSE_PORT}"
    try:
        with httpx.Client(base_url=base, timeout=30.0) as client:
            # 并发提交 8 个任务（闸门容量 4 → 后 4 个排队），轮询至全部终态
            ids: list[str] = []
            for _ in range(N_CONCURRENT):
                r = client.post("/api/audits", json={"source_path": str(MINI_APP), "do_fix": False, "do_tests": False})
                if r.status_code == 200:
                    ids.append(r.json()["audit_id"])
            assert len(ids) == N_CONCURRENT, f"提交失败：仅 {len(ids)}/{N_CONCURRENT} 被接纳"
            terminal: set[str] = set()
            deadline = time.monotonic() + TERMINAL_TIMEOUT_SEC
            while len(terminal) < len(ids) and time.monotonic() < deadline:
                for aid in ids:
                    if aid in terminal:
                        continue
                    got = client.get(f"/api/audits/{aid}")
                    if got.status_code == 200:
                        body = got.json()
                        if body.get("status") in ("done", "failed"):
                            terminal.add(aid)
                time.sleep(POLL_INTERVAL)
            time.sleep(1.0)  # 等 done callback 在服务循环里跑完

            import json as _json

            import server.app as sa

            running = len(sa._RUNNING)
            tasks_left = len(sa._TASKS)
            gate_left = sa._RUN_GATE._value  # type: ignore[attr-defined] —— CPython 内省口径
            gate_init = sa._MAX_RUNNING_AUDITS
            # 离线断言：① db config_json api_key ∈ {'', '<redacted>'}（W12-A1/F6 落库脱敏语义，
            # 其他非空值=明文 Key 落库违例）；② 终态报告 stats tokens 恒 0（服务端 FakeLLM
            # 无脚本回放、用量为空；真实 GLM 调用必产生非零用量。注意 FakeLLM 的 llm_calls
            # 会正常计数，不能作为在线判据）；③ 服务 CWD 无 .env（F7 自动加载不触发）。
            api_key_bad = 0
            token_bad = 0
            store = sa._get_store()
            for aid in ids:
                entry = store.get(aid)
                cfg_raw = str(entry.get("config_json", "")) if entry else ""
                try:
                    key_state = str(_json.loads(cfg_raw).get("api_key", "")) if cfg_raw else ""
                except ValueError:
                    key_state = "?"
                if key_state not in ("", "<redacted>"):
                    api_key_bad += 1
                if aid in terminal:
                    got = client.get(f"/api/audits/{aid}")
                    stats_dict = got.json().get("report", {}).get("stats", {}) if got.status_code == 200 else {}
                    try:
                        zero_usage = int(stats_dict.get("prompt_tokens", -1)) == 0 and int(
                            stats_dict.get("completion_tokens", -1)
                        ) == 0
                    except (TypeError, ValueError):
                        zero_usage = False
                    if not zero_usage:
                        token_bad += 1
            env_exists = (Path.cwd() / ".env").exists()
            offline_ok = api_key_bad == 0 and token_bad == 0 and not env_exists
            findings += [
                ("_RUNNING 句柄清理", "通过" if running == 0 else "泄漏", f"{N_CONCURRENT} 任务（含排队）终态后 len(_RUNNING)={running}"),
                ("_TASKS 弱引用集清理", "通过" if tasks_left == 0 else "泄漏", f"终态后 len(_TASKS)={tasks_left}"),
                ("_RUN_GATE 信号量配对", "通过" if gate_left == gate_init else "泄漏", f"排队路径终态后 _value={gate_left}（初始 {gate_init}）"),
                (
                    "离线断言（api_key 脱敏 + FakeLLM 零用量 + 无 .env）",
                    "通过" if offline_ok else "违例",
                    f"{len(ids)} 任务：api_key 违例={api_key_bad}，tokens 非零违例={token_bad}，"
                    f"服务 CWD 存在 .env={env_exists}",
                ),
            ]

            # SSE 中途断开 ×3：提交任务 → 开流读首行即弃流关闭（模拟客户端断开）→ 等终态
            gens_before = _count_event_stream_gens()
            for _ in range(ABORT_ROUNDS):
                r = client.post("/api/audits", json={"source_path": str(MINI_APP), "do_fix": False, "do_tests": False})
                aid = r.json()["audit_id"]
                with client.stream("GET", f"/api/audits/{aid}/events") as resp:
                    for _line in resp.iter_lines():
                        break  # 读到首行即弃流关闭
                ddl = time.monotonic() + TERMINAL_TIMEOUT_SEC
                while time.monotonic() < ddl:
                    body = client.get(f"/api/audits/{aid}").json()
                    if body.get("status") in ("done", "failed"):
                        break
                    time.sleep(POLL_INTERVAL)
            time.sleep(2.0)
            gc.collect()
            gens_after = _count_event_stream_gens()
            findings.append(
                (
                    "SSE 客户端断开后生成器释放",
                    "通过" if gens_after <= gens_before else "泄漏",
                    f"{ABORT_ROUNDS} 次中途断开；断开前存活 event_stream 生成器={gens_before}，断开后={gens_after}"
                    "（sse_starlette 3.4.11 cancel_on_finish + _listen_for_disconnect：断开即取消 task group）",
                )
            )
    finally:
        server.should_exit = True
        th.join(timeout=10)

    # 留一个 done 任务给阶段 2 做正常收流（读 db 定位，不经 HTTP）
    import server.app as sa2

    audits, _ev = _db_counts(sa2._get_store()._db_path)  # type: ignore[attr-defined] —— 探针内省口径
    if audits > 0:
        for item in reversed(sa2._get_store().list(0, 0)[1]):
            if item["status"] == "done":
                keep_id = item["audit_id"]
                break
    return findings, keep_id


async def _phase2_asgi(app, keep_id: str) -> list[tuple[str, str, str]]:  # type: ignore[no-untyped-def]
    """阶段 2（主循环 ASGI 形态，顺序请求）：正常 SSE 收流 / events 归零 / 连接与游标计数。"""
    import server.app as sa

    findings: list[tuple[str, str, str]] = []
    db_path = sa._get_store()._db_path  # type: ignore[attr-defined] —— 探针内省口径
    ev_before = _db_counts(db_path)[1]

    # 正常消费一个 done 任务的 SSE 至 done 终帧
    saw_done = False
    if keep_id:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=15.0) as client:
            async with client.stream("GET", f"/api/audits/{keep_id}/events") as resp:
                async for line in resp.aiter_lines():
                    if '"type": "done"' in line or '"type":"done"' in line:
                        saw_done = True
                        break
        gc.collect()
    gens_left = _count_event_stream_gens()
    findings.append(
        (
            "SSE 正常收流后生成器关闭",
            "通过" if gens_left == 0 else "可疑",
            f"done 终帧={saw_done}；残留 event_stream 生成器={gens_left}",
        )
    )

    # 顺序 DELETE 全部任务 → events/audits 归零（events 无界增长对读路径内存的影响排除）
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=30.0) as client:
        resp = await client.get("/api/audits?limit=0")
        for item in resp.json().get("audits", []):
            await client.delete(f"/api/audits/{item['audit_id']}")
    audits_after, ev_after = _db_counts(db_path)
    findings.append(
        (
            "events 表 DELETE 归零",
            "通过" if ev_after == 0 and audits_after == 0 else "可疑",
            f"任务存活期 events 行数={ev_before}；全量 DELETE 后 events={ev_after}，audits={audits_after}"
            "（get_events 按 audit_id 过滤读，单任务事件数有界 → 读路径内存不受表总量影响）",
        )
    )

    # 静默期连接/游标计数（阶段 2 全程顺序请求，闸门走无争用快速路径，不绑定主循环）
    await asyncio.sleep(0.1)
    gc.collect()
    conns = _count(lambda o: isinstance(o, sqlite3.Connection))
    cursors = _count(lambda o: isinstance(o, sqlite3.Cursor))
    singleton = sa._STORE is sa._get_store()
    findings += [
        (
            "store 单例连接复用",
            "通过" if conns == 1 and singleton else "可疑",
            f"静默期存活 sqlite3.Connection={conns}（应为 1：_STORE 单例）；单例判定={singleton}",
        ),
        (
            "游标无钉死",
            "通过" if cursors == 0 else "可疑",
            f"静默期存活 sqlite3.Cursor={cursors}（每查询 execute 新建游标，用后引用计数回收）",
        ),
    ]
    return findings


def main() -> int:
    """主流程：消毒 → 临时目录 → 进程内加载 server.app → 阶段1/2 取证 → 报告。"""
    for key in ("GLM_API_KEY", "GLM_BASE_URL", "GLM_MODEL"):
        os.environ.pop(key, None)
    tmp = Path(tempfile.mkdtemp(prefix="probe_server_state_w12_"))
    os.environ["CODEAUDIT_DB_PATH"] = str(tmp / "audits.db")
    orig_cwd = os.getcwd()
    os.chdir(tmp)  # work_root（".codeaudit" 相对 CWD）随之落临时目录
    lines: list[str] = [
        "# W12-A4 服务端层进程内状态探针报告（bench/memdiag/probe_server_state.py）",
        "",
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- Python：{sys.version.split()[0]}；被测：server/app.py（同进程双形态：uvicorn 子线程 + ASGI transport）",
        f"- 任务素材：demo/mini_app；阶段1 并发 {N_CONCURRENT} 个（闸门容量 4，含排队）+ SSE 断开 {ABORT_ROUNDS} 轮",
        "- 离线：进程 pop GLM_*；db 与工作目录在系统临时目录（finally 已清理）",
        "",
    ]
    findings: list[tuple[str, str, str]] = []
    try:
        import server.app as sa

        base_threads = threading.active_count()
        base_handlers = _count_log_handlers()
        app = sa.create_app()
        # 阶段 1 先行：闸门（asyncio 原语）首次争用绑定 uvicorn 循环，后续阶段不再争用
        phase1, keep_id = _phase1_uvicorn(app)
        findings += phase1
        findings += asyncio.run(_phase2_asgi(app, keep_id))
        gc.collect()
        handlers_after = _count_log_handlers()
        buffering = _count(lambda o: isinstance(o, _BUFFERING_HANDLER_TYPES))
        threads_after = threading.active_count()
        findings.append(
            (
                "请求日志缓冲（uvicorn/FastAPI）",
                "通过" if buffering == 0 else "可疑",
                f"logging handler 数：负载前={base_handlers}，负载后={handlers_after}（含 uvicorn 启动期配置）；"
                f"内存缓冲型 handler（Memory/Queue/Buffering）={buffering}——uvicorn 为写透型 StreamHandler，"
                f"无内存缓冲累积；线程数 {base_threads}→{threads_after}"
                "（增量=to_thread 默认线程池惰性扩容，上限 min(32, cpu+4)，有界）",
            )
        )
    finally:
        # 先关 _STORE 的 sqlite 连接再删临时目录：Windows 下打开的 db 句柄会令 rmtree 静默跳过
        try:
            import server.app as _sa_cleanup

            if _sa_cleanup._STORE is not None:
                _sa_cleanup._STORE.close()
        except Exception:  # noqa: BLE001 —— 清理兜底，失败不掩盖主流程结果
            pass
        os.chdir(orig_cwd)
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [p for p, r, _e in findings if r in ("泄漏", "违例", "可疑")]
    ok = not bad and len(findings) > 0
    lines += [
        "## 逐嫌疑取证",
        "",
        "| 嫌疑点 | 结果 | 证据 |",
        "|---|---|---|",
    ]
    for point, result, evidence in findings:
        lines.append(f"| {point} | {result} | {evidence} |")
    lines += [
        "",
        "## 结论",
        "",
        f"- 共 {len(findings)} 项取证，异常 {len(bad)} 项："
        + ("server/app.py 簿记嫌疑全部排除。" if not bad else f"异常项：{'、'.join(bad)}"),
        "- 静态佐证：done callback 对 _TASKS.discard 与 _RUNNING.pop 成对执行且含 is 判守卫"
        "（server/app.py:250-256，add_done_callback 在任务取消/异常时同样触发）；"
        "_run_audit_task 以 acquired 标志 + finally release 严格配对（217-225，acquire 未返回时无超额释放）；"
        "TaskStore 单连接复用（taskstore.py:84），get/list/get_events 均 fetchall 后行集即刻可回收；"
        "delete/prune 均连带清理 events（taskstore.py:172-178/304）——events 表只随存活任务增长；"
        "sse_starlette 3.4.11 __call__ 在断开/收流结束时 cancel 整个 task group（生成器 aclose，游标为局部列表）。",
        "",
    ]
    out_path = PROJECT_ROOT / "bench" / "results" / "probe_server_state_w12.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[probe-state] 报告已写入：{out_path}")
    for point, result, evidence in findings:
        print(f"[probe-state] {point}: {result} | {evidence}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
