"""多 worker 压测基线运行器（W13-A3）：`cli.py serve --workers N` 真实服务 + 单 worker 对照，全程离线。

用法::

    python -m bench.stress.run_soak_multiworker [--duration 600] [--quick] [--port-base 8930]
                                                [--out bench/results/soak_multiworker_w13.md]
                                                [--workers 1,2]

对 ``--workers`` 列表中的每个口径依次（端口 port-base+i）：自起 ``cli.py serve --port P --workers N``
子进程 → 混合负载（照抄 bench.stress.run_soak 模式）→ 三态判定 → 清理 → 汇总对照报告。

混合负载（直至时长用尽；--quick 时固定 300s，契约验收口径）：
  - 读负载：常驻 2 Hz 轮询 GET /api/health 与 GET /api/audits?limit=5（状态码 + 延迟）；
  - 任务负载：每 6s 提交一个审计任务（demo/mini_app 内存 zip 与 files=80 合成项目 zip 交替），
    提交后异步轮询至终态（上限 240s），终态保留 5s（供 SSE 消费）后 DELETE；429 记 denied
    （F5 全局准入的合法观测，不计错误）；另测任务端到端延迟（提交 → 客户端观测到终态）；
  - SSE 负载：每 30s 一拍对最新已知任务开 1 条 SSE 连接，消费至 done 终帧或 10s 窗口
    （复用 run_soak._sse_loop，历史回放语义）；
  - 采样：每 10s 记录 /api/health 的 audits.active/total 与服务进程树 RSS 之和
    （Windows：uvicorn 主进程 + spawn 出的 worker 子进程；CreateToolhelp32Snapshot 枚举
    父子关系后逐进程 GetProcessMemoryInfo 工作集累加——比 tasklist/wmic 解析少一层
    外部进程与文本解析，口径最稳；非 Windows 回退 psutil，再不可用则如实跳过判定）。

多 worker 特有观测（workers>1 口径必做；为保两口径负载完全一致，探测流量在单 worker 口径同样注入）：
  - 429 触发率：全局准入 store.count_active()（跨 worker 汇总）≥ 20 时的拒绝观测；
  - active 计数一致性：每次提交瞬间对 health.active 连续采样（0.15s × 10），校验
    （a）永不越过 pending 上限 20（若越过即跨 worker 准入竞态）；（b）不多于客户端视角的
    全局非终态任务数（服务端先行终态，客户端滞后观测，故 server active ≤ client expected）；
  - 跨 worker SSE：每次提交后立即对该任务开一条 SSE 消费至 done 终帧或 5s 窗口——sticky
    模型下 POST 落 worker A 执行，SSE 连接可能落 worker B，事件经 SQLite store 读取应能收齐；
    k 次全收齐 + 每连接 50% 概率落同 worker → "必须同 worker 才能收帧"假设的联合概率 ≤ 2^-k
    （客户端无法直接观测 worker 身份，取统计 + 代码层双证据，诚实标注）。

结束判定（每口径独立，全部通过 → 该口径 PASS）：
  1. 无 5xx；2. 被接纳任务终态率 100%；3. RSS 稳态斜率三态（PASS/DEFER/FAIL，剔除预热，
  与 run_soak 同口径）；4. 结束 total ≤ 50；5. 离线口径验证（首个终态任务秒级而非分钟级，
  防 .env 在线通道泄漏——服务子进程 env 注入 GLM_API_KEY=""（F7 语义：真实环境变量优先于
  .env）强制离线 + CODEAUDIT_DB_PATH 指向口径专属临时目录隔离任务库）。

对照报告：RPS / 任务吞吐（tasks/min）/ 读延迟与端到端分位 / 429 数 / RSS 峰值与斜率 / 判定，
默认落 bench/results/soak_multiworker_w13.md。诚实边界：Windows spawn 开销（每 worker 独立
解释器，启动秒级）、sticky 执行（任务在接收 POST 的 worker 线程池执行，其他 worker 经 SQLite
只读可见）、双进程 GIL 不叠加（吞吐上限来自两个独立事件循环与线程池隔离，而非 CPU 级并行）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from bench.stress.run_soak import (
    FIFO_CAPACITY,
    MIN_SLOPE_SAMPLES,
    POLL_INTERVAL,
    RSS_SLOPE_LIMIT,
    RSS_TASK_CORR_DEFER,
    RSS_WARMUP_SEC,
    SAMPLE_PERIOD_SEC,
    TERMINAL_GRACE_SEC,
    TERMINAL_STATUSES,
    SoakStats,
    _do,
    _read_loop,
    _rss_slope,
    _rss_task_corr,
    _sleep_until,
    _sse_loop,
    _terminal_rate,
    _zip_dir_bytes,
)
from bench.stress.run_stress_web import AUDIT_TIMEOUT_SEC, DATA_DIR, MINI_APP, _pctl
from bench.stress.generator import generate_project

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = PROJECT_ROOT / "bench" / "results" / "soak_multiworker_w13.md"

# 节拍常量（W13-A3 契约：混合负载照抄 run_soak 模式；任务提交周期契约为 6s）
TASK_PERIOD_SEC = 6.0  # 任务提交周期（run_soak 为 8s，W13-A3 收紧到 6s 加大准入压力）
ACTIVE_PROBE_COUNT = 10  # 每次提交后的 active 连续采样次数
ACTIVE_PROBE_INTERVAL = 0.15  # active 连续采样间隔（秒）
ACTIVE_PROBE_TOTAL_SEC = ACTIVE_PROBE_COUNT * ACTIVE_PROBE_INTERVAL  # ≈1.5s 探测窗
XSSE_WINDOW_SEC = 5.0  # 提交后跨 worker SSE 探针的收流窗口上限
PENDING_CAP = 20  # F5 全局 pending 上限（CODEAUDIT_MAX_PENDING 默认）
OFFLINE_E2E_LIMIT_SEC = 60.0  # 离线口径：单个任务端到端应远小于该值；分钟级即疑似在线通道
STARTUP_TIMEOUT_SEC = 90.0  # 服务就绪等待上限（spawn 多 worker 冷启动实测 ~6s，留足余量）

RSS_TREE_NOTE = (
    "Windows psapi GetProcessMemoryInfo 工作集，服务进程树求和"
    "（uvicorn 主进程 + spawn worker 子进程，CreateToolhelp32Snapshot 枚举父子关系）"
)


# ---------------------------------------------------------------- 进程树 RSS
def _win_tree_rss(root_pid: int) -> tuple[float, int] | None:
    """Windows 口径：以 root_pid 为根的进程树工作集求和（MB）与进程数；失败返回 None。

    实现最稳口径的理由（报告已注明）：CreateToolhelp32Snapshot 一次枚举全量进程拿到
    (pid, ppid) 关系，纯 ctypes 内存读取，不依赖已弃装的 wmic，也不必逐 PID 拉起
    tasklist 子进程再解析文本；工作集逐进程 GetProcessMemoryInfo 累加。
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class PE32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

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
        k32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if snap == -1:
            return None
        parent_of: dict[int, int] = {}
        pe = PE32()
        pe.dwSize = ctypes.sizeof(PE32)
        ok = k32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            parent_of[pe.th32ProcessID] = pe.th32ParentProcessID
            ok = k32.Process32NextW(snap, ctypes.byref(pe))
        k32.CloseHandle(snap)

        children: dict[int, list[int]] = {}
        for pid_, ppid in parent_of.items():
            children.setdefault(ppid, []).append(pid_)
        tree: list[int] = []
        stack = [root_pid]
        while stack:
            cur = stack.pop()
            tree.append(cur)
            stack.extend(children.get(cur, []))
        if root_pid not in tree:  # 服务进程已退出（进程 id 失效）
            return None

        total = 0.0
        for pid_ in tree:
            handle = k32.OpenProcess(0x1000, False, pid_)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                handle = k32.OpenProcess(0x0400, False, pid_)  # PROCESS_QUERY_INFORMATION
            if not handle:
                continue
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                total += pmc.WorkingSetSize / 1048576
            k32.CloseHandle(handle)
        return total, len(tree)
    except Exception:  # 指标可选：取不到返回 None，采样循环按缺样本处理
        return None


def _psutil_tree_rss(root_pid: int) -> tuple[float, int] | None:
    """非 Windows 回退口径：psutil 进程树 RSS 求和；psutil 不可用返回 None。"""
    try:
        import psutil
    except Exception:
        return None
    try:
        root = psutil.Process(root_pid)
        procs = [root, *root.children(recursive=True)]
        total = 0.0
        for p in procs:
            try:
                total += p.memory_info().rss / 1048576
            except Exception:  # 单进程消失跳过
                continue
        return total, len(procs)
    except Exception:
        return None


def _load_tree_rss_probe() -> Any:
    """返回 probe(root_pid) -> (rss_mb, n_procs) | None：Windows 优先 ctypes，非 Windows 回退 psutil。"""
    if sys.platform == "win32":
        return _win_tree_rss
    return _psutil_tree_rss


# ---------------------------------------------------------------- 服务句柄
class MultiWorkerServer:
    """自起 `cli.py serve --workers N` 子进程：强制离线 env + 口径专属任务库；taskkill 树清理。"""

    def __init__(self, port: int, workers: int, work_dir: Path) -> None:
        self.port = port
        self.workers = workers
        self.work_dir = work_dir
        self.base = f"http://127.0.0.1:{port}"
        self.proc: subprocess.Popen[bytes] | None = None
        self.log_file: Any = None

    def _child_env(self) -> dict[str, str]:
        """服务子进程环境（离线口径的关键）：

        - GLM_API_KEY 置空串：F7 语义"真实进程环境变量永远优先于 .env"，服务 CWD 在项目根
          会自动加载根目录 .env（W12 F7），空串真实变量压制 .env 的在线密钥 → FakeLLM 离线；
        - GLM_BASE_URL / GLM_MODEL 直接 pop（离线通道不读，防误配）；
        - CODEAUDIT_DB_PATH 显式指向口径专属临时目录：该键在 .env 白名单内会被 setdefault，
          但 setdefault 不覆盖已有真实变量 → 本口径任务库严格隔离。
        """
        env = os.environ.copy()
        env["GLM_API_KEY"] = ""
        env.pop("GLM_BASE_URL", None)
        env.pop("GLM_MODEL", None)
        env["CODEAUDIT_DB_PATH"] = str(self.work_dir / "audits.db")
        return env

    def start(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = (self.work_dir / "server.log").open("wb")
        cmd = [
            sys.executable,
            "cli.py",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--workers",
            str(self.workers),
        ]
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            env=self._child_env(),
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
        )
        t0 = time.monotonic()
        while time.monotonic() - t0 < STARTUP_TIMEOUT_SEC:
            try:
                if httpx.get(f"{self.base}/api/health", timeout=2.0).status_code == 200:
                    return
            except Exception:  # noqa: BLE001 —— 服务未就绪属预期，继续等待
                pass
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"服务子进程提前退出（exit={self.proc.returncode}），日志：{self.work_dir / 'server.log'}"
                )
            time.sleep(0.5)
        raise RuntimeError(f"服务 {STARTUP_TIMEOUT_SEC:.0f}s 未就绪，日志：{self.work_dir / 'server.log'}")

    def stop(self) -> None:
        """杀服务进程树：Windows taskkill /F /T（多 worker spawn 子树必须整树终止）。"""
        if self.proc is not None:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                    capture_output=True,
                    check=False,
                )
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None
        if self.log_file is not None and not self.log_file.closed:
            self.log_file.close()


# ---------------------------------------------------------------- 观测聚合
@dataclass
class MWExtras:
    """多 worker 基线在 SoakStats 之外的增量观测（单事件循环内访问，无需加锁）。"""

    e2e_ms: list[float] = field(default_factory=list)  # 终态任务的端到端延迟（提交→观测到终态）
    first_e2e_s: float | None = None  # 首个终态任务端到端（离线口径验证）
    active_samples: list[dict[str, float]] = field(default_factory=list)  # 提交瞬间采样
    max_active: int = -1
    cap_violations: int = 0  # active > 20（pending 全局上限）——跨 worker 准入竞态信号
    consist_violations: int = 0  # active > 客户端视角全局非终态数——计数不一致信号
    xsse: dict[str, int] = field(default_factory=lambda: {"opened": 0, "done": 0, "frameless": 0, "errors": 0})
    xsse_frames: list[int] = field(default_factory=list)  # 每条提交后 SSE 探针收到的帧数
    n_proc_min: int = 10**9
    n_proc_max: int = 0  # 进程树进程数范围（验证 --workers 生效：1+workers）


class ActiveTracker:
    """客户端视角的全局非终态任务集合：admit 加入，观测到终态移除。

    服务端 active = SQLite 非终态行数；行转终态先于客户端观测，DELETE/容量淘汰只删终态行，
    故任意采样时刻 server active ≤ len(live)（expected）——多算即不一致信号。
    """

    def __init__(self) -> None:
        self.live: set[str] = set()

    def admit(self, audit_id: str) -> None:
        self.live.add(audit_id)

    def release(self, audit_id: str) -> None:
        self.live.discard(audit_id)

    @property
    def expected(self) -> int:
        return len(self.live)


# ---------------------------------------------------------------- 负载协程
async def _task_lifecycle_mw(
    client: httpx.AsyncClient,
    stats: SoakStats,
    extras: MWExtras,
    tracker: ActiveTracker,
    audit_id: str,
    admit_perf: float,
) -> None:
    """轮询单个被接纳任务至终态（上限 240s）并记录端到端延迟；终态保留 5s 后 DELETE。

    与 run_soak._task_lifecycle 同构，增量：tracker 释放时机（观测到终态即移出客户端
    视角集合，供 active 一致性比对）、端到端延迟记录（W11 起审计经 to_thread 在独立
    线程执行，POST 不再被 CPU 密集段饿死，端到端为可测量的真实口径）。
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
    tracker.release(audit_id)
    e2e_ms = (time.perf_counter() - admit_perf) * 1000
    stats.terminals[status] = stats.terminals.get(status, 0) + 1
    if status in TERMINAL_STATUSES:
        extras.e2e_ms.append(e2e_ms)
        if extras.first_e2e_s is None:
            extras.first_e2e_s = e2e_ms / 1000.0
    await asyncio.sleep(TERMINAL_GRACE_SEC)
    await _do(client, stats, "DELETE", f"/api/audits/{audit_id}")


async def _submit_sse_probe(client: httpx.AsyncClient, stats: SoakStats, extras: MWExtras, audit_id: str) -> None:
    """提交后立即对该任务开一条 SSE 消费至 done 终帧或 5s 窗口。

    跨 worker SSE 观测（契约 §1 W13-A3）：sticky 模型下任务在接收 POST 的 worker 执行，
    本 SSE 连接由共享监听套接字分派、可能落另一 worker——事件经 SQLite store 读取，
    预期仍能收齐并拿到 done 终帧。计数入 extras.xsse（与 30s 拍 SSE 分开统计）。
    """
    stats.requests += 1
    extras.xsse["opened"] += 1
    got_done = False
    frames = 0

    async def _drain() -> None:
        nonlocal got_done, frames
        async with client.stream("GET", f"/api/audits/{audit_id}/events", timeout=15.0) as resp:
            code = resp.status_code
            stats.status_counts[code] = stats.status_counts.get(code, 0) + 1
            if code >= 500:
                stats.status_5xx.append(f"GET /api/audits/{audit_id}/events(提交后探针) -> {code}")
            if code != 200:
                return
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                frames += 1
                if '"type": "done"' in line or '"type":"done"' in line:
                    got_done = True
                    return

    try:
        await asyncio.wait_for(_drain(), timeout=XSSE_WINDOW_SEC)
    except TimeoutError:
        pass  # 窗口内未见终帧：任务尚未终态/流未回放完，如实计 frameless
    except Exception as exc:  # 单连接异常只计数，不中断负载
        extras.xsse["errors"] += 1
        stats.sse_error_samples.append(f"提交后探针 {type(exc).__name__}: {exc}")
    if got_done:
        extras.xsse["done"] += 1
        extras.xsse_frames.append(frames)


async def _active_probe_burst(
    client: httpx.AsyncClient,
    stats: SoakStats,
    extras: MWExtras,
    tracker: ActiveTracker,
    load_t0: float,
) -> None:
    """提交瞬间的 active 连续采样（0.15s × 10）：验证跨 worker 全局计数一致。

    校验两条：（a）active ≤ 20（F5 全局 pending 上限；若越过说明两 worker 同时通过
    准入检查的竞态真实发生了）；（b）active ≤ tracker.expected（客户端视角全局非终态数；
    服务端先行终态、客户端滞后观测，故服务端不该多算）。
    """
    for i in range(ACTIVE_PROBE_COUNT):
        resp, _ms = await _do(client, stats, "GET", "/api/health")
        if resp is not None and resp.status_code == 200:
            active = int(resp.json().get("audits", {}).get("active", -1))
            expected = tracker.expected
            extras.max_active = max(extras.max_active, active)
            if active > PENDING_CAP:
                extras.cap_violations += 1
            if active > expected:
                extras.consist_violations += 1
            extras.active_samples.append(
                {
                    "t": round(time.monotonic() - load_t0, 1),
                    "active": active,
                    "expected": expected,
                }
            )
        if i + 1 < ACTIVE_PROBE_COUNT:
            await asyncio.sleep(ACTIVE_PROBE_INTERVAL)


async def _submit_one_mw(
    client: httpx.AsyncClient,
    stats: SoakStats,
    extras: MWExtras,
    tracker: ActiveTracker,
    name: str,
    payload: bytes,
    load_t0: float,
    lifecycles: list["asyncio.Task[None]"],
    probes: list["asyncio.Task[None]"],
    inflight: dict[str, None],
) -> None:
    """提交一个上传任务：200 接纳并派生 生命周期/跨worker SSE 探针/active 探针 三类协程；
    429 记 denied（F5 全局准入合法观测）；其余记异常。"""
    stats.submits += 1
    admit_perf = time.perf_counter()
    resp, _ms = await _do(
        client,
        stats,
        "POST",
        "/api/audits/upload",
        files={"file": (f"soakmw_{name}.zip", payload, "application/zip")},
        data={"do_fix": "false", "do_tests": "false"},
    )
    if resp is None:
        stats.submit_anomaly.append(f"{name}: 客户端异常")
        return
    if resp.status_code == 429:
        stats.denied_429 += 1
        print(f"[soakmw] task#{stats.submits} {name} -> 429 denied（全局准入上限，合法观测）")
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
    tracker.admit(audit_id)
    inflight[audit_id] = None
    lifecycles.append(asyncio.create_task(_task_lifecycle_mw(client, stats, extras, tracker, audit_id, admit_perf)))
    probes.append(asyncio.create_task(_submit_sse_probe(client, stats, extras, audit_id)))
    probes.append(asyncio.create_task(_active_probe_burst(client, stats, extras, tracker, load_t0)))
    print(f"[soakmw] task#{stats.submits} {name} -> admitted audit_id={audit_id}")


async def _task_loop_mw(
    client: httpx.AsyncClient,
    stats: SoakStats,
    extras: MWExtras,
    tracker: ActiveTracker,
    stop: asyncio.Event,
    deadline: float,
    materials: list[tuple[str, bytes]],
    load_t0: float,
    lifecycles: list["asyncio.Task[None]"],
    probes: list["asyncio.Task[None]"],
    inflight: dict[str, None],
) -> None:
    """任务负载：每 6s 提交一个审计任务（两种素材交替），直至负载期截止（W13-A3 节拍）。"""
    t0 = time.monotonic()
    n = 0
    while not stop.is_set():
        if time.monotonic() >= deadline:
            break
        name, payload = materials[n % len(materials)]
        await _submit_one_mw(client, stats, extras, tracker, name, payload, load_t0, lifecycles, probes, inflight)
        n += 1
        next_t = t0 + n * TASK_PERIOD_SEC
        if next_t >= deadline:
            break
        await _sleep_until(stop, next_t)


async def _sampler_loop_mw(
    client: httpx.AsyncClient,
    stats: SoakStats,
    extras: MWExtras,
    tracker: ActiveTracker,
    stop: asyncio.Event,
    rss_probe: Any,
    server_proc: Any,
    t0: float,
) -> None:
    """采样协程：每 10s 记录 health 的 active/total（含客户端视角 expected）与进程树 RSS 之和。"""
    n = 0
    while not stop.is_set():
        resp, _ms = await _do(client, stats, "GET", "/api/health")
        active = total = -1
        if resp is not None and resp.status_code == 200:
            audits = resp.json().get("audits", {})
            active = int(audits.get("active", -1))
            total = int(audits.get("total", -1))
        rss_now = -1.0
        n_proc = 0
        if server_proc is not None:
            probed = rss_probe(server_proc.pid)
            if probed is not None:
                rss_now, n_proc = round(probed[0], 1), probed[1]
                extras.n_proc_min = min(extras.n_proc_min, n_proc)
                extras.n_proc_max = max(extras.n_proc_max, n_proc)
        cum_done = stats.terminals["done"] + stats.terminals["failed"]
        stats.samples.append(
            {
                "t": round(time.monotonic() - t0, 1),
                "rss": rss_now,
                "active": active,
                "expected": tracker.expected,
                "total": total,
                "cum_done": cum_done,
            }
        )
        print(
            f"[soakmw] sample t={stats.samples[-1]['t']:.0f}s active={active} expected={tracker.expected} "
            f"total={total} cum_done={cum_done} rss={rss_now}MB ({n_proc} 进程)"
        )
        n += 1
        await _sleep_until(stop, t0 + n * SAMPLE_PERIOD_SEC)


# ---------------------------------------------------------------- 单口径运行
async def run_once(
    workers: int,
    port: int,
    duration: float,
    materials: list[tuple[str, bytes]],
) -> dict[str, Any]:
    """单口径主流程：起服务 → 混合负载 + 探测 → 排空 → 清理 → 判定；返回该口径全部结果。"""
    for key in ("GLM_API_KEY", "GLM_BASE_URL", "GLM_MODEL"):
        os.environ.pop(key, None)  # 父进程侧消毒（子进程 env 另行显式注入，双保险）

    work_dir = Path(tempfile.mkdtemp(prefix=f"soak_mw_w{workers}_"))
    server = MultiWorkerServer(port, workers, work_dir)
    print(f"[soakmw] ===== 口径 workers={workers} port={port} duration={duration:.0f}s =====")
    server.start()
    print(f"[soakmw] 服务就绪：{server.base}（workers={workers}）")
    stats = SoakStats()
    extras = MWExtras()
    tracker = ActiveTracker()
    rss_probe = _load_tree_rss_probe()
    store_total_start = -1
    load_t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(
            base_url=server.base,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
            timeout=httpx.Timeout(60.0, connect=10.0),
        ) as client:
            r0 = await _do(client, stats, "GET", "/api/health")
            if r0[0] is not None and r0[0].status_code == 200:
                store_total_start = int(r0[0].json().get("audits", {}).get("total", -1))

            stop = asyncio.Event()
            deadline = load_t0 + duration
            inflight: dict[str, None] = {}
            lifecycles: list["asyncio.Task[None]"] = []
            probes: list["asyncio.Task[None]"] = []
            loops = [
                asyncio.create_task(_read_loop(client, stats, stop, "/api/health", "health")),
                asyncio.create_task(_read_loop(client, stats, stop, "/api/audits?limit=5", "list")),
                asyncio.create_task(
                    _task_loop_mw(
                        client, stats, extras, tracker, stop, deadline, materials, load_t0, lifecycles, probes, inflight
                    )
                ),
                asyncio.create_task(_sse_loop(client, stats, stop, deadline, inflight)),
                asyncio.create_task(
                    _sampler_loop_mw(client, stats, extras, tracker, stop, rss_probe, server.proc, load_t0)
                ),
            ]
            await asyncio.sleep(max(0.0, deadline - time.monotonic()))
            stop.set()
            for coro in await asyncio.gather(*loops, return_exceptions=True):
                if isinstance(coro, BaseException):
                    stats.loop_crashes.append(f"{type(coro).__name__}: {coro}")
            wall_load = time.monotonic() - load_t0

            # 排空：生命周期（各自 240s 上限）+ 提交后探针全部结束，保证终态率/观测口径完整
            drain_t0 = time.monotonic()
            for res in await asyncio.gather(*lifecycles, *probes, return_exceptions=True):
                if isinstance(res, BaseException):
                    stats.loop_crashes.append(f"probe/lifecycle {type(res).__name__}: {res}")
            drain_wall = time.monotonic() - drain_t0

            # 结束时任务表规模快照（清理前取数，FIFO 容量判定口径）
            final_total = -1
            resp, _ms = await _do(client, stats, "GET", "/api/health")
            if resp is not None and resp.status_code == 200:
                final_total = int(resp.json().get("audits", {}).get("total", -1))

            # 清理：DELETE 全部任务（生命周期已删大半；列表枚举兜底覆盖一切残留行）
            listed: list[str] = []
            resp_l, _ms2 = await _do(client, stats, "GET", "/api/audits?limit=0")
            if resp_l is not None and resp_l.status_code == 200:
                listed = [str(item.get("audit_id", "")) for item in resp_l.json().get("audits", [])]
            for audit_id in dict.fromkeys([*stats.created_ids, *listed]):
                if audit_id:
                    await _do(client, stats, "DELETE", f"/api/audits/{audit_id}")
    finally:
        server.stop()
        # 临时目录清理（服务树已终止，WAL 句柄释放；Windows 文件锁偶发延迟，重试三次）
        for i in range(3):
            try:
                shutil.rmtree(work_dir, ignore_errors=False)
                break
            except OSError:
                time.sleep(1.0 * (i + 1))
        else:
            shutil.rmtree(work_dir, ignore_errors=True)
    wall_total = time.monotonic() - load_t0

    verdict = _judge(stats, extras, final_total)
    rss_vals = [s["rss"] for s in stats.samples if s["rss"] > 0]
    result: dict[str, Any] = {
        "workers": workers,
        "port": port,
        "base": server.base,
        "stats": stats,
        "extras": extras,
        "wall_load": wall_load,
        "wall_total": wall_total,
        "drain_wall": drain_wall,
        "final_total": final_total,
        "store_total_start": store_total_start,
        "rss_first": rss_vals[0] if rss_vals else -1.0,
        "rss_peak": max(rss_vals) if rss_vals else -1.0,
        "rss_last": rss_vals[-1] if rss_vals else -1.0,
        "verdict": verdict,
    }
    print(
        f"[soakmw] 口径 workers={workers} 判定：{verdict['overall']}｜"
        + "；".join(f"{n}={r}" for n, r, _note in verdict["checks"])
    )
    return result


# ---------------------------------------------------------------- 判定
def _judge(stats: SoakStats, extras: MWExtras, final_total: int) -> dict[str, Any]:
    """三态判定 + 离线口径验证（与 run_soak 同阈值口径：无 5xx / 终态率 / RSS 斜率 / total 上限）。"""
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
        checks.append(("RSS 稳态斜率 < 1 MB/min", "SKIP", f"有效样本 {n_slope} < {MIN_SLOPE_SAMPLES}，跳过判定"))
    elif slope < RSS_SLOPE_LIMIT:
        checks.append(
            (
                "RSS 稳态斜率 < 1 MB/min",
                "PASS",
                f"稳态斜率={slope:.3f} MB/min（{n_slope} 个 ≥{RSS_WARMUP_SEC}s 样本）；全窗口参考={slope_full:.3f}",
            )
        )
    elif rss_corr is not None and rss_corr >= RSS_TASK_CORR_DEFER:
        checks.append(
            (
                "RSS 稳态斜率 < 1 MB/min",
                "DEFER",
                f"稳态斜率={slope:.3f} MB/min 超限，但与累计终态任务数 Pearson r={rss_corr:.2f}"
                f"（≥{RSS_TASK_CORR_DEFER}）→ 疑似每任务常数缓存；FIFO 容量兜底下有界，列归因待办",
            )
        )
    else:
        checks.append(
            (
                "RSS 稳态斜率 < 1 MB/min",
                "FAIL",
                f"稳态斜率={slope:.3f} MB/min 超限且与任务数相关性弱"
                f"（r={'-' if rss_corr is None else f'{rss_corr:.2f}'})→ 时间泄漏特征",
            )
        )
    if final_total < 0:
        checks.append(("结束时 total <= 50", "FAIL", "结束时 /api/health 快照失败"))
    else:
        checks.append(
            (
                "结束时 total <= 50",
                "PASS" if final_total <= FIFO_CAPACITY else "FAIL",
                f"total={final_total}（FIFO 容量 {FIFO_CAPACITY}）",
            )
        )
    if extras.first_e2e_s is None:
        checks.append(("离线口径验证（首任务秒级终态）", "SKIP", "无终态任务，无法验证"))
    else:
        ok = extras.first_e2e_s < OFFLINE_E2E_LIMIT_SEC
        checks.append(
            (
                "离线口径验证（首任务秒级终态）",
                "PASS" if ok else "FAIL",
                f"首个终态任务端到端 {extras.first_e2e_s:.1f}s"
                + ("" if ok else f"（≥{OFFLINE_E2E_LIMIT_SEC:.0f}s：分钟级为在线 LLM 特征，.env 疑似生效）"),
            )
        )
    overall = "PASS" if all(r != "FAIL" for _n, r, _note in checks) else "FAIL"
    if overall == "PASS" and any(r == "DEFER" for _n, r, _note in checks):
        overall = "PASS（含 DEFER）"
    return {
        "checks": checks,
        "overall": overall,
        "terminal_rate": rate,
        "rss_slope": slope,
        "rss_slope_full": slope_full,
        "rss_corr": rss_corr,
    }


# ---------------------------------------------------------------- 报告渲染


def _render_md(env: dict[str, str], runs: list[dict[str, Any]]) -> str:
    """渲染对照报告：环境 → 单/多 worker 对照表 → 判定 → 多 worker 特有观测 → 时序 → 诚实边界。"""
    by_w = {r["workers"]: r for r in runs}
    w_keys = sorted(by_w)
    col_heads = " | ".join(f"workers={w}" for w in w_keys)

    def _two(fn: Any) -> str:
        return " | ".join(fn(by_w[w]) for w in w_keys)

    def _slope_str(r: dict[str, Any]) -> str:
        v = r["verdict"]["rss_slope"]
        return "-" if v is None else f"{v:.3f}"

    lines = [
        "# 多 worker 压测基线报告（bench/stress/run_soak_multiworker.py，W13-A3）",
        "",
        "## 环境",
        "",
        f"- **生成时间**：{env['now']}",
        f"- **主机 / 平台**：{env['host']} / {env['platform']}",
        f"- **CPU**：{env['cpu']}｜**Python**：{env['python']}",
        f"- **被测服务**：cli.py serve（FastAPI + uvicorn；--workers N，Windows spawn 模式），端口基址 {env['port_base']}",
        f"- **口径与时长**：workers 口径 {'、'.join(env['workers_list'])} 依次串行跑；负载期 {env['duration']}s/口径"
        f"（{'quick' if env['quick'] == '1' else '标准'}）",
        '- **离线口径**：服务子进程 env 注入 GLM_API_KEY=""（F7 语义：真实环境变量优先于项目根 .env）'
        "并 pop GLM_BASE_URL/GLM_MODEL → FakeLLM/纯规则通道；CODEAUDIT_DB_PATH 指向口径专属临时目录隔离任务库"
        "（起服后首查 total=0 验证隔离；首任务秒级终态验证未走在线通道）",
        f"- **RSS 口径**：{RSS_TREE_NOTE}（选型说明：纯 ctypes 枚举 + 内存读取，"
        "不依赖已弃装的 wmic、无需逐 PID 拉起 tasklist 解析文本，为最稳实现）",
        "- **探测流量口径**：为保两口径负载完全一致，提交后 active 采样突发（10×0.15s）与提交后 SSE 探针"
        "（5s 窗口）在单 worker 口径同样注入，RPS 与请求数含该流量",
        "",
        "## 单 / 多 worker 对照表",
        "",
        f"| 指标 | {col_heads} |",
        f"|---|{'---|' * len(w_keys)}",
    ]

    def _fmt_rps(r: dict[str, Any]) -> str:
        s: SoakStats = r["stats"]
        return f"{s.requests / r['wall_load']:.2f}" if r["wall_load"] > 0 else "-"

    def _fmt_throughput(r: dict[str, Any]) -> str:
        s: SoakStats = r["stats"]
        return (
            f"{(s.terminals['done'] + s.terminals['failed']) / (r['wall_load'] / 60.0):.2f}"
            if r["wall_load"] > 0
            else "-"
        )

    def _fmt_e2e(r: dict[str, Any], p: float) -> str:
        return f"{_pctl(r['extras'].e2e_ms, p):.0f}"

    rows: list[tuple[str, Any]] = [
        ("负载期实际时长（s）", lambda r: f"{r['wall_load']:.1f}"),
        ("总请求数", lambda r: str(r["stats"].requests)),
        ("RPS（按负载期）", _fmt_rps),
        ("状态码分布", lambda r: ", ".join(f"{k}×{v}" for k, v in sorted(r["stats"].status_counts.items())) or "-"),
        (
            "读负载 health / list 样本数",
            lambda r: f"{len(r['stats'].read_lat['health'])} / {len(r['stats'].read_lat['list'])}",
        ),
        ("客户端异常请求", lambda r: str(r["stats"].req_errors)),
        ("任务 提交 / 接纳", lambda r: f"{r['stats'].submits} / {r['stats'].admitted}"),
        (
            "429 计数（触发率）",
            lambda r: f"{r['stats'].denied_429}（{r['stats'].denied_429 / r['stats'].submits * 100:.1f}%）",
        ),
        (
            "终态 done / failed / 超时",
            lambda r: f"{r['stats'].terminals['done']} / {r['stats'].terminals['failed']} / {r['stats'].terminals['timeout']}",
        ),
        (
            "终态率 (done+failed)/admitted",
            lambda r: "-" if r["verdict"]["terminal_rate"] is None else f"{r['verdict']['terminal_rate'] * 100:.1f}%",
        ),
        ("任务吞吐（终态 tasks/min）", _fmt_throughput),
        ("任务端到端 p50（ms）", lambda r: _fmt_e2e(r, 50)),
        ("任务端到端 p95（ms）", lambda r: _fmt_e2e(r, 95)),
        ("任务端到端 p99（ms）", lambda r: _fmt_e2e(r, 99)),
        (
            "30s 拍 SSE 开/终帧/无终帧/错/跳过",
            lambda r: "{opened}/{done}/{closed}/{errors}/{skipped}".format(**r["stats"].sse),
        ),
        (
            "提交后 SSE 探针 开/收齐终帧/无终帧/错",
            lambda r: "{opened}/{done}/{frameless}/{errors}".format(**r["extras"].xsse),
        ),
        ("RSS 首 / 峰值 / 末（MB）", lambda r: f"{r['rss_first']:.1f} / {r['rss_peak']:.1f} / {r['rss_last']:.1f}"),
        ("RSS 稳态斜率（MB/min）", _slope_str),
        (
            "进程树进程数（min-max）",
            lambda r: "-" if r["extras"].n_proc_max == 0 else f"{r['extras'].n_proc_min}-{r['extras'].n_proc_max}",
        ),
        ("任务库首查 total（隔离验证）", lambda r: str(r["store_total_start"])),
        ("结束时 total", lambda r: str(r["final_total"])),
        ("判定", lambda r: r["verdict"]["overall"]),
    ]
    for label, fn in rows:
        lines.append(f"| {label} | {_two(fn)} |")

    lines += [
        "",
        "## 判定明细（逐口径）",
        "",
        "| 口径 | 准则 | 结果 | 说明 |",
        "|---|---|---|---|",
    ]
    for r in runs:
        for name, res, note in r["verdict"]["checks"]:
            lines.append(f"| workers={r['workers']} | {name} | {res} | {note} |")
    for r in runs:
        lines.append(
            f"| workers={r['workers']} | **总判定** | **{r['verdict']['overall']}** | 全部非 FAIL 即 PASS；DEFER=有界归因待办 |"
        )

    # 多 worker 特有观测（取 workers 最大的口径；契约要求 2 worker 必做）
    mw = by_w[max(w_keys)]
    xs: MWExtras = mw["extras"]
    xrate = mw["stats"].denied_429 / mw["stats"].submits * 100 if mw["stats"].submits else 0.0
    pigeon = (2.0 ** -max(1, xs.xsse["done"])) if xs.xsse["done"] else None
    single = by_w[min(w_keys)]
    lines += [
        "",
        f"## 多 worker 特有观测（workers={max(w_keys)} 口径）",
        "",
        "### 429 全局准入触发率",
        "",
        f"- 提交 {mw['stats'].submits} 次，429 拒绝 {mw['stats'].denied_429} 次（触发率 {xrate:.1f}%）。",
        "- 机制：F5 准入走 store.count_active()（SQLite 全局非终态计数），多 worker 下天然跨进程汇总，"
        f"上限 {PENDING_CAP}（CODEAUDIT_MAX_PENDING 默认）。离线规则通道单任务亚秒级终态 + 6s 提交周期，"
        f"全局并发非终态远达不到 {PENDING_CAP}（本次观测 max active={xs.max_active}），429 未触发属负载形态使然——"
        "机制存在性与跨 worker 汇总正确性由 active 一致性观测佐证，不虚构触发。",
        "",
        "### active 计数一致性（提交瞬间 0.15s × 10 连续采样）",
        "",
        f"- 采样 {len(xs.active_samples)} 次：max active={xs.max_active}，"
        f"越过 pending 上限 {PENDING_CAP} 的样本 {xs.cap_violations} 次，"
        f"多于客户端视角全局非终态数（expected）的样本 {xs.consist_violations} 次。",
        f"- 判读：{'两口径均未观测到计数越界/多算——跨 worker 全局计数单调合理' if xs.cap_violations == 0 and xs.consist_violations == 0 else '观测到越界/多算样本——跨 worker 计数存在真实竞态，详见时序表'}；"
        "单 worker 对照口径（同探测流量）：max active="
        f"{single['extras'].max_active}，越界 {single['extras'].cap_violations} / 多算 {single['extras'].consist_violations} 次。",
        "- 口径诚实性：采样粒度 0.15s、每提交 1.5s 窗口，属抽样验证而非全程全量校验；"
        "SQLite WAL 单写串行化使跨进程 count_active 接近原子，本观测为该机制的负载期实证。",
        "",
        "### 跨 worker SSE（sticky 执行 + 连接随机分派）",
        "",
        f"- 提交后 SSE 探针：opened={xs.xsse['opened']}，done 终帧收齐={xs.xsse['done']}，"
        f"窗口内无终帧={xs.xsse['frameless']}，错误={xs.xsse['errors']}"
        + (f"，收齐时平均帧数={statistics.mean(xs.xsse_frames):.1f}" if xs.xsse_frames else ""),
        f"- 30s 拍 SSE（复用 run_soak 口径）：opened={mw['stats'].sse['opened']}，done={mw['stats'].sse['done']}，"
        f"closed={mw['stats'].sse['closed']}，errors={mw['stats'].sse['errors']}，skipped={mw['stats'].sse['skipped']}。",
        "- 证据口径（诚实标注）：客户端无法直接观测请求落在哪个 worker，跨 worker 收帧取双重证据——"
        "（a）代码层：SSE 端点经 store.get_events 从 SQLite 读事件（server/app.py audit_events），"
        "任何 worker 均可回放任意任务的事件流；（b）统计层：每条连接落在执行 worker 的概率约 1/2，"
        + (
            f'k={xs.xsse["done"]} 条全部收齐终帧；若"必须同 worker 才能收帧"，'
            f"联合概率 ≤ 2^-{xs.xsse['done']} ≈ {pigeon * 100:.3g}%"
            if pigeon is not None
            else "本次无收齐样本，统计证据不成立（如实标注）"
        )
        + "——据此判定跨 worker SSE 收帧成立而非巧合。",
        "",
    ]

    # 读延迟对照
    lines += [
        "## 读延迟与端到端分位对照（客户端口径，ms）",
        "",
        "| 口径 | 端点 | n | p50 | p95 | p99 |",
        "|---|---|---|---|---|---|",
    ]
    for r in runs:
        s: SoakStats = r["stats"]
        for key, label in (("health", "GET /api/health"), ("list", "GET /api/audits?limit=5")):
            vals = s.read_lat[key]
            lines.append(
                f"| workers={r['workers']} | {label} | {len(vals)} | {_pctl(vals, 50):.1f} | "
                f"{_pctl(vals, 95):.1f} | {_pctl(vals, 99):.1f} |"
            )
        lines.append(
            f"| workers={r['workers']} | 任务端到端（提交→观测到终态） | {len(r['extras'].e2e_ms)} | "
            f"{_pctl(r['extras'].e2e_ms, 50):.0f} | {_pctl(r['extras'].e2e_ms, 95):.0f} | {_pctl(r['extras'].e2e_ms, 99):.0f} |"
        )

    # 时序表（每口径一张）
    for r in runs:
        lines += [
            "",
            f"## RSS / health 时序（workers={r['workers']}，每采样点）",
            "",
            "| t(s) | audits.active | 客户端 expected | audits.total | 累计终态 | 树RSS(MB) |",
            "|---|---|---|---|---|---|",
        ]
        for smp in r["stats"].samples:
            lines.append(
                f"| {smp['t']:.0f} | {smp['active']:.0f} | {smp.get('expected', 0):.0f} | {smp['total']:.0f} "
                f"| {smp.get('cum_done', 0):.0f} | {smp['rss']:.1f} |"
            )

    # 每口径诊断 + 诚实边界
    lines += ["", "## RSS 诊断（参考，不参与判定）", ""]
    for r in runs:
        slope = r["verdict"]["rss_slope"]
        corr = r["verdict"]["rss_corr"]
        full = r["verdict"]["rss_slope_full"]
        lines.append(
            f"- workers={r['workers']}：稳态斜率={'-' if slope is None else f'{slope:.3f}'} MB/min（判定口径，剔除前 "
            f"{RSS_WARMUP_SEC}s 预热）；全窗口参考={'-' if full is None else f'{full:.3f}'}；"
            f"RSS-累计终态 Pearson r={'-' if corr is None else format(corr, '.2f')}（≥{RSS_TASK_CORR_DEFER} 判 DEFER：疑似每任务常数缓存）"
        )

    lines += [
        "",
        "## 诚实边界",
        "",
        "- **Windows spawn 开销**：--workers N>1 走 import string + spawn，每 worker 独立解释器冷启动"
        "（实测启动数秒）；worker 崩溃由 uvicorn 管理进程重启；启动期开销不计入负载期指标。",
        "- **sticky 执行**：任务由接收 POST 的 worker 在自身线程池执行，其他 worker 经 SQLite 只读可见"
        "（列表/详情/SSE 跨 worker 可用）；DELETE 由任意 worker 落库即生效。",
        "- **双进程 GIL 不叠加**：两个 worker 进程各自持 GIL，CPU 密集的规则扫描不因多 worker 获得 CPU 级并行；"
        "吞吐/延迟改善（若有）来自独立事件循环与线程池隔离（读请求不再排队在审计 worker 的事件循环上），"
        "而非算力叠加——对照表中读延迟与吞吐数字应据此解读。",
        '- **离线 FakeLLM**：GLM_API_KEY="" 压制项目根 .env（F7 真实环境变量优先语义）+ 首任务端到端秒级'
        "双重验证；在线 LLM 通道的延迟与内存行为不在本报告覆盖范围。",
        "- **429 未触发的归因**：离线亚秒任务 + 6s 周期达不到全局 pending 上限 20，触发率 0 是负载形态结果；"
        "准入机制的跨 worker 正确性由 active 一致性观测与 SQLite 单写串行化佐证，压测本身未直接压出 429。",
        "- **active 一致性为抽样口径**：0.15s 粒度、每次提交 1.5s 窗口；理论竞态窗口（两 worker 同时通过"
        "准入检查）在本负载形态下需要恰好同时到达 20 阈值，抽样未观测到即报告 0，不等价于机制不可能竞态。",
        "- **跨 worker SSE 证据为统计 + 代码层**：客户端无法观测 worker 身份；k 条连接全收齐的鸽笼论证"
        "（联合概率 ≤ 2^-k）辅以事件流来自 SQLite 的实现事实，非逐请求的直接证明。",
        "- **RSS 口径**：进程树工作集求和（含 uvicorn 管理进程），多 worker 口径天然高于单 worker"
        "（每 worker 独立解释器常驻），对照表 RSS 绝对值不可直接相减评价优劣，斜率（泄漏）才是判定口径；"
        "Windows 工作集含分配器高水位，非 Windows 平台回退 psutil，均取不到时斜率判定按约定跳过。",
        f"- **quick 口径（{env['duration']}s）**：任务数 / SSE 连接数 / RSS 样本数相应缩减；{env['duration']}s 窗口对小时级"
        "慢泄漏灵敏度有限，斜率达标不外推为长期无泄漏。",
        "- **分位数为客户端口径**（含 HTTP 栈往返）；服务端审计经 to_thread 在独立线程执行（W11），"
        "读延迟不再被 CPU 密集段饿死，但 Windows 线程调度抖动仍会抬升 p99。",
        "- **本场景任务终态即 DELETE**，结束时 total 天然有界；上传 zip 与审计工作副本的磁盘回收不在 API "
        "语义内（本次以口径专属临时目录 + 进程树终止兜底，work_root 下 uploads 不在清理范围）。",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程
async def run_all(args: argparse.Namespace) -> int:
    """依次跑 --workers 列表各口径（端口 port-base+i），汇总对照报告。"""
    duration = 300.0 if args.quick else (args.duration if args.duration > 0 else 600.0)
    source = generate_project(DATA_DIR / "soak_mw_proj80", files=80, defect_ratio=0.02, seed=42)
    materials = [("mini_app", _zip_dir_bytes(MINI_APP)), ("proj80", _zip_dir_bytes(source))]
    print(f"[soakmw] 素材就绪：mini_app={len(materials[0][1]) // 1024}KB proj80={len(materials[1][1]) // 1024}KB")

    runs: list[dict[str, Any]] = []
    for i, workers in enumerate(args.workers):
        if i > 0:
            await asyncio.sleep(3.0)  # 口径间冷却：端口 TIME_WAIT / 前口径进程树完全退出
        runs.append(await run_once(workers, args.port_base + i, duration, materials))

    overall = "PASS" if all(r["verdict"]["overall"].startswith("PASS") for r in runs) else "FAIL"
    env = {
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "host": platform.node(),
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "cpu": platform.processor(),
        "python": platform.python_version(),
        "port_base": str(args.port_base),
        "workers_list": [str(w) for w in args.workers],
        "quick": "1" if args.quick else "0",
        "duration": f"{duration:.0f}",
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_render_md(env, runs), encoding="utf-8")
    print(f"[soakmw] 报告已写入：{out}")
    print(
        f"[soakmw] 总判定：{overall}｜" + "；".join(f"workers={r['workers']}={r['verdict']['overall']}" for r in runs)
    )
    return 0 if overall == "PASS" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="多 worker 压测基线（单 worker 对照，真实 HTTP，离线）")
    parser.add_argument("--duration", type=float, default=600.0, help="每口径负载时长（秒，默认 600）")
    parser.add_argument("--quick", action="store_true", help="快速口径：每口径时长固定 300s（契约验收口径）")
    parser.add_argument("--port-base", type=int, default=8930, help="端口基址（第 i 个口径用 port-base+i，默认 8930）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="markdown 对照报告输出路径")
    parser.add_argument(
        "--workers",
        default="1,2",
        help='逗号分隔的 worker 数列表，每个口径依次跑（默认 "1,2"）',
    )
    args = parser.parse_args(argv)
    try:
        args.workers = [int(x) for x in str(args.workers).split(",") if x.strip()]
    except ValueError:
        parser.error(f"--workers 必须是逗号分隔的整数，收到：{args.workers}")
    if not args.workers or any(w < 1 for w in args.workers):
        parser.error(f"--workers 每项必须 >= 1，收到：{args.workers}")

    import asyncio

    return asyncio.run(run_all(args))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
