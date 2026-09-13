"""Web 服务压测运行器（W8-A4）：真实 HTTP 口径，针对 server/app.py（FastAPI + uvicorn）。

用法::

    python -m bench.stress.run_stress_web [--port 8901] [--out md路径] [--quick]
                                          [--url http://127.0.0.1:8901] [--keep-server]

场景（全部离线，FakeLLM，未配置也不读取 GLM_API_KEY）：
  S1 read_burst   读端点突发：C 并发 × M 请求轮询 8 个只读端点 → p50/p95/p99、RPS、错误数；
  S2 concurrent   并发审计：C 个 POST /api/audits 同时提交（合成项目 files=80）轮询至终态
                  → 每任务端到端延迟、总吞吐、audit_id 互不串扰校验；
  S3 sse_streams  SSE 并发流：单个运行中任务的 /events 上开 S 个并发连接（依赖服务端
                  历史回放语义）→ 各连接收到事件与 done 终帧的比例、首事件/收流耗时；
  S4 upload       zip 上传：R 次 multipart（demo/mini_app 打包内存 zip）→ 延迟分位 +
                  非 zip 负样本必须 400。

口径说明：真实 HTTP（uvicorn 单进程单 worker；本脚本默认自起服务子进程，--url 可复用外部
服务）；任务表为内存态；读压测目标为 files=80 合成项目的完成报告（真实负载非空表）；
分位数 = 组内排序取分位，客户端口径（含 HTTP 栈往返，不含连接建立）。
对 server/audit 只读：不修改任何源文件；上传与并发任务产生的内存表项在场景后 DELETE 清理。
"""

from __future__ import annotations

import argparse
import io
import platform
import statistics
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESULTS_DIR = PROJECT_ROOT / "bench" / "results"
DATA_DIR = PROJECT_ROOT / "bench" / "stress" / "data"
MINI_APP = PROJECT_ROOT / "demo" / "mini_app"

# 端到端轮询上限（合成项目 files=80 离线审计通常 < 30s）
AUDIT_TIMEOUT_SEC = 240.0
POLL_INTERVAL = 0.25


# ---------------------------------------------------------------- 基础设施
def _pctl(values: list[float], p: float) -> float:
    """分位数（%）：排序线性取位，空列表返回 NaN 的替代值 -1.0。"""
    if not values:
        return -1.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))
    return s[idx]


def _fmt_pctl(values: list[float]) -> str:
    return f"p50={_pctl(values, 50):.1f} / p95={_pctl(values, 95):.1f} / p99={_pctl(values, 99):.1f}"


class ServerHandle:
    """自起/复用 uvicorn 服务；atexit 式清理。"""

    def __init__(self, port: int, external_url: str | None) -> None:
        self.port = port
        self.base = external_url or f"http://127.0.0.1:{port}"
        self.proc: subprocess.Popen[bytes] | None = None
        self.owned = external_url is None

    def start(self) -> None:
        if not self.owned:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        log_path = DATA_DIR / "web_server.log"
        self.log_file = log_path.open("wb")
        self.proc = subprocess.Popen(
            [sys.executable, "cli.py", "serve", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(PROJECT_ROOT),
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                r = httpx.get(f"{self.base}/api/health", timeout=2.0)
                if r.status_code == 200:
                    return
            except Exception:  # noqa: BLE001 —— 服务未就绪属预期，继续等待
                pass
            if self.proc.poll() is not None:
                raise RuntimeError(f"服务子进程提前退出（exit={self.proc.returncode}），日志：{log_path}")
            time.sleep(0.5)
        raise RuntimeError(f"服务 60s 未就绪，日志：{log_path}")

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        if hasattr(self, "log_file") and self.log_file and not self.log_file.closed:
            self.log_file.close()


# ---------------------------------------------------------------- 审计任务辅助
async def _create_audit(client: httpx.AsyncClient, source: Path) -> str:
    r = await client.post(
        "/api/audits",
        json={"source_path": str(source), "do_fix": False, "do_tests": False},
    )
    r.raise_for_status()
    return r.json()["audit_id"]


async def _wait_terminal(client: httpx.AsyncClient, audit_id: str, timeout: float = AUDIT_TIMEOUT_SEC) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = await client.get(f"/api/audits/{audit_id}")
        r.raise_for_status()
        status = r.json()["status"]
        if status in ("done", "failed"):
            return status
        await asyncio_sleep(POLL_INTERVAL)
    raise TimeoutError(f"任务 {audit_id} {timeout}s 未到终态")


async def asyncio_sleep(sec: float) -> None:
    import asyncio

    await asyncio.sleep(sec)


async def _prepare_done_report(client: httpx.AsyncClient, source: Path) -> str:
    """构造一份已完成报告作为读压测目标（幂等：每次运行新建任务，结束 DELETE）。"""
    audit_id = await _create_audit(client, source)
    status = await _wait_terminal(client, audit_id)
    if status != "done":
        raise RuntimeError(f"读压测目标任务 {audit_id} 终态为 {status}")
    return audit_id


# ---------------------------------------------------------------- 场景
async def s4_upload(client: httpx.AsyncClient, quick: bool) -> dict[str, Any]:
    """S4 zip 上传：demo/mini_app 内存打包 → R 次 multipart 上传 + 1 个负样本。"""
    rounds = 6 if quick else 12
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(MINI_APP.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                zf.write(p, p.relative_to(MINI_APP).as_posix())
    payload = buf.getvalue()

    latencies: list[float] = []
    created: list[str] = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        r = await client.post(
            "/api/audits/upload",
            files={"file": ("mini_app.zip", payload, "application/zip")},
            data={"do_fix": "false", "do_tests": "false"},
        )
        ms = (time.perf_counter() - t0) * 1000
        if r.status_code != 200:
            raise RuntimeError(f"upload 返回 {r.status_code}: {r.text[:200]}")
        latencies.append(ms)
        created.append(r.json()["audit_id"])

    # 负样本：内容非 zip 必须被 400 拒绝
    r = await client.post(
        "/api/audits/upload",
        files={"file": ("not_a_zip.zip", b"this is not a zip", "application/zip")},
        data={"do_fix": "false", "do_tests": "false"},
    )
    negative_status = r.status_code

    for audit_id in created:  # 内存表项清理（不等终态，跑完即删）
        await client.delete(f"/api/audits/{audit_id}")
    return {
        "rounds": rounds,
        "zip_kb": round(len(payload) / 1024, 1),
        "latencies_ms": latencies,
        "negative_status": negative_status,
    }


async def s1_read_burst(client: httpx.AsyncClient, target_id: str, quick: bool) -> dict[str, Any]:
    """S1 读端点突发：C 并发 × M 请求轮询 8 个只读端点。"""
    conc = 8 if quick else 32
    total = 80 if quick else 560
    endpoints = [
        "/api/health",
        "/api/audits?limit=20",
        f"/api/audits/{target_id}",
        f"/api/audits/{target_id}/summary",
        f"/api/audits/{target_id}/issues?limit=200",
        f"/api/audits/{target_id}/issues?severity=high&limit=100",
        f"/api/audits/{target_id}/patches",
        f"/api/audits/{target_id}/refactors",
    ]
    import asyncio

    latencies: list[float] = []
    errors: list[str] = []
    sem = asyncio.Semaphore(conc)
    counter = {"n": 0}

    async def worker() -> None:
        while True:
            if counter["n"] >= total:
                return
            counter["n"] += 1
            url = endpoints[counter["n"] % len(endpoints)]
            async with sem:
                t0 = time.perf_counter()
                try:
                    r = await client.get(url)
                    if r.status_code != 200:
                        errors.append(f"{url} → {r.status_code}")
                except Exception as exc:  # noqa: BLE001 —— 计入错误率
                    errors.append(f"{url} → {type(exc).__name__}: {exc}")
                latencies.append((time.perf_counter() - t0) * 1000)

    wall_t0 = time.perf_counter()
    await asyncio.gather(*[worker() for _ in range(conc)])
    wall = time.perf_counter() - wall_t0
    return {
        "concurrency": conc,
        "total": len(latencies),
        "rps": round(len(latencies) / wall, 1) if wall > 0 else 0.0,
        "wall": round(wall, 2),
        "p50": round(_pctl(latencies, 50), 1),
        "p95": round(_pctl(latencies, 95), 1),
        "p99": round(_pctl(latencies, 99), 1),
        "mean": round(statistics.fmean(latencies), 1) if latencies else -1.0,
        "errors": len(errors),
        "error_samples": errors[:5],
    }


async def s3_sse_streams(client: httpx.AsyncClient, source: Path, quick: bool) -> dict[str, Any]:
    """S3 SSE 并发流：单任务 /events 上开 S 个并发连接，靠服务端历史回放全部收齐。"""
    import asyncio

    streams = 8 if quick else 24
    audit_id = await _create_audit(client, source)
    # 等任务进入 running（保证有历史事件可回放）
    for _ in range(80):
        r = await client.get(f"/api/audits/{audit_id}")
        if r.json()["status"] in ("running", "done", "failed"):
            break
        await asyncio_sleep(0.1)

    saw_event = 0
    saw_done = 0
    first_event_ms: list[float] = []
    total_ms: list[float] = []

    async def one_stream() -> None:
        nonlocal saw_event, saw_done
        t0 = time.perf_counter()
        got_first = False
        try:
            async with client.stream("GET", f"/api/audits/{audit_id}/events") as resp:
                if resp.status_code != 200:
                    return
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    if not got_first:
                        first_event_ms.append((time.perf_counter() - t0) * 1000)
                        got_first = True
                    if '"type": "done"' in line or '"type":"done"' in line:
                        saw_done += 1
                        break
                    saw_event += 1
        except Exception:  # noqa: BLE001 —— 流中断按未收到 done 计
            pass
        finally:
            total_ms.append((time.perf_counter() - t0) * 1000)

    wall_t0 = time.perf_counter()
    await asyncio.gather(*[one_stream() for _ in range(streams)])
    wall = time.perf_counter() - wall_t0
    status = (await client.get(f"/api/audits/{audit_id}")).json()["status"]
    await client.delete(f"/api/audits/{audit_id}")
    return {
        "streams": streams,
        "saw_event": saw_event,
        "saw_done": saw_done,
        "task_status_at_end": status,
        "wall": round(wall, 2),
        "first_event_p50_ms": round(_pctl(first_event_ms, 50), 1),
        "first_event_p95_ms": round(_pctl(first_event_ms, 95), 1),
        "drain_p95_ms": round(_pctl(total_ms, 95), 1),
    }


async def s2_concurrent_audits(client: httpx.AsyncClient, source: Path, quick: bool) -> dict[str, Any]:
    """S2 并发审计：C 个任务同时提交、各自轮询至终态，校验互不串扰。"""
    import asyncio

    conc = 3 if quick else 6
    t0 = time.perf_counter()

    async def one() -> tuple[str, str, float]:
        s0 = time.perf_counter()
        audit_id = await _create_audit(client, source)
        status = await _wait_terminal(client, audit_id)
        return audit_id, status, (time.perf_counter() - s0)

    results = await asyncio.gather(*[one() for _ in range(conc)])
    wall = time.perf_counter() - t0
    ids = [r[0] for r in results]
    statuses = [r[1] for r in results]
    e2e = [r[2] for r in results]
    r = await client.get("/api/audits?limit=100")
    total_listed = r.json()["total"]
    for audit_id in ids:  # 清理内存表项
        await client.delete(f"/api/audits/{audit_id}")
    return {
        "concurrency": conc,
        "statuses": statuses,
        "distinct_ids": len(set(ids)) == len(ids),
        "e2e_sec": [round(x, 2) for x in e2e],
        "e2e_p50": round(_pctl(e2e, 50), 2),
        "e2e_max": round(max(e2e), 2),
        "wall": round(wall, 2),
        "throughput_per_min": round(conc / wall * 60, 1) if wall > 0 else 0.0,
        "list_total_observed": total_listed,
    }


# ---------------------------------------------------------------- 报告
def _render_md(env: dict[str, Any], results: dict[str, Any]) -> str:
    lines = [
        "# Web 服务压力测试报告（bench/stress/run_stress_web.py，W8-A4）",
        "",
        "## 环境",
        "",
        f"- **生成时间**：{env['now']}",
        f"- **主机 / 平台**：{env['host']} / {env['platform']}",
        f"- **CPU**：{env['cpu']}",
        f"- **Python**：{env['python']}",
        f"- **被测服务**：server/app.py（FastAPI + uvicorn，**单进程单 worker**），base={env['base']}",
        "- **网络**：全程离线（FakeLLM / 纯规则模式，未配置也不读取 GLM_API_KEY）",
        "- **口径**：真实 HTTP 客户端计时（httpx/asyncio）；任务表为内存态；分位数为客户端口径",
        "",
        "## 摘要",
        "",
        "| 场景 | 关键指标 | 状态 |",
        "|---|---|---|",
    ]
    s1, s2, s3, s4 = results["s1"], results["s2"], results["s3"], results["s4"]
    lines.append(
        f"| S1 读端点突发 | {s1['concurrency']} 并发 × {s1['total']} 请求，"
        f"RPS={s1['rps']}，p95={s1['p95']:.1f}ms，错误={s1['errors']} | {'OK' if s1['errors'] == 0 else 'WARN'} |"
    )
    lines.append(
        f"| S2 并发审计 | {s2['concurrency']} 任务，终态={','.join(s2['statuses'])}，"
        f"ID 互异={s2['distinct_ids']}，端到端 p50={s2['e2e_p50']}s / max={s2['e2e_max']}s，"
        f"吞吐={s2['throughput_per_min']} 任务/min | {'OK' if all(s == 'done' for s in s2['statuses']) and s2['distinct_ids'] else 'WARN'} |"
    )
    lines.append(
        f"| S3 SSE 并发流 | {s3['streams']} 连接，收到 done 终帧={s3['saw_done']}，"
        f"首事件 p50={s3['first_event_p50_ms']}ms，收流 p95={s3['drain_p95_ms']}ms | {'OK' if s3['saw_done'] == s3['streams'] else 'WARN'} |"
    )
    lines.append(
        f"| S4 zip 上传 | {s4['rounds']} 次（zip {s4['zip_kb']}KB），"
        f"p50={_pctl(s4['latencies_ms'], 50):.1f}ms / p95={_pctl(s4['latencies_ms'], 95):.1f}ms，"
        f"非 zip 负样本 HTTP {s4['negative_status']} | {'OK' if s4['negative_status'] == 400 else 'WARN'} |"
    )
    lines += ["", "## 场景明细", ""]
    for name, s in (("S1 读端点突发", s1), ("S2 并发审计", s2), ("S3 SSE 并发流", s3), ("S4 zip 上传", s4)):
        lines.append(f"### {name}")
        lines.append("")
        lines.append("| 指标 | 值 |")
        lines.append("|---|---|")
        for k, v in s.items():
            lines.append(f"| {k} | `{v}` |")
        lines.append("")
    lines += [
        "## 诚实边界",
        "",
        "- 单 worker 内存态任务表：本报告只对 0.5.0 的真实形态负责，多 worker/持久化属后续 Wave。",
        "- S2 的 C 个任务共享同一 uvicorn 事件循环，规则扫描为 CPU 密集，延迟随并发近线性增长属预期。",
        "- S3 依赖服务端 SSE 历史回放语义（事件列表重放后跟随新事件），连接慢于任务结束仍可收齐。",
        "- S1 的目标报告为 files=80 合成项目，payload 属轻中量；更大报告的分位数值会相应上移。",
        "",
    ]
    return "\n".join(lines)


def _env_info(base: str) -> dict[str, str]:
    return {
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "host": platform.node(),
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "cpu": platform.processor(),
        "python": platform.python_version(),
        "base": base,
    }


# ---------------------------------------------------------------- 主流程
async def run(port: int, out: Path, quick: bool, external_url: str | None) -> int:
    server = ServerHandle(port, external_url)
    server.start()
    print(f"[web-stress] 服务就绪：{server.base}")
    status_line = {k: "SKIP" for k in ("S1", "S2", "S3", "S4")}
    results: dict[str, Any] = {}
    exit_code = 0
    try:
        # 读压测目标：files=80 合成项目（生成幂等，落 bench/stress/data）
        from bench.stress.generator import generate_project

        source = generate_project(DATA_DIR / "web_read_target", files=80, defect_ratio=0.02, seed=42)
        limits = httpx.Limits(max_connections=64, max_keepalive_connections=32)
        timeout = httpx.Timeout(30.0, connect=5.0)
        async with httpx.AsyncClient(base_url=server.base, limits=limits, timeout=timeout) as client:
            print("[web-stress] 准备读压测目标（files=80 审计至 done）…")
            target_id = await _prepare_done_report(client, source)
            print(f"[web-stress] 目标就绪 audit_id={target_id}")

            for key, coro in (
                ("S4", s4_upload(client, quick)),
                ("S1", s1_read_burst(client, target_id, quick)),
                ("S3", s3_sse_streams(client, MINI_APP, quick)),
                ("S2", s2_concurrent_audits(client, source, quick)),
            ):
                t0 = time.perf_counter()
                try:
                    results[key.lower()] = await coro
                    status_line[key] = "OK"
                    print(f"[web-stress] {key} 完成（{time.perf_counter() - t0:.1f}s）")
                except Exception as exc:  # noqa: BLE001 —— 单场景失败不中断整份报告
                    results[key.lower()] = {"error": f"{type(exc).__name__}: {exc}"}
                    status_line[key] = "ERROR"
                    exit_code = 1
                    print(f"[web-stress] {key} 失败：{exc}")
            await client.delete(f"/api/audits/{target_id}")
    finally:
        server.stop()

    # 状态汇总；任一 ERROR / 关键校验不过 → 非零退出
    summary_checks = [
        len(results.get("s1", {}).get("error_samples", [])) == 0 if "s1" in results else False,
        all(s == "done" for s in results.get("s2", {}).get("statuses", [])) if "s2" in results else False,
        results.get("s2", {}).get("distinct_ids", False),
        results.get("s3", {}).get("saw_done", 0) == results.get("s3", {}).get("streams", -1),
        results.get("s4", {}).get("negative_status", -1) == 400,
    ]
    if not all(summary_checks):
        exit_code = 1

    md = _render_md(_env_info(server.base), results)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"[web-stress] 报告已写入：{out}")
    print("[web-stress] 场景状态：" + " ".join(f"{k}={v}" for k, v in status_line.items()))
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Web 服务压测（真实 HTTP，四场景，离线）")
    parser.add_argument("--port", type=int, default=8901, help="自起服务端口（默认 8901）")
    parser.add_argument("--url", default=None, help="复用外部已起服务（不再自起子进程）")
    parser.add_argument("--quick", action="store_true", help="冒烟口径（小并发小轮次）")
    parser.add_argument(
        "--out", default=str(DEFAULT_RESULTS_DIR / "stress_web_w8.md"), help="markdown 报告输出路径"
    )
    args = parser.parse_args(argv)
    return asyncio_run(args)


def asyncio_run(args: argparse.Namespace) -> int:
    import asyncio

    return asyncio.run(run(args.port, Path(args.out), args.quick, args.url))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
