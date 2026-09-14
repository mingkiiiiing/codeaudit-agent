"""W12-A4 服务端层内存差分探针：真服务子进程 + 40 个完整 HTTP 任务 + 进程级 RSS 锚点拟合。

背景（F9，docs/17 §1.2）：HTTP soak 实测每任务 ~0.15-0.27 MB RSS 缓爬（与任务数 r=0.80），
W11 memdiag 已证明不在 audit/* 库层（稳态斜率 ≈ 0）。本探针在**服务端层**做差分归因：
起真服务子进程（uvicorn 单 worker，完整 HTTP 栈含 multipart 上传/任务线程/SSE 可达），
顺序跑 N=40 个 mini_app 任务（多于 W11 库层的 30 轮），任务数 0/10/20/30/40 五个锚点
记录服务进程 RSS，用锚点段增量 + 逐任务序列拟合每任务增量。

方法要点：
  - 跨进程 tracemalloc/pympler 不可行 → 按约定退化为「逐嫌疑代码路径静态排除 + HTTP 层差分」；
  - 每任务协议与 soak 一致：POST /api/audits/upload → 轮询至终态 → 短保留 → DELETE；
  - 每个任务做离线断言（三重）：① 终态报告 stats tokens 恒 0（服务端 FakeLLM 无脚本
    回放、用量为空；真实 GLM 必产生非零用量）；② DELETE 前从临时 db 读 config_json，
    api_key ∈ {'', '<redacted>'}（W12-A1/F6 脱敏语义）；③ 服务 CWD 无 .env（F7 自动
    加载不触发）；
  - RSS 优先 psutil（Windows 口径 = 工作集），不可用时回退 psapi GetProcessMemoryInfo；
    同时记录线程数锚点（to_thread 默认线程池随负载惰性扩容，属一次性有界抬升候选）；
  - 结束后直连临时 db 盘点 audits/events 行数（DELETE 语义下应归零）；
  - 全程离线：父进程与服务子进程双重 pop GLM_*；服务工作目录/CODEAUDIT_DB_PATH 均在
    系统临时目录，finally 清理。

判读口径：锚点段增量随任务数递减/归零 → 预热集中（有界）；恒定 > 0.05 MB/任务 →
每任务常数滞留（真嫌疑）；序列出现台阶式平台 → 分配器高水位/线程池扩容（一次性）。

用法：
    python -m bench.memdiag.run_serverdiff            # 完整 40 任务
    python -m bench.memdiag.run_serverdiff --tasks 10 # 冒烟
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MINI_APP = PROJECT_ROOT / "demo" / "mini_app"
DEFAULT_OUT = PROJECT_ROOT / "bench" / "results" / "serverdiff_w12.md"
DEFAULT_TASKS = 40
ANCHOR_EVERY = 10  # 锚点间隔（0/10/20/30/40）
POLL_INTERVAL = 0.25
TERMINAL_TIMEOUT_SEC = 240.0
GRACE_SEC = 1.0  # 终态保留（差分探针无需 SSE 回放，较 soak 的 5s 缩短）
SETTLE_SEC = 2.0  # 锚点采样前的静置（让暂态请求收尾）
FINAL_SETTLE_SEC = 5.0  # 全部任务结束后静置（观察 RSS 松弛）
PER_TASK_SUSPECT_KB = 50.0  # 每任务增量疑点阈值（>50 KB/任务列为真嫌疑）


# ---------------------------------------------------------------- RSS / 线程探针
class _ProcProbe:
    """子进程指标探针：优先 psutil（RSS + 线程数），失败回退 psapi（仅 RSS，峰值可用）。"""

    def __init__(self, pid: int, popen: Any) -> None:
        self._psutil_proc = None
        self._popen = popen
        self.backend = "psapi"
        try:
            import psutil

            self._psutil_proc = psutil.Process(pid)
            self.backend = "psutil"
        except Exception:  # noqa: BLE001 —— psutil 缺失时回退 psapi（指标可选）
            self._psutil_proc = None

    def sample(self) -> dict[str, float] | None:
        """返回 {rss_mb, threads, peak_mb}；两个后端都取不到时 None。"""
        if self._psutil_proc is not None:
            try:
                mem = self._psutil_proc.memory_info()
                return {
                    "rss_mb": mem.rss / 1048576,
                    "threads": float(self._psutil_proc.num_threads()),
                    "peak_mb": -1.0,
                }
            except Exception:  # noqa: BLE001 —— 进程退出等瞬态，回退 psapi
                pass
        try:
            from bench.stress.run_soak import _win_rss_mb_local

            got = _win_rss_mb_local(self._popen)
            if got is not None:
                return {"rss_mb": got[0], "threads": -1.0, "peak_mb": got[1]}
        except Exception:  # noqa: BLE001 —— 指标可选
            return None
        return None


# ---------------------------------------------------------------- 数值工具
def _slope_per_task(ys: list[float]) -> float | None:
    """对等距 y 序列做最小二乘，返回每步增量；样本不足或零方差返回 None。"""
    n = len(ys)
    if n < 2:
        return None
    xs = [float(i) for i in range(n)]
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / denom


# ---------------------------------------------------------------- 单任务协议
def _run_one_task(
    client: httpx.AsyncClient, idx: int, zip_bytes: bytes, ro_conn: sqlite3.Connection | None, server_cwd: Path
) -> dict[str, Any]:
    """跑一个完整任务：上传 → 轮询终态 → 离线断言 → 保留 → DELETE。

    离线断言（三重）：① 终态报告 stats prompt/completion tokens 恒 0（服务端 FakeLLM
    无脚本回放、用量为空；真实 GLM 调用必产生非零用量——llm_calls 本身会计数，不能
    作为在线判据）；② db config_json api_key ∈ {'', '<redacted>'}（W12-A1/F6 脱敏语义，
    DELETE 前行必在；其他非空值=明文 Key 落库违例）；③ 服务 CWD 无 .env（F7 自动加载
    不触发，ro_conn 为 None 时第②项跳过）。
    """
    t0 = time.perf_counter()
    resp = client.post(
        "/api/audits/upload",
        files={"file": (f"serverdiff_{idx:03d}.zip", zip_bytes, "application/zip")},
        data={"do_fix": "false", "do_tests": "false"},
    )
    if resp.status_code != 200:
        return {"idx": idx, "status": f"HTTP {resp.status_code}", "duration": time.perf_counter() - t0, "offline": False}
    audit_id = str(resp.json().get("audit_id", ""))
    status = "timeout"
    sig_ok = False
    deadline = time.monotonic() + TERMINAL_TIMEOUT_SEC
    while time.monotonic() < deadline:
        got = client.get(f"/api/audits/{audit_id}")
        if got.status_code == 200:
            body = got.json()
            st = str(body.get("status", ""))
            if st in ("done", "failed"):
                status = st
                if st == "done":
                    stats_dict = body.get("report", {}).get("stats", {})
                    sig_ok = int(stats_dict.get("prompt_tokens", -1)) == 0 and int(
                        stats_dict.get("completion_tokens", -1)
                    ) == 0
                break
        time.sleep(POLL_INTERVAL)
    duration = time.perf_counter() - t0
    db_ok = True
    if ro_conn is not None:
        row = ro_conn.execute("SELECT config_json FROM audits WHERE audit_id = ?", (audit_id,)).fetchone()
        try:
            key_state = str(json.loads(str(row[0])).get("api_key", "")) if row else "?"
        except ValueError:
            key_state = "?"
        db_ok = key_state in ("", "<redacted>")
    env_ok = not (server_cwd / ".env").exists()  # 服务子进程 CWD（非探针 CWD）
    time.sleep(GRACE_SEC)
    client.delete(f"/api/audits/{audit_id}")
    return {
        "idx": idx,
        "audit_id": audit_id,
        "status": status,
        "duration": duration,
        "offline": sig_ok and db_ok and env_ok,
    }


# ---------------------------------------------------------------- 报告渲染
def _render(out_env: dict[str, str], rows: list[dict[str, Any]], anchors: list[dict[str, Any]], db_lines: list[str]) -> str:
    """渲染差分报告：环境、锚点表、段增量、逐任务序列、判读与诚实边界。"""
    tasks = out_env["tasks"]
    rss_seq = [r["rss_after"] for r in rows if r["rss_after"] > 0]
    anchor_rss = [a["rss"] for a in anchors if a["rss"] > 0]
    seg_lines: list[str] = []
    for i in range(1, len(anchors)):
        a0, a1 = anchors[i - 1], anchors[i]
        if a0["rss"] > 0 and a1["rss"] > 0:
            delta = a1["rss"] - a0["rss"]
            n_tasks = a1["n"] - a0["n"]
            label = f"{a0['n']}→{a1['n']}" if n_tasks > 0 else f"{a1['n']}（静置）"
            per_task_kb = (delta / n_tasks * 1024) if n_tasks > 0 else 0.0
            seg_lines.append(
                f"| {label} | {a0['rss']:.1f} | {a1['rss']:.1f} | {delta:+.1f} | {per_task_kb:+.1f} |"
            )
    per_task_all = _slope_per_task(rss_seq)
    per_task_late = _slope_per_task(rss_seq[4:])  # 跳过前 4 个（懒加载/缓存填充预热）
    warmup_share = "-"
    if anchor_rss and len(anchor_rss) >= 2:
        total_gain = anchor_rss[-1] - anchor_rss[0]
        first_seg = anchor_rss[1] - anchor_rss[0]
        warmup_share = f"{first_seg / total_gain * 100:.0f}%" if total_gain > 0 else "-"

    classify = "需人工判读"
    if per_task_late is not None:
        kb_late = per_task_late * 1024
        if kb_late > PER_TASK_SUSPECT_KB:
            classify = f"每任务常数滞留嫌疑（稳态 {kb_late:.1f} KB/任务 > {PER_TASK_SUSPECT_KB:.0f}）"
        elif abs(kb_late) <= 5:
            classify = "无每任务常数增长（稳态段 ≈ 0，差分法未复现缓爬）"
        else:
            classify = f"弱增长（稳态 {kb_late:.1f} KB/任务，低于疑点阈值）"
    slope_all_txt = f"{per_task_all * 1024:.1f}" if per_task_all is not None else "-"
    slope_late_txt = f"{per_task_late * 1024:.1f}" if per_task_late is not None else "-"

    lines = [
        "# W12-A4 服务端层内存差分探针报告（bench/memdiag/run_serverdiff.py）",
        "",
        "## 环境",
        "",
        f"- 生成时间：{out_env['now']}",
        f"- Python：{sys.version.split()[0]}；平台：{sys.platform}",
        f"- 被测服务：server/app.py 子进程（uvicorn 单 worker，完整 HTTP 栈）；RSS 后端：{out_env['backend']}",
        f"- 任务数：{tasks}（mini_app，顺序执行，协议=上传→终态→DELETE，与 soak 一致）",
        "- 离线：父/子进程均 pop GLM_*；每任务三重断言（FakeLLM 零用量 + db api_key 脱敏 + 服务 CWD 无 .env）",
        "- 服务工作目录与 CODEAUDIT_DB_PATH 均在系统临时目录（finally 已清理）",
        "",
        "## 锚点（任务数 0/10/20/30/40）",
        "",
        "| 锚点（累计任务） | RSS(MB) | 线程数 | 采样时刻(s) |",
        "|---|---|---|---|",
    ]
    for a in anchors:
        lines.append(f"| {a['n']} | {a['rss']:.1f} | {a['threads']:.0f} | {a['t']:.1f} |")
    lines += [
        "",
        "## 锚点段增量",
        "",
        "| 段 | 起始 RSS | 结束 RSS | 段增量(MB) | 折算 KB/任务 |",
        "|---|---|---|---|---|",
        *seg_lines,
        "",
        f"- 首段（0→{ANCHOR_EVERY}）占总增量比例：{warmup_share}"
        f"（占比高且后段趋零 → 预热集中/线程池扩容等一次性抬升）",
        f"- 逐任务序列最小二乘：全序列 {slope_all_txt} KB/任务；"
        f"稳态段（跳过前 4 任务）{slope_late_txt} KB/任务",
        f"- 判读：**{classify}**",
        "",
        "## 逐任务序列",
        "",
        "| # | 终态 | 耗时(s) | 离线断言 | RSS after(MB) |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['idx']} | {r['status']} | {r['duration']:.2f} | {'通过' if r['offline'] else '违例'} | {r['rss_after']:.1f} |"
        )
    lines += ["", "## 结束后 db 盘点（临时库直连）", ""]
    lines += [f"- {x}" for x in db_lines]
    lines += [
        "",
        "## 诚实边界",
        "",
        "- 跨进程无法用 tracemalloc/pympler 做对象级归因；本探针为进程级差分 + 静态排查组合证据"
        "（对象级结论见 probe_server_state_w12.md 的进程内探针）。",
        "- RSS 为 Windows 工作集口径，含解释器/依赖常驻与分配器高水位；差分增量为「滞留 + 未归还高水位」的合并上界。",
        f"- 稳态阈值 {PER_TASK_SUSPECT_KB:.0f} KB/任务为本探针约定（与 soak DEFER 观测 ~150-270 KB/任务相区分："
        "低于该值即判定 soak 缓爬主要来自一次性抬升 + 高水位，而非每任务常数滞留）。",
        "- 单 worker + 每任务独立 to_thread 线程执行；线程池扩容的一次性抬升在锚点「线程数」列可见。",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程
def main(argv: list[str] | None = None) -> int:
    """主流程：消毒 → 起服务 → 顺序 40 任务 + 锚点采样 → db 盘点 → 报告 → 清理。"""
    parser = argparse.ArgumentParser(description="W12-A4 服务端层内存差分探针（真服务 + HTTP 层差分）")
    parser.add_argument("--tasks", type=int, default=DEFAULT_TASKS, help="任务数（默认 40）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="报告输出路径（markdown）")
    parser.add_argument("--port", type=int, default=8948, help="自起服务端口（默认 8948）")
    args = parser.parse_args(argv)

    # 全程离线：父进程消毒（服务子进程由 handle 再消毒一次）
    for key in ("GLM_API_KEY", "GLM_BASE_URL", "GLM_MODEL"):
        os.environ.pop(key, None)

    from bench.stress.run_soak import _zip_dir_bytes
    from bench.stress.run_soak_long import _ServerHandleLong

    zip_bytes = _zip_dir_bytes(MINI_APP)
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = PROJECT_ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[serverdiff] 素材：{MINI_APP}（{len(zip_bytes) // 1024}KB）；任务数：{args.tasks}")
    handle = _ServerHandleLong(args.port)
    handle.start()
    print(f"[serverdiff] 服务就绪：{handle.base}")
    rows: list[dict[str, Any]] = []
    anchors: list[dict[str, Any]] = []
    db_lines: list[str] = []
    ro_conn: sqlite3.Connection | None = None
    t_start = time.monotonic()
    try:
        probe = _ProcProbe(handle.proc.pid, handle.proc)
        # 离线断言用只读连接（WAL 下并发读安全；服务端写入经其自身连接）
        ro_conn = sqlite3.connect(f"file:{handle.db_path}?mode=ro", uri=True)
        limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)
        with httpx.Client(base_url=handle.base, limits=limits, timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            def _anchor(n: int) -> None:
                time.sleep(SETTLE_SEC)
                s = probe.sample()
                anchors.append(
                    {
                        "n": n,
                        "rss": s["rss_mb"] if s else -1.0,
                        "threads": s["threads"] if s else -1.0,
                        "t": time.monotonic() - t_start,
                    }
                )
                print(f"[serverdiff] anchor n={n} rss={anchors[-1]['rss']:.1f}MB threads={anchors[-1]['threads']:.0f}")

            _anchor(0)
            for i in range(1, args.tasks + 1):
                r = _run_one_task(client, i, zip_bytes, ro_conn, handle.tmp_dir)
                s = probe.sample()
                r["rss_after"] = s["rss_mb"] if s else -1.0
                r["threads_after"] = s["threads"] if s else -1.0
                rows.append(r)
                print(
                    f"[serverdiff] task {i}/{args.tasks} {r['status']} dur={r['duration']:.2f}s "
                    f"offline={'ok' if r['offline'] else 'VIOLATION'} rss={r['rss_after']:.1f}MB"
                )
                if i % ANCHOR_EVERY == 0:
                    _anchor(i)

            # 全部结束后的松弛观察
            time.sleep(FINAL_SETTLE_SEC)
            s = probe.sample()
            anchors.append(
                {
                    "n": args.tasks,
                    "rss": s["rss_mb"] if s else -1.0,
                    "threads": s["threads"] if s else -1.0,
                    "t": time.monotonic() - t_start,
                }
            )
            print(f"[serverdiff] final(静置{FINAL_SETTLE_SEC:.0f}s后) rss={anchors[-1]['rss']:.1f}MB")

            # 服务端准入/表规模核对
            health = client.get("/api/health").json()
            db_lines.append(f"/api/health 结束快照：{json.dumps(health.get('audits', {}), ensure_ascii=False)}")
        ro_conn.close()

        # 临时 db 盘点（独立只读连接，连接即关，不干扰探针计数结论）
        db_path = handle.tmp_dir / "audits.db"
        if db_path.exists():
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                cur = conn.execute("SELECT COUNT(*) FROM audits")
                db_lines.append(f"audits 表行数（DELETE 后应为 0）：{cur.fetchone()[0]}")
                cur = conn.execute("SELECT COUNT(*) FROM events")
                db_lines.append(f"events 表行数（DELETE 后应为 0）：{cur.fetchone()[0]}")
            finally:
                conn.close()

        bad_offline = [r for r in rows if not r["offline"]]
        if bad_offline:
            db_lines.append(f"离线违例（三重断言任一不满足）：{len(bad_offline)} 个")
        backend = probe.backend
        out_env = {"now": datetime.now().isoformat(timespec="seconds"), "tasks": str(args.tasks), "backend": backend}
        out_path.write_text(_render(out_env, rows, anchors, db_lines), encoding="utf-8")
        print(f"[serverdiff] 报告已写入：{out_path}")
    finally:
        if ro_conn is not None:
            ro_conn.close()  # 先关探针侧连接再删临时目录（Windows 下打开句柄会阻止删除）
        handle.stop()
        print(f"[serverdiff] 临时目录已清理：{handle.tmp_dir}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
