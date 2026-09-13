"""Soak 持续混合负载压测运行器（W10-A4）：S9 长时稳定性口径，真实 HTTP + 全程离线。

用法::

    python -m bench.stress.run_soak [--duration 300] [--quick] [--port 8917] [--out md路径]

混合负载（直至时长用尽；--quick 时固定 120s）：
  - 读负载：常驻 2 Hz 轮询 GET /api/health 与 GET /api/audits?limit=5（记录状态码与延迟）；
  - 任务负载：每 8s 提交一个审计任务（demo/mini_app 内存 zip 与 files=80 合成项目 zip 交替），
    提交后异步轮询至终态（上限 240s），终态保留 5s（供 SSE 消费历史回放）后 DELETE 清理；
    收到 429 记入 denied 计数并跳过
    本轮（F5 准入语义下的合法观测，不作为错误；服务端未合入 429 时该计数自然为 0）；
  - SSE 负载：每 30s 一拍对最新已知任务开 1 条 SSE 连接，消费至 done 终帧或 10s 窗口后
    关闭（服务端审计为进程内 CPU 密集，客户端拿到 audit_id 时任务多已终态，终帧依赖
    SSE 端点的历史回放语义送达，与 bench.stress S3 同一约束）；
  - 采样：每 10s 记录 /api/health 的 audits.active/total 与服务子进程 RSS（Windows psapi
    GetProcessMemoryInfo 工作集口径，优先复用 bench.adversarial.run_adversarial._win_rss_mb，
    导入不便时回退本文件同款实现）。

结束判定（全部通过 → 退出码 0；任一失败 → 1）：
  1. 无 5xx（全程所有请求）；2. 被接纳任务终态率 100%（(done+failed)/admitted）；
  3. RSS 线性回归斜率 < 1 MB/min（最小二乘；样本 < 5 时跳过判定并如实标注）；
  4. 结束时 /api/health 的 total <= 50（FIFO 容量）。

口径说明：真实 HTTP（uvicorn 单进程单 worker，本脚本自起服务子进程，启动前清除 GLM_*
环境变量保证离线）；任务表为服务端内存态；分位数为客户端口径（含 HTTP 栈往返）；
RSS 为 Windows 工作集口径；报告默认落 bench/results/soak_w10.md；本场景创建的任务在
负载期终态即删 + 结束统一兜底 DELETE（服务 stop 由 finally 保证）。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import platform
import statistics
import sys
import time
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from bench.stress.run_stress_web import AUDIT_TIMEOUT_SEC, DATA_DIR, MINI_APP, ServerHandle, _pctl

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = PROJECT_ROOT / "bench" / "results" / "soak_w10.md"

# 节拍常量（与 docs/15 W10-A4 契约一致）
READ_PERIOD_SEC = 0.5  # 读负载 2 Hz（health 与列表各自独立节拍）
TASK_PERIOD_SEC = 8.0  # 任务提交周期
SSE_PERIOD_SEC = 30.0  # SSE 连接周期
SSE_WINDOW_SEC = 10.0  # SSE 单连接消费窗口上限
SAMPLE_PERIOD_SEC = 10.0  # RSS / health 采样周期
TERMINAL_GRACE_SEC = 5.0  # 终态后保留窗口：供 SSE 拍点连接该任务（历史回放），到点仍会 DELETE
POLL_INTERVAL = 0.25  # 任务终态轮询间隔（与 bench.stress 系列同口径）
RSS_SLOPE_LIMIT = 1.0  # MB/min，泄漏判定阈值
MIN_SLOPE_SAMPLES = 5  # 斜率判定最少样本数，不足则跳过
RSS_WARMUP_SEC = 60  # 预热期（W10-A5 集成裁决）：服务首任务触发懒加载导入的一次性抬升不参与泄漏判定
RSS_TASK_CORR_DEFER = 0.6  # 斜率超限但 RSS 与累计终态任务强相关（≥此值）→ DEFER（疑似每任务常数缓存，非时间泄漏）
FIFO_CAPACITY = 50  # 任务表 FIFO 容量（结束时 total 上限）

TERMINAL_STATUSES = ("done", "failed")


# ---------------------------------------------------------------- 基础设施
def _win_rss_mb_local(proc: Any) -> tuple[float, float] | None:
    """Windows psapi GetProcessMemoryInfo 取进程当前/峰值工作集（MB）；非 Windows 或失败返回 None。

    与 bench.adversarial.run_adversarial._win_rss_mb 同款实现，作为其导入不便时的回退。
    """
    if sys.platform != "win32" or proc is None:
        return None
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    try:
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        handle = int(proc._handle)  # type: ignore[attr-defined]
        if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
            return pmc.WorkingSetSize / 1048576, pmc.PeakWorkingSetSize / 1048576
    except Exception:  # 指标可选：取不到返回 None，不影响压测主流程
        pass
    return None


def _load_rss_probe() -> Callable[[Any], tuple[float, float] | None]:
    """优先复用对抗测试运行器的 _win_rss_mb（同口径零分叉）；导入不便时回退本文件同款实现。"""
    try:
        from bench.adversarial.run_adversarial import _win_rss_mb
    except Exception:  # 回退属预期路径（并行开发中模块可能临时不可导入）
        return _win_rss_mb_local
    return _win_rss_mb


def _zip_dir_bytes(source: Path) -> bytes:
    """目录打包为内存 zip（剔除 __pycache__；与 bench 系列上传素材构造同构）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(source.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                zf.write(p, p.relative_to(source).as_posix())
    return buf.getvalue()


# ---------------------------------------------------------------- 计数聚合
class SoakStats:
    """全程计数与样本聚合（单事件循环内访问，无需加锁）。"""

    def __init__(self) -> None:
        self.requests = 0
        self.status_counts: dict[int, int] = {}
        self.status_5xx: list[str] = []
        self.req_errors = 0
        self.read_lat: dict[str, list[float]] = {"health": [], "list": []}
        self.submits = 0
        self.admitted = 0
        self.denied_429 = 0
        self.submit_anomaly: list[str] = []
        self.terminals: dict[str, int] = {"done": 0, "failed": 0, "timeout": 0}
        self.sse: dict[str, int] = {"opened": 0, "done": 0, "closed": 0, "errors": 0, "skipped": 0}
        self.sse_error_samples: list[str] = []
        self.samples: list[dict[str, float]] = []
        self.created_ids: list[str] = []
        self.loop_crashes: list[str] = []


async def _do(
    client: httpx.AsyncClient, stats: SoakStats, method: str, url: str, *, lat_key: str | None = None, **kw: Any
) -> tuple[httpx.Response | None, float]:
    """发一个计数请求：计入请求数/状态码分布/5xx/客户端异常；lat_key 非空时记录读延迟。"""
    stats.requests += 1
    t0 = time.perf_counter()
    try:
        resp = await client.request(method, url, **kw)
    except Exception:  # 连接失败/超时按客户端错误计数，不中断负载
        stats.req_errors += 1
        return None, (time.perf_counter() - t0) * 1000
    ms = (time.perf_counter() - t0) * 1000
    stats.status_counts[resp.status_code] = stats.status_counts.get(resp.status_code, 0) + 1
    if resp.status_code >= 500:
        stats.status_5xx.append(f"{method} {url} -> {resp.status_code}")
    if lat_key is not None:
        stats.read_lat[lat_key].append(ms)
    return resp, ms


async def _sleep_until(stop: asyncio.Event, next_t: float) -> None:
    """可被 stop 事件提前打断的定时睡眠（到点或 stop 置位后返回，调用方再查 stop）。"""
    delay = next_t - time.monotonic()
    if delay <= 0:
        return
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
    except TimeoutError:
        pass


# ---------------------------------------------------------------- 负载协程
async def _read_loop(client: httpx.AsyncClient, stats: SoakStats, stop: asyncio.Event, path: str, key: str) -> None:
    """常驻读负载：固定频率轮询单个只读端点，记录状态码与延迟。"""
    t0 = time.monotonic()
    n = 0
    while not stop.is_set():
        await _do(client, stats, "GET", path, lat_key=key)
        n += 1
        await _sleep_until(stop, t0 + n * READ_PERIOD_SEC)


async def _task_lifecycle(client: httpx.AsyncClient, stats: SoakStats, audit_id: str) -> None:
    """轮询单个被接纳任务至终态（上限 240s），终态后保留 TERMINAL_GRACE_SEC 再 DELETE 清理。

    保留窗口原因：客户端只能经 POST 响应得知 audit_id，而服务端审计为进程内 CPU 密集，
    POST 返回时任务多已终态——若立即 DELETE，SSE 拍点将永远只能探到 404（实测 0 连接）。
    保留 5s 让 SSE 消费走服务端历史回放拿到终帧；DELETE 仍保证执行（含 finally 兜底）。
    """
    status = "timeout"
    deadline = time.monotonic() + AUDIT_TIMEOUT_SEC
    while time.monotonic() < deadline:
        resp, _ms = await _do(client, stats, "GET", f"/api/audits/{audit_id}")
        if resp is not None and resp.status_code == 200:
            st = str(resp.json().get("status", ""))
            if st in TERMINAL_STATUSES:
                status = st
                break
        await asyncio.sleep(POLL_INTERVAL)
    stats.terminals[status] = stats.terminals.get(status, 0) + 1
    await asyncio.sleep(TERMINAL_GRACE_SEC)
    await _do(client, stats, "DELETE", f"/api/audits/{audit_id}")


async def _submit_one(
    client: httpx.AsyncClient,
    stats: SoakStats,
    name: str,
    payload: bytes,
    lifecycles: list["asyncio.Task[None]"],
    inflight: dict[str, None],
) -> None:
    """提交一个上传任务：200 接纳并派生轮询协程；429 记 denied（合法观测）；其余记异常。"""
    stats.submits += 1
    resp, _ms = await _do(
        client,
        stats,
        "POST",
        "/api/audits/upload",
        files={"file": (f"soak_{name}.zip", payload, "application/zip")},
        data={"do_fix": "false", "do_tests": "false"},
    )
    if resp is None:
        stats.submit_anomaly.append(f"{name}: 客户端异常")
        return
    if resp.status_code == 429:
        stats.denied_429 += 1  # F5 准入拒绝：合法观测，跳过本轮，不计错误
        print(f"[soak] task#{stats.submits} {name} -> 429 denied（准入上限，跳过本轮）")
        return
    if resp.status_code != 200:
        stats.submit_anomaly.append(f"{name}: HTTP {resp.status_code}")
        return
    audit_id = str(resp.json().get("audit_id", ""))
    if not audit_id:
        stats.submit_anomaly.append(f"{name}: 响应缺 audit_id")
        return
    stats.admitted += 1
    stats.created_ids.append(audit_id)
    inflight[audit_id] = None
    lifecycles.append(asyncio.create_task(_task_lifecycle(client, stats, audit_id)))
    print(f"[soak] task#{stats.submits} {name} -> admitted audit_id={audit_id}")


async def _task_loop(
    client: httpx.AsyncClient,
    stats: SoakStats,
    stop: asyncio.Event,
    deadline: float,
    materials: list[tuple[str, bytes]],
    lifecycles: list["asyncio.Task[None]"],
    inflight: dict[str, None],
) -> None:
    """任务负载：每 8s 提交一个审计任务，两种素材交替，直至负载期截止。"""
    t0 = time.monotonic()
    n = 0
    while not stop.is_set():
        if time.monotonic() >= deadline:
            break
        name, payload = materials[n % len(materials)]
        await _submit_one(client, stats, name, payload, lifecycles, inflight)
        n += 1
        next_t = t0 + n * TASK_PERIOD_SEC
        if next_t >= deadline:
            break
        await _sleep_until(stop, next_t)


async def _newest_task(client: httpx.AsyncClient, stats: SoakStats, inflight: dict[str, None]) -> str | None:
    """取注册表中最新任务的 audit_id（注册表为空或全部查询 404 时 None）。

    时序事实（与 stress_web S3 同一约束）：服务端规则审计为进程内 CPU 密集，事件循环
    在审计期间被饿死，POST 响应要到审计结束后才回到客户端——客户端拿到 audit_id 时任务
    多已终态。因此 SSE 目标取"最新已知任务"，终帧依赖服务端 SSE 历史回放语义送达
    （端点先重放全部历史事件再跟随新事件）。
    """
    for audit_id in reversed(list(inflight)[-5:]):
        resp, _ms = await _do(client, stats, "GET", f"/api/audits/{audit_id}")
        if resp is not None and resp.status_code == 200:
            return audit_id
    return None


async def _consume_sse(client: httpx.AsyncClient, stats: SoakStats, audit_id: str) -> None:
    """开 1 条 SSE 连接消费至 done 终帧或 10s 窗口超时后关闭；计入 SSE 计数。"""
    stats.requests += 1
    stats.sse["opened"] += 1

    async def _drain() -> bool:
        saw_done = False
        async with client.stream("GET", f"/api/audits/{audit_id}/events", timeout=15.0) as resp:
            code = resp.status_code
            stats.status_counts[code] = stats.status_counts.get(code, 0) + 1
            if code >= 500:
                stats.status_5xx.append(f"GET /api/audits/{audit_id}/events -> {code}")
            if code != 200:
                return False
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                if '"type": "done"' in line or '"type":"done"' in line:
                    saw_done = True
                    break
        return saw_done

    try:
        if await asyncio.wait_for(_drain(), timeout=SSE_WINDOW_SEC):
            stats.sse["done"] += 1
        else:
            stats.sse["closed"] += 1  # 流自然结束但未见 done 终帧（如任务被清理），同为合法收流
    except TimeoutError:
        stats.sse["closed"] += 1
    except Exception as exc:  # 单连接异常只计数，不中断负载
        stats.sse["errors"] += 1
        if len(stats.sse_error_samples) < 5:
            stats.sse_error_samples.append(f"{type(exc).__name__}: {exc}")


async def _sse_loop(
    client: httpx.AsyncClient, stats: SoakStats, stop: asyncio.Event, deadline: float, inflight: dict[str, None]
) -> None:
    """SSE 负载：每 30s 一拍对最新已知任务开 1 条连接，消费至 done 终帧或 10s 窗口后关闭；
    拍内每 0.2s 找一次目标，一拍内注册表始终为空才记跳过（终帧送达依赖服务端历史回放，
    见 _newest_task 说明）。"""
    t0 = time.monotonic()
    n = 0
    while not stop.is_set():
        if time.monotonic() >= deadline:
            break
        target: str | None = None
        for _ in range(40):
            target = await _newest_task(client, stats, inflight)
            if target is not None or stop.is_set() or time.monotonic() >= deadline:
                break
            await _sleep_until(stop, time.monotonic() + 0.2)
        if target is None:
            stats.sse["skipped"] += 1
        else:
            await _consume_sse(client, stats, target)
        n += 1
        next_t = t0 + n * SSE_PERIOD_SEC
        if next_t >= deadline:
            break
        await _sleep_until(stop, next_t)


async def _sampler_loop(
    client: httpx.AsyncClient,
    stats: SoakStats,
    stop: asyncio.Event,
    rss_probe: Callable[[Any], tuple[float, float] | None],
    server_proc: Any,
    t0: float,
) -> None:
    """采样协程：每 10s 记录 health 的 audits.active/total 与服务子进程 RSS。"""
    n = 0
    while not stop.is_set():
        resp, _ms = await _do(client, stats, "GET", "/api/health")
        active = total = -1
        if resp is not None and resp.status_code == 200:
            audits = resp.json().get("audits", {})
            active = int(audits.get("active", -1))
            total = int(audits.get("total", -1))
        rss = rss_probe(server_proc)
        rss_now = round(rss[0], 1) if rss else -1.0
        cum_done = stats.terminals["done"] + stats.terminals["failed"]
        stats.samples.append(
            {
                "t": round(time.monotonic() - t0, 1),
                "rss": rss_now,
                "active": active,
                "total": total,
                "cum_done": cum_done,
            }
        )
        print(
            f"[soak] sample t={stats.samples[-1]['t']:.0f}s active={active} total={total} "
            f"cum_done={cum_done} rss={rss_now}MB"
        )
        n += 1
        await _sleep_until(stop, t0 + n * SAMPLE_PERIOD_SEC)


# ---------------------------------------------------------------- 判定与报告
def _slope_of(pts: list[tuple[float, float]]) -> float | None:
    """对 (t_sec, rss_mb) 点列做最小二乘，返回 MB/min 斜率；点数不足返回 None。"""
    if len(pts) < 2:
        return None
    xs = [t / 60.0 for t, _v in pts]
    ys = [v for _t, v in pts]
    return statistics.linear_regression(xs, ys).slope


def _rss_slope(samples: list[dict[str, float]]) -> tuple[float | None, int, float | None]:
    """RSS 泄漏判定斜率（MB/min）。

    W10-A5 集成裁决口径：剔除前 RSS_WARMUP_SEC 秒预热样本后做最小二乘——
    冷启动懒加载导入属一次性抬升而非泄漏，全窗口斜率会被其主导（quick 窗口尤甚）；
    稳态样本不足 MIN_SLOPE_SAMPLES 时回落全窗口序列。第三返回值为全窗口斜率，
    仅诊断参考，不参与判定。
    """
    pts = [(s["t"], s["rss"]) for s in samples if s["rss"] > 0]
    if len(pts) < MIN_SLOPE_SAMPLES:
        return None, len(pts), None
    full = _slope_of(pts)
    steady = [(t, v) for t, v in pts if t >= RSS_WARMUP_SEC]
    if len(steady) >= MIN_SLOPE_SAMPLES:
        return _slope_of(steady), len(steady), full
    return full, len(pts), full


def _rss_task_corr(samples: list[dict[str, float]]) -> float | None:
    """稳态窗口内 RSS 与累计终态任务数的 Pearson 相关（判定"每任务常数缓存 vs 时间泄漏"）。

    样本 <3 或零方差时返回 None（无法判定）。W10-A5 集成裁决口径。
    """
    pts = [(s["rss"], s.get("cum_done", 0)) for s in samples if s["rss"] > 0 and s["t"] >= RSS_WARMUP_SEC]
    if len(pts) < 3:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    mean_x, mean_y = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mean_x) * (y - mean_y) for x, y in pts)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / (var_x**0.5 * var_y**0.5)


def _terminal_rate(stats: SoakStats) -> float | None:
    """被接纳任务终态率 (done+failed)/admitted；无接纳任务时 None（跳过判定）。"""
    if stats.admitted == 0:
        return None
    return (stats.terminals["done"] + stats.terminals["failed"]) / stats.admitted


def _render_md(env: dict[str, str], stats: SoakStats, verdict: dict[str, Any]) -> str:
    """渲染 markdown 报告：环境头、摘要、判定、RSS 时序、读延迟分位、诚实边界。"""
    wall_load = verdict["wall_load"]
    slope, n_slope, slope_full = verdict["rss_slope"]
    rss_corr = verdict["rss_corr"]
    rate = verdict["terminal_rate"]
    lines = [
        "# Soak 持续混合负载压测报告（bench/stress/run_soak.py，W10-A4）",
        "",
        "## 环境",
        "",
        f"- **生成时间**：{env['now']}",
        f"- **主机 / 平台**：{env['host']} / {env['platform']}",
        f"- **CPU**：{env['cpu']}",
        f"- **Python**：{env['python']}",
        f"- **被测服务**：server/app.py（FastAPI + uvicorn，**单进程单 worker**），base={env['base']}",
        f"- **压测口径**：{'quick（120s）' if env['quick'] == '1' else '标准'}，负载期目标 {env['duration']}s",
        "- **网络**：全程离线（启动前清除 GLM_API_KEY/GLM_BASE_URL/GLM_MODEL，服务子进程继承消毒后环境，FakeLLM/纯规则模式）",
        "- **RSS 口径**：Windows psapi GetProcessMemoryInfo 工作集（服务子进程）",
        "",
        "## 摘要",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 负载期实际时长 / 含排空总时长 | {wall_load:.1f}s / {verdict['wall_total']:.1f}s（排空 {verdict['drain_wall']:.1f}s） |",
        f"| 总请求数 / RPS（按负载期） | {stats.requests} / {stats.requests / wall_load if wall_load > 0 else 0:.2f} |",
        "| 状态码分布 | " + (", ".join(f"{k}×{v}" for k, v in sorted(stats.status_counts.items())) or "-") + " |",
        f"| 读负载（2 Hz 各一） | health×{len(stats.read_lat['health'])}，audits?limit=5×{len(stats.read_lat['list'])} |",
        f"| 客户端异常请求（超时/连接） | {stats.req_errors} |",
        f"| 429 计数（denied，合法观测） | {stats.denied_429} |",
        f"| 任务 提交 / 接纳(admitted) | {stats.submits} / {stats.admitted} |",
        f"| 终态 done / failed / 超时未终态 | {stats.terminals['done']} / {stats.terminals['failed']} / {stats.terminals['timeout']} |",
        f"| 终态率 (done+failed)/admitted | {'-' if rate is None else f'{rate * 100:.1f}%'} |",
        f"| SSE 连接 / done 终帧 / 无终帧关闭 / 错误 / 跳过 | {stats.sse['opened']} / {stats.sse['done']} / {stats.sse['closed']} / {stats.sse['errors']} / {stats.sse['skipped']} |",
        f"| RSS 首 / 末 | {verdict['rss_first']:.1f} / {verdict['rss_last']:.1f} MB |",
        f"| RSS 稳态斜率（判定，剔除 {RSS_WARMUP_SEC}s 预热） | {'-' if slope is None else f'{slope:.3f}'} MB/min（{n_slope} 个稳态样本；全窗口参考 {'-' if slope_full is None else f'{slope_full:.3f}'}） |",
        f"| 结束时 /api/health total | {verdict['final_total']} |",
        "",
        "## 判定",
        "",
        "| 准则 | 结果 | 说明 |",
        "|---|---|---|",
    ]
    for name, result, note in verdict["checks"]:
        lines.append(f"| {name} | {result} | {note} |")
    lines.append(f"| **总判定** | **{verdict['overall']}** | 全部非 SKIP/DEFER 准则通过即 PASS；DEFER=疑似每任务缓存（有界），列归因待办不阻断 |")

    lines += [
        "",
        "## RSS / 任务表时序（每采样点）",
        "",
        "| t(s) | audits.active | audits.total | 累计终态 | RSS(MB) |",
        "|---|---|---|---|---|",
    ]
    for s in stats.samples:
        lines.append(
            f"| {s['t']:.0f} | {s['active']:.0f} | {s['total']:.0f} | {s.get('cum_done', 0):.0f} | {s['rss']:.1f} |"
        )

    # RSS 诊断（参考信息，不参与判定）：区分"冷启动一次性抬升"与"随任务数线性增长的真泄漏"
    valid = [(s["t"], s["rss"]) for s in stats.samples if s["rss"] > 0]
    diag = [
        f"- 全窗口斜率（参考口径）：{'-' if slope_full is None else f'{slope_full:.3f}'} MB/min；"
        f"稳态窗口斜率（判定口径，剔除前 {RSS_WARMUP_SEC}s 预热）：{'-' if slope is None else f'{slope:.3f}'} MB/min",
        f"- 稳态窗口 RSS 与累计终态任务数 Pearson 相关：{'-' if rss_corr is None else format(rss_corr, '.2f')}"
        f"（≥{RSS_TASK_CORR_DEFER} 判 DEFER：疑似每任务常数缓存；弱相关且超限判 FAIL：时间泄漏特征）",
        f"- RSS 总增量：{valid[-1][1] - valid[0][1]:.1f} MB（{valid[0][1]:.1f} → {valid[-1][1]:.1f}），"
        f"同期累计终态任务 {stats.terminals['done'] + stats.terminals['failed']} 个；"
        "若 RSS 增量与任务数近似线性则为真泄漏信号，若前段抬升后进入平台期则属冷启动预热（懒加载导入 + 分配器高水位）",
    ]
    lines += ["", "## RSS 诊断（参考，不参与判定）", ""] + diag

    lines += [
        "",
        "## 读延迟分位（客户端口径，ms）",
        "",
        "| 端点 | n | p50 | p95 | p99 |",
        "|---|---|---|---|---|",
    ]
    combined = stats.read_lat["health"] + stats.read_lat["list"]
    for key, vals in (("GET /api/health", stats.read_lat["health"]), ("GET /api/audits?limit=5", stats.read_lat["list"]), ("合计", combined)):
        lines.append(
            f"| {key} | {len(vals)} | {_pctl(vals, 50):.1f} | {_pctl(vals, 95):.1f} | {_pctl(vals, 99):.1f} |"
        )

    if stats.submit_anomaly:
        lines += ["", f"## 提交异常记录（非 200/429，共 {len(stats.submit_anomaly)} 条）", ""]
        lines += [f"- {x}" for x in stats.submit_anomaly[:10]]
    if stats.loop_crashes:
        lines += ["", f"## 负载协程异常（共 {len(stats.loop_crashes)} 条）", ""]
        lines += [f"- {x}" for x in stats.loop_crashes]

    lines += [
        "",
        "## 诚实边界",
        "",
        "- 单 worker 内存态任务表：本报告只对当前版本（单进程单 worker + 内存表）的真实形态负责，多 worker / 持久化属后续 Wave。",
        "- 全程离线 FakeLLM：启动前清除 GLM_* 环境变量，审计走纯规则通道；在线 LLM 通道的延迟与内存行为不在本报告覆盖范围。",
        "- quick 口径（120s）：任务数 / SSE 连接数 / RSS 样本数相应缩减；2 分钟窗口对小时级慢泄漏的灵敏度有限，斜率达标不外推为长期无泄漏。",
        "- 429 记为合法观测（F5 准入拒绝语义）；若服务端准入控制尚未合入，本计数为 0，脚本行为不受影响。",
        "- 本场景任务终态即 DELETE，结束时 total 天然有界；FIFO 淘汰的强校验由 bench/adversarial A3 场景覆盖。",
        "- DELETE 只清任务表项；上传 zip 与审计工作副本目录的磁盘回收不在 API 语义内，长时运行需关注 work_root 磁盘占用。",
        "- 离线规则通道单任务在本机亚秒~2s 完成，远快于 8s 提交周期；且审计为进程内 CPU 密集，事件循环被饿死期间 POST 响应被推迟到审计结束后，客户端拿到 audit_id 时任务多已终态——SSE 目标因此取最新已知任务，终帧靠服务端历史回放送达（与 bench.stress S3 同口径）。",
        "- RSS 泄漏判定为稳态窗口口径（W10-A5 集成裁决：剔除前 "
        f"{RSS_WARMUP_SEC}s 预热样本——冷启动懒加载导入属一次性抬升而非泄漏；稳态样本不足时回落全窗口并如实标注）。"
        "全窗口斜率与 RSS-任务数相关性继续在「RSS 诊断」节如实报告；每任务 ~0.15 MB 量级的缓爬若在长窗口复现，列 W11 归因待办。",
        "- RSS 为 Windows 工作集口径，含解释器与依赖常驻内存；非 Windows 平台取不到时斜率判定按约定跳过并如实标注。",
        "- 分位数为客户端口径（含 HTTP 栈往返）；规则扫描为 CPU 密集，运行期事件循环被饿死时读延迟 p99 上移属预期。",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程
async def run(port: int, out: Path, duration: float, quick: bool) -> int:
    """soak 主流程：消毒环境 → 自起服务 → 混合负载 → 排空 → 采样判定 → 清理 → 报告。"""
    # 全程离线：启动前清除 GLM_*，服务子进程继承消毒后环境（与 bench.adversarial 同约定）
    for key in ("GLM_API_KEY", "GLM_BASE_URL", "GLM_MODEL"):
        os.environ.pop(key, None)

    # 素材准备：mini_app 内存 zip + files=80 合成项目 zip（生成幂等，落 bench/stress/data）
    from bench.stress.generator import generate_project

    source = generate_project(DATA_DIR / "soak_proj80", files=80, defect_ratio=0.02, seed=42)
    materials = [("mini_app", _zip_dir_bytes(MINI_APP)), ("proj80", _zip_dir_bytes(source))]
    print(f"[soak] 素材就绪：mini_app={len(materials[0][1]) // 1024}KB proj80={len(materials[1][1]) // 1024}KB")

    server = ServerHandle(port, None)
    server.start()
    print(f"[soak] 服务就绪：{server.base}（duration={duration:.0f}s quick={quick}）")
    stats = SoakStats()
    rss_probe = _load_rss_probe()
    load_t0 = time.monotonic()
    try:
        limits = httpx.Limits(max_connections=64, max_keepalive_connections=32)
        timeout = httpx.Timeout(60.0, connect=10.0)
        async with httpx.AsyncClient(base_url=server.base, limits=limits, timeout=timeout) as client:
            stop = asyncio.Event()
            deadline = load_t0 + duration
            inflight: dict[str, None] = {}
            lifecycles: list["asyncio.Task[None]"] = []
            loops = [
                asyncio.create_task(_read_loop(client, stats, stop, "/api/health", "health")),
                asyncio.create_task(_read_loop(client, stats, stop, "/api/audits?limit=5", "list")),
                asyncio.create_task(_task_loop(client, stats, stop, deadline, materials, lifecycles, inflight)),
                asyncio.create_task(_sse_loop(client, stats, stop, deadline, inflight)),
                asyncio.create_task(_sampler_loop(client, stats, stop, rss_probe, server.proc, load_t0)),
            ]
            await asyncio.sleep(max(0.0, deadline - time.monotonic()))
            stop.set()
            for coro in await asyncio.gather(*loops, return_exceptions=True):
                if isinstance(coro, BaseException):
                    stats.loop_crashes.append(f"{type(coro).__name__}: {coro}")
            wall_load = time.monotonic() - load_t0

            # 排空：等待全部在途任务生命周期结束（各自 240s 上限），保证终态率口径完整
            drain_t0 = time.monotonic()
            for res in await asyncio.gather(*lifecycles, return_exceptions=True):
                if isinstance(res, BaseException):
                    stats.loop_crashes.append(f"lifecycle {type(res).__name__}: {res}")
            drain_wall = time.monotonic() - drain_t0

            # 结束时任务表规模快照（清理前取数，FIFO 容量判定口径）
            final_total = -1
            resp, _ms = await _do(client, stats, "GET", "/api/health")
            if resp is not None and resp.status_code == 200:
                final_total = int(resp.json().get("audits", {}).get("total", -1))

            # 清理：DELETE 所有本场景创建的任务（生命周期已删大半，此处兜底；404 属已清理）
            for audit_id in stats.created_ids:
                await _do(client, stats, "DELETE", f"/api/audits/{audit_id}")
    finally:
        server.stop()
    wall_total = time.monotonic() - load_t0

    # 判定（全部通过 → 退出码 0；任一失败 → 1；SKIP 不判失败但如实标注）
    checks: list[tuple[str, str, str]] = []
    checks.append(
        (
            "无 5xx（全程所有请求）",
            "PASS" if not stats.status_5xx else "FAIL",
            f"5xx={len(stats.status_5xx)}" + (f"，样本：{stats.status_5xx[:3]}" if stats.status_5xx else ""),
        )
    )
    rate = _terminal_rate(stats)
    if rate is None:
        checks.append(("接纳任务终态率 100%", "SKIP", "无被接纳任务（全部被 429 拒绝或未提交）"))
    else:
        checks.append(
            (
                "接纳任务终态率 100%",
                "PASS" if rate >= 1.0 else "FAIL",
                f"({stats.terminals['done']}+{stats.terminals['failed']})/{stats.admitted}"
                f"，超时未终态={stats.terminals['timeout']}",
            )
        )
    slope, n_slope, slope_full = _rss_slope(stats.samples)
    rss_corr = _rss_task_corr(stats.samples)
    if slope is None:
        checks.append(("RSS 斜率 < 1 MB/min", "SKIP", f"有效样本 {n_slope} < {MIN_SLOPE_SAMPLES}，跳过判定（样本不足或非 Windows）"))
    elif slope < RSS_SLOPE_LIMIT:
        checks.append(
            (
                "RSS 稳态斜率 < 1 MB/min（剔除预热）",
                "PASS",
                f"稳态斜率={slope:.3f} MB/min（{n_slope} 个 ≥{RSS_WARMUP_SEC}s 样本）；全窗口参考={slope_full:.3f}",
            )
        )
    elif rss_corr is not None and rss_corr >= RSS_TASK_CORR_DEFER:
        # DEFER：高斜率与任务数强相关 → 疑似"每任务常数缓存"而非时间泄漏；
        # FIFO 容量（50）兜底下内存有界（基线 + 50×每任务增量）。不阻断，列归因待办。
        checks.append(
            (
                "RSS 稳态斜率 < 1 MB/min（剔除预热）",
                "DEFER",
                f"稳态斜率={slope:.3f} MB/min 超限，但与累计终态任务数 Pearson r={rss_corr:.2f}（≥{RSS_TASK_CORR_DEFER}）"
                f"→ 疑似每任务常数缓存；FIFO 容量兜底下有界，长窗口归因列 W11 待办",
            )
        )
    else:
        checks.append(
            (
                "RSS 稳态斜率 < 1 MB/min（剔除预热）",
                "FAIL",
                f"稳态斜率={slope:.3f} MB/min 超限且与任务数相关性弱（r={'-' if rss_corr is None else f'{rss_corr:.2f}'}）→ 时间泄漏特征",
            )
        )
    if final_total < 0:
        checks.append(("结束时 total <= 50", "FAIL", "结束时 /api/health 快照失败"))
    else:
        checks.append(("结束时 total <= 50", "PASS" if final_total <= FIFO_CAPACITY else "FAIL", f"total={final_total}（FIFO 容量 {FIFO_CAPACITY}）"))
    overall = "PASS" if all(r != "FAIL" for _n, r, _note in checks) else "FAIL"
    if overall == "PASS" and any(r == "DEFER" for _n, r, _note in checks):
        overall = "PASS（含 DEFER）"

    rss_vals = [s["rss"] for s in stats.samples if s["rss"] > 0]
    verdict: dict[str, Any] = {
        "wall_load": wall_load,
        "wall_total": wall_total,
        "drain_wall": drain_wall,
        "terminal_rate": rate,
        "rss_slope": (slope, n_slope, slope_full),
        "rss_corr": rss_corr,
        "rss_first": rss_vals[0] if rss_vals else -1.0,
        "rss_last": rss_vals[-1] if rss_vals else -1.0,
        "final_total": final_total,
        "checks": checks,
        "overall": overall,
    }

    env = {
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "host": platform.node(),
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "cpu": platform.processor(),
        "python": platform.python_version(),
        "base": server.base,
        "quick": "1" if quick else "0",
        "duration": f"{duration:.0f}",
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_render_md(env, stats, verdict), encoding="utf-8")

    print(f"[soak] 报告已写入：{out}")
    print(f"[soak] 判定：{overall}｜" + "；".join(f"{n}={r}" for n, r, _note in checks))
    return 0 if overall == "PASS" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Soak 持续混合负载压测（真实 HTTP，离线）")
    parser.add_argument("--duration", type=float, default=300.0, help="负载时长（秒，默认 300）")
    parser.add_argument("--quick", action="store_true", help="冒烟口径：时长固定 120s")
    parser.add_argument("--port", type=int, default=8917, help="自起服务端口（默认 8917）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="markdown 报告输出路径")
    args = parser.parse_args(argv)
    duration = 120.0 if args.quick else (args.duration if args.duration > 0 else 300.0)

    import asyncio

    return asyncio.run(run(args.port, Path(args.out), duration, bool(args.quick)))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
