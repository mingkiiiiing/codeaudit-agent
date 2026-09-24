"""长窗 soak 压测运行器（W12-A4，F9 服务端层归因）：10 分钟混合负载，真实 HTTP + 全程离线。

用法::

    python -m bench.stress.run_soak_long [--duration 600] [--quick] [--drain-sec 60] [--port 8947] [--out md路径]

与 bench.stress.run_soak.py（W10-A4）的差异（F9 归因需要更密的任务样本与更长窗口）：
  - 负载时长默认 600s（--quick 固定 300s），任务提交周期 6s（run_soak 为 8s）——
    quick 档即可取得 50 个提交样本、约 36-50 个终态任务，支撑"RSS-任务数"相关性判定；
  - 其余判据沿用三态口径：RSS 稳态斜率 < 1 MB/min → PASS；斜率超限但与累计终态任务数
    Pearson r >= 0.6 → DEFER（疑似每任务常数缓存，FIFO 兜底下有界）；否则 FAIL。
  - 读负载 2 Hz、SSE 每 30s 一拍、RSS 采样 10s 粒度、终态保留 5s 后 DELETE——全部与
    run_soak 同口径（直接 import 复用其部件，run_soak.py 本身零改动）。

W12 新增口径：
  - 离线金丝雀（独立判定项）：负载开始前提交 1 个探针任务并等待终态，三重断言：
    ① db config_json api_key ∈ {'', '<redacted>'}（W12-A1/F6 落库脱敏语义；出现其他
    非空值即明文 Key 落库违例）；② 终态报告 stats prompt/completion tokens 均为 0
    （服务端 FakeLLM 无脚本回放、用量恒 0；真实 GLM 调用必产生非零用量——注意
    FakeLLM 的 llm_calls 会正常计数，不能作为在线判据）；③ 服务子进程 CWD 无 .env
    文件（W12-A1/F7 的 from_env 自动加载 CWD/.env 不触发的结构性保障）；
  - 环境隔离：父进程与服务子进程双重 pop GLM_API_KEY/GLM_BASE_URL/GLM_MODEL；
    服务子进程以系统临时目录为工作目录（work_root=.codeaudit 相对 CWD 随之落临时目录，
    且该目录不存在 .env，F7 自动加载无从触发），显式注入 CODEAUDIT_DB_PATH 指向
    临时 db，全部临时产物 finally 清理。

W29 新增口径（卡 B：drain 排水等待）：
  - 接纳窗口关闭后，对仍未终态的被接纳任务追加排水等待：继续轮询至 drain 截止
    （每任务截止 = max(自身 240s 审计上限, 窗口关闭 + drain_sec)）；drain 内转终态计入
    drain_rescued（「排水收编」），drain 截止仍未终态计入 drain_still_running 并照旧按
    timeout 参与终态率拒绝——本机 CPU 负载高时末尾在途任务的「慢」不再被测量口径
    误判为「坏」（第九轮审计定性：固定窗口关闭立即统计对负载敏感）；
  - 终态率口径本身不变（仍 = (done+failed)/admitted），仅统计时点后移：drain 截止不早于
    各任务原上限，故只会改善 rate、不会恶化；退出码语义不变（终态率判定仍要求 rate >= 1.0）；
  - 参数：--drain-sec（CLI 优先）> 环境变量 CODEAUDIT_SOAK_DRAIN_SEC > 默认 60；
    0 = 关闭（等价既有行为）。排水归因见报告「排水归因」行（drain_split 纯函数可单测）。

结束判定（全部通过 → 退出码 0；任一失败 → 1）：
  1. 无 5xx；2. 被接纳任务终态率 100%；3. RSS 稳态斜率 < 1 MB/min（三态：PASS/DEFER/FAIL）；
  4. 结束时 /api/health total <= 50；5. 离线金丝雀三重断言通过。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from bench.stress.run_soak import (
    FIFO_CAPACITY,
    MIN_SLOPE_SAMPLES,
    RSS_SLOPE_LIMIT,
    RSS_TASK_CORR_DEFER,
    RSS_WARMUP_SEC,
    TERMINAL_GRACE_SEC,
    SoakStats,
    _do,
    _load_rss_probe,
    _read_loop,
    _rss_slope,
    _rss_task_corr,
    _sampler_loop,
    _sleep_until,
    _sse_loop,
    _terminal_rate,
    _zip_dir_bytes,
)
from bench.stress.run_stress_web import AUDIT_TIMEOUT_SEC, DATA_DIR, MINI_APP, _pctl

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = PROJECT_ROOT / "bench" / "results" / "soak_long_w12.md"

# 节拍常量：W12-A4 长窗口径（读/SSE/采样与 run_soak 一致，任务周期更密）
TASK_PERIOD_SEC = 6.0  # 任务提交周期（run_soak 为 8s；600s 窗 → 100 个提交样本）
READY_TIMEOUT_SEC = 60.0  # 服务就绪等待上限
POLL_INTERVAL = 0.25
TERMINAL_STATUSES = ("done", "failed")
DEFAULT_DRAIN_SEC = 60.0  # 排水等待默认秒数（W29 卡 B；0=关闭，等价既有行为）
DRAIN_ENV = "CODEAUDIT_SOAK_DRAIN_SEC"  # --drain-sec 的同名环境变量兜底（CLI 优先）


class _ServerHandleLong:
    """长窗 soak 自起服务：与 ServerHandle 同模式，但工作目录/数据库/日志全部落系统临时目录。

    - 子进程 cwd=临时目录：work_root（".codeaudit" 相对 CWD）与上传/工作副本随之隔离；
    - env 注入 CODEAUDIT_DB_PATH=临时 db（不触碰项目根存量 .codeaudit/audits.db）；
    - env 先行消毒：pop GLM_API_KEY/GLM_BASE_URL/GLM_MODEL 保证离线；
    - db_path 属性：临时 db 路径（离线金丝雀读取 config_json 用）。
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.proc: subprocess.Popen[bytes] | None = None
        self.tmp_dir: Path | None = None
        self._log_file = None

    @property
    def db_path(self) -> Path:
        """服务端 TaskStore 的临时 db 路径。"""
        if self.tmp_dir is None:
            raise RuntimeError("服务尚未启动")
        return self.tmp_dir / "audits.db"

    def start(self) -> None:
        """消毒环境 → 临时目录起服务子进程 → 轮询 /api/health 至就绪（60s 上限）。"""
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="soak_long_w12_"))
        env = os.environ.copy()
        for key in ("GLM_API_KEY", "GLM_BASE_URL", "GLM_MODEL"):
            env.pop(key, None)
        env["CODEAUDIT_DB_PATH"] = str(self.db_path)
        log_path = self.tmp_dir / "server.log"
        self._log_file = log_path.open("wb")
        self.proc = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "cli.py"), "serve", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(self.tmp_dir),
            env=env,
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + READY_TIMEOUT_SEC
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
        raise RuntimeError(f"服务 {READY_TIMEOUT_SEC:.0f}s 未就绪，日志：{log_path}")

    def stop(self) -> None:
        """停服务并清理全部临时产物（幂等）。"""
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        if self._log_file is not None and not self._log_file.closed:
            self._log_file.close()
        if self.tmp_dir is not None:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------- 负载协程（6s 周期提交）
async def _task_loop(
    client: httpx.AsyncClient,
    stats: SoakStats,
    stop: asyncio.Event,
    deadline: float,
    materials: list[tuple[str, bytes]],
    lifecycles: list["asyncio.Task[None]"],
    inflight: dict[str, None],
    drain_deadline: float | None = None,
    drain_events: list[tuple[float, float | None]] | None = None,
) -> None:
    """任务负载：每 6s 提交一个审计任务，mini_app 与 files=80 合成项目交替，直至负载期截止。

    提交/生命周期走本文件 _submit_one_drain（排水版）：与 run_soak._submit_one +
    _task_lifecycle 同口径（轮询至终态 + 保留 5s + DELETE），drain 关闭时行为完全一致。
    """
    t0 = time.monotonic()
    n = 0
    while not stop.is_set():
        if time.monotonic() >= deadline:
            break
        name, payload = materials[n % len(materials)]
        await _submit_one_drain(client, stats, name, payload, lifecycles, inflight, drain_deadline, drain_events)
        n += 1
        next_t = t0 + n * TASK_PERIOD_SEC
        if next_t >= deadline:
            break
        await _sleep_until(stop, next_t)


# ---------------------------------------------------------------- 排水等待（W29 卡 B）
def _resolve_drain_sec(cli_value: float | None, environ: dict[str, str] | None = None) -> float:
    """--drain-sec 解析：CLI 优先，缺省时读环境变量 CODEAUDIT_SOAK_DRAIN_SEC，再缺省 60。

    负值按 0（关闭）处理；env 配了非数字时回退默认 60（巡检自动化兜底优先于报错中断）。
    """
    env = os.environ if environ is None else environ
    raw = cli_value if cli_value is not None else env.get(DRAIN_ENV)
    if raw is None:
        return DEFAULT_DRAIN_SEC
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_DRAIN_SEC
    return max(0.0, value)


def _drain_split(
    waiting_deadlines: Sequence[float],
    drain_sec: float,
    finish_times: Sequence[float | None],
    window_close: float,
) -> tuple[int, int]:
    """窗口关闭时在途任务的排水归因二分（纯函数，便于单测）。

    入参两列等长（第 i 项描述同一个在途任务）：
      - waiting_deadlines[i]：该任务既有生命周期截止时刻（monotonic 秒，提交 + 240s 审计上限）；
      - finish_times[i]：实际转终态时刻；None 表示 drain 截止仍未终态。
    判定：
      - drain_sec <= 0：返回 (0, 0)，零排水归因（等价既有行为）；
      - drain 截止 = max(waiting_deadlines[i], window_close + drain_sec)：截止前转终态 →
        收编（drain_rescued），否则维持未终态（drain_still_running）。
    返回 (drain_rescued, drain_still_running)；终态率口径不在本函数内（仍按 SoakStats 聚合）。
    """
    if drain_sec <= 0:
        return 0, 0
    rescued = still_running = 0
    drain_at = window_close + drain_sec
    for cap, fin in zip(waiting_deadlines, finish_times, strict=True):
        if fin is None or fin > max(cap, drain_at):
            still_running += 1
        else:
            rescued += 1
    return rescued, still_running


async def _task_lifecycle_drain(
    client: httpx.AsyncClient,
    stats: SoakStats,
    audit_id: str,
    drain_deadline: float | None,
    drain_events: list[tuple[float, float | None]] | None,
) -> None:
    """单个被接纳任务的生命周期（排水版，W29 卡 B）。

    与 run_soak._task_lifecycle 同口径（轮询至终态 + 保留 TERMINAL_GRACE_SEC 后 DELETE，
    轮询间隔复用 POLL_INTERVAL），唯一差异：240s 审计上限到期仍未终态且 drain_deadline
    晚于该上限时，继续轮询至 drain 截止——
      - drain 截止前转终态：照常计 done/failed，并记 (原上限, 终态时刻) 供 drain_rescued 归因；
      - drain 截止仍未终态：照旧计 timeout（终态率照常拒绝），记 (原上限, None) 供
        drain_still_running 归因。
    drain_deadline=None（或不晚于原上限）时不追加等待、不记事件，行为与既有完全一致。
    """
    cap_deadline = time.monotonic() + AUDIT_TIMEOUT_SEC
    deadline = cap_deadline if drain_deadline is None else max(cap_deadline, drain_deadline)
    status = "timeout"
    while time.monotonic() < deadline:
        resp, _ms = await _do(client, stats, "GET", f"/api/audits/{audit_id}")
        if resp is not None and resp.status_code == 200:
            st = str(resp.json().get("status", ""))
            if st in TERMINAL_STATUSES:
                status = st
                break
        await asyncio.sleep(POLL_INTERVAL)
    finished_at = time.monotonic() if status != "timeout" else None
    if drain_events is not None and drain_deadline is not None and drain_deadline > cap_deadline:
        # 仅记录「原上限到期仍未终态」的任务：drain 内转终态为收编，截止仍未终态维持未终态
        if status == "timeout" or (finished_at is not None and finished_at > cap_deadline):
            drain_events.append((cap_deadline, finished_at))
    stats.terminals[status] = stats.terminals.get(status, 0) + 1
    await asyncio.sleep(TERMINAL_GRACE_SEC)
    await _do(client, stats, "DELETE", f"/api/audits/{audit_id}")


async def _submit_one_drain(
    client: httpx.AsyncClient,
    stats: SoakStats,
    name: str,
    payload: bytes,
    lifecycles: list["asyncio.Task[None]"],
    inflight: dict[str, None],
    drain_deadline: float | None,
    drain_events: list[tuple[float, float | None]] | None,
) -> None:
    """提交一个上传任务：与 run_soak._submit_one 同口径（200 接纳 / 429 记 denied / 其余记异常），
    唯以 _task_lifecycle_drain 派生生命周期（排水版）。"""
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
    lifecycles.append(
        asyncio.create_task(_task_lifecycle_drain(client, stats, audit_id, drain_deadline, drain_events))
    )
    print(f"[soak] task#{stats.submits} {name} -> admitted audit_id={audit_id}")


# ---------------------------------------------------------------- 离线金丝雀
def _db_api_key_state(db_path: Path, audit_id: str) -> str:
    """从临时 db 读指定任务 config_json 的 api_key 状态。

    返回 "ok"（''或 F6 脱敏占位 '<redacted>'——W12-A1 起 db 中恒为占位符）/
    "leak"（其他非空值 = 明文 Key 落库违例）/ "missing"（行不存在或坏 JSON）。
    只返回状态，绝不返回 Key 值本身（防泄漏进日志/报告）。
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT config_json FROM audits WHERE audit_id = ?", (audit_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return "missing"
    try:
        cfg = json.loads(str(row[0]))
    except ValueError:
        return "missing"
    return "ok" if str(cfg.get("api_key", "")) in ("", "<redacted>") else "leak"


def _fake_llm_signature(stats: dict[str, Any]) -> bool:
    """离线 FakeLLM 用量签名：服务端 FakeLLM 无脚本回放，全部响应 usage 为空 → tokens 恒 0。

    真实 GLM 调用 prompt/completion tokens 必然非零——非零即在线违例（F7 .env 自动加载
    等行为变化导致静默在线时的直接证据）。
    """
    try:
        return int(stats.get("prompt_tokens", -1)) == 0 and int(stats.get("completion_tokens", -1)) == 0
    except (TypeError, ValueError):
        return False


async def _offline_canary(client: httpx.AsyncClient, db_path: Path, payload: bytes, server_cwd: Path) -> tuple[int, int]:
    """离线金丝雀：提交 1 个 mini_app 任务，三重断言后 DELETE。

    断言（任一不满足即违例）：
      1. db config_json api_key ∈ {'', '<redacted>'}（F6 脱敏生效，无明文 Key 落库）；
      2. 终态报告 stats 的 prompt/completion tokens 均为 0（FakeLLM 零用量签名）；
      3. 服务子进程 CWD 无 .env 文件（F7 自动加载不触发的结构性保障）。
    返回 (检查数, 违例数)。
    """
    resp = await client.post(
        "/api/audits/upload",
        files={"file": ("canary_offline.zip", payload, "application/zip")},
        data={"do_fix": "false", "do_tests": "false"},
    )
    if resp.status_code != 200:
        return 0, 1  # 金丝雀无法落地按违例处理（暴露于判定，不静默）
    audit_id = str(resp.json().get("audit_id", ""))
    sig_ok = False
    deadline = time.monotonic() + AUDIT_TIMEOUT_SEC
    while time.monotonic() < deadline:
        got = await client.get(f"/api/audits/{audit_id}")
        if got.status_code == 200:
            body = got.json()
            if body.get("status") == "done":
                sig_ok = _fake_llm_signature(body.get("report", {}).get("stats", {}))
                break
            if body.get("status") == "failed":
                break
        await asyncio.sleep(POLL_INTERVAL)
    state = _db_api_key_state(db_path, audit_id)
    env_ok = not (server_cwd / ".env").exists()
    await client.delete(f"/api/audits/{audit_id}")
    bad = 0 if (sig_ok and state == "ok" and env_ok) else 1
    if bad == 0:
        print("[soak-long] 离线金丝雀：db api_key ok（F6 脱敏）+ FakeLLM 零用量签名 + 服务 CWD 无 .env")
    else:
        print(f"[soak-long] 离线金丝雀违例：fake_llm_sig={sig_ok} db_api_key={state} cwd_env_exists={not env_ok}")
    return 1, bad


# ---------------------------------------------------------------- 判定与报告
def _render_md(env: dict[str, str], stats: SoakStats, verdict: dict[str, Any]) -> str:
    """渲染 markdown 报告：环境头、摘要（含排水归因行，W29）、三态判定、RSS 时序、诊断、诚实边界。

    排水归因行：drain>0 时「排水：{drain_sec}s 内收编 N 任务，仍 M 在途」，drain 关闭或
    旧形状 verdict（无 drain 键）时「排水：关闭（--drain-sec=0，等价既有口径）」；
    既有行格式零变化（仅新增行）。
    """
    wall_load = verdict["wall_load"]
    slope, n_slope, slope_full = verdict["rss_slope"]
    rss_corr = verdict["rss_corr"]
    rate = verdict["terminal_rate"]
    drain_sec = verdict.get("drain_sec")
    drain_rescued = int(verdict.get("drain_rescued", 0))
    drain_still_running = int(verdict.get("drain_still_running", 0))
    drain_note = (
        f"排水：{drain_sec:.0f}s 内收编 {drain_rescued} 任务，仍 {drain_still_running} 在途"
        if drain_sec is not None and drain_sec > 0
        else "排水：关闭（--drain-sec=0，等价既有口径）"
    )
    lines = [
        "# 长窗 soak 压测报告（bench/stress/run_soak_long.py，W12-A4 / F9 服务端层归因）",
        "",
        "## 环境",
        "",
        f"- **生成时间**：{env['now']}",
        f"- **主机 / 平台**：{env['host']} / {env['platform']}",
        f"- **CPU**：{env['cpu']}",
        f"- **Python**：{env['python']}",
        f"- **被测服务**：server/app.py（FastAPI + uvicorn，**单进程单 worker**），base={env['base']}",
        f"- **压测口径**：{'quick（300s）' if env['quick'] == '1' else '标准'}，负载期目标 {env['duration']}s，"
        f"任务提交周期 {env['task_period']}s",
        "- **网络**：全程离线（父进程与服务子进程均清除 GLM_*；子进程注入 CODEAUDIT_DB_PATH 指向临时 db，"
        "工作目录为系统临时目录）；离线金丝雀（db api_key 脱敏 + FakeLLM 零用量 + 服务 CWD 无 .env）独立判定",
        "- **RSS 口径**：Windows psapi GetProcessMemoryInfo 工作集（服务子进程）",
        "- **与 run_soak（W10-A4）的关系**：判定口径完全一致（三态），任务周期 6s→样本更密、窗口 300/600s→更长；"
        "读负载/SSE/采样节拍不变",
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
        f"| 离线金丝雀（三重断言） | 检查 {verdict['canary_checked']}，违例 {verdict['canary_bad']} |",
        f"| RSS 首 / 末 | {verdict['rss_first']:.1f} / {verdict['rss_last']:.1f} MB |",
        f"| RSS 稳态斜率（判定，剔除 {RSS_WARMUP_SEC}s 预热） | {'-' if slope is None else f'{slope:.3f}'} MB/min"
        f"（{n_slope} 个稳态样本；全窗口参考 {'-' if slope_full is None else f'{slope_full:.3f}'}） |",
        f"| 结束时 /api/health total | {verdict['final_total']} |",
        f"| 排水归因 | {drain_note} |",
        "",
        "## 判定",
        "",
        "| 准则 | 结果 | 说明 |",
        "|---|---|---|",
    ]
    for name, result, note in verdict["checks"]:
        lines.append(f"| {name} | {result} | {note} |")
    lines.append(
        f"| **总判定** | **{verdict['overall']}** | 三态口径：斜率达标即 PASS；超限但与任务数强相关判 DEFER"
        "（疑似每任务常数缓存，FIFO 兜底下有界）；否则 FAIL |"
    )

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

    valid = [(s["t"], s["rss"]) for s in stats.samples if s["rss"] > 0]
    cum_done = stats.terminals["done"] + stats.terminals["failed"]
    per_task = (valid[-1][1] - valid[0][1]) / cum_done if cum_done > 0 and valid else 0.0
    diag = [
        f"- 全窗口斜率（参考口径）：{'-' if slope_full is None else f'{slope_full:.3f}'} MB/min；"
        f"稳态窗口斜率（判定口径，剔除前 {RSS_WARMUP_SEC}s 预热）：{'-' if slope is None else f'{slope:.3f}'} MB/min",
        f"- 稳态窗口 RSS 与累计终态任务数 Pearson 相关：{'-' if rss_corr is None else format(rss_corr, '.2f')}"
        f"（≥{RSS_TASK_CORR_DEFER} 判 DEFER：疑似每任务常数缓存；弱相关且超限判 FAIL：时间泄漏特征）",
        f"- RSS 总增量：{valid[-1][1] - valid[0][1]:.1f} MB（{valid[0][1]:.1f} → {valid[-1][1]:.1f}），"
        f"同期累计终态任务 {cum_done} 个，粗折算 ≈ {per_task * 1024:.0f} KB/任务"
        "（含预热抬升；精确每任务差分见 bench/results/serverdiff_w12.md）",
    ]
    lines += ["", "## RSS 诊断（F9 归因输入，参考）", ""] + diag
    lines += [
        "",
        "## 读延迟分位（客户端口径，ms）",
        "",
        "| 端点 | n | p50 | p95 | p99 |",
        "|---|---|---|---|---|",
    ]
    combined = stats.read_lat["health"] + stats.read_lat["list"]
    for key, vals in (
        ("GET /api/health", stats.read_lat["health"]),
        ("GET /api/audits?limit=5", stats.read_lat["list"]),
        ("合计", combined),
    ):
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
        "- 本报告针对当前真实形态（单进程单 worker + SQLite TaskStore + to_thread 线程执行）；在线 LLM 通道不在覆盖范围"
        "（离线金丝雀兜底监测静默在线）。",
        "- 全程离线：父进程与服务子进程启动前均清除 GLM_* 三键；server 建任务 config 来自 from_env 进程环境"
        "（F7 起自动加载 CWD/.env，服务子进程 CWD 为无 .env 的临时目录，自动加载无从触发）；"
        "金丝雀三重断言见脚本 docstring。注意 FakeLLM 路径 stats.llm_calls 会正常计数，不构成在线证据。",
        "- 窗口 300s/600s 对小时级慢泄漏灵敏度有限，斜率达标不外推为长期无泄漏；每任务差分归因由 memdiag 系探针承担。",
        "- 429 记为合法观测（F5 准入拒绝语义）。",
        "- 本场景任务终态即 DELETE，结束时 total 天然有界；FIFO 淘汰强校验由 bench/adversarial A3 覆盖。",
        "- RSS 为 Windows 工作集口径；非 Windows 取不到时斜率判定按约定跳过并如实标注。",
        "- 分位数为客户端口径（含 HTTP 栈往返）。",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程
async def run(port: int, out: Path, duration: float, quick: bool, drain_sec: float = DEFAULT_DRAIN_SEC) -> int:
    """长窗 soak 主流程：消毒 → 自起服务（临时目录隔离）→ 离线金丝雀 → 混合负载
    → 排空（含 drain 排水等待，W29 卡 B）→ 三态判定。"""
    # 全程离线：父进程先行消毒（服务子进程由 _ServerHandleLong 再消毒一次）
    for key in ("GLM_API_KEY", "GLM_BASE_URL", "GLM_MODEL"):
        os.environ.pop(key, None)

    # 素材：mini_app 内存 zip + files=80 合成项目 zip（生成幂等，落 bench/stress/data）
    from bench.stress.generator import generate_project

    source = generate_project(DATA_DIR / "soak_long_proj80", files=80, defect_ratio=0.02, seed=42)
    materials = [("mini_app", _zip_dir_bytes(MINI_APP)), ("proj80", _zip_dir_bytes(source))]
    print(f"[soak-long] 素材就绪：mini_app={len(materials[0][1]) // 1024}KB proj80={len(materials[1][1]) // 1024}KB")

    server = _ServerHandleLong(port)
    server.start()
    print(f"[soak-long] 服务就绪：{server.base}（duration={duration:.0f}s quick={quick}）")
    stats = SoakStats()
    rss_probe = _load_rss_probe()
    canary_checked = canary_bad = 0
    load_t0 = time.monotonic()
    try:
        limits = httpx.Limits(max_connections=64, max_keepalive_connections=32)
        timeout = httpx.Timeout(60.0, connect=10.0)
        async with httpx.AsyncClient(base_url=server.base, limits=limits, timeout=timeout) as client:
            # 离线金丝雀（负载前）：三重断言（db api_key ok + FakeLLM 零用量 + 服务 CWD 无 .env）
            canary_checked, canary_bad = await _offline_canary(client, server.db_path, materials[0][1], server.tmp_dir)

            stop = asyncio.Event()
            deadline = load_t0 + duration
            inflight: dict[str, None] = {}
            lifecycles: list["asyncio.Task[None]"] = []
            # 排水等待（W29 卡 B）：drain>0 时，窗口关闭后仍未终态的任务继续轮询至 drain 截止
            drain_deadline = deadline + drain_sec if drain_sec > 0 else None
            drain_events: list[tuple[float, float | None]] = []
            loops = [
                asyncio.create_task(_read_loop(client, stats, stop, "/api/health", "health")),
                asyncio.create_task(_read_loop(client, stats, stop, "/api/audits?limit=5", "list")),
                asyncio.create_task(
                    _task_loop(client, stats, stop, deadline, materials, lifecycles, inflight, drain_deadline, drain_events)
                ),
                asyncio.create_task(_sse_loop(client, stats, stop, deadline, inflight)),
                asyncio.create_task(_sampler_loop(client, stats, stop, rss_probe, server.proc, load_t0)),
            ]
            await asyncio.sleep(max(0.0, deadline - time.monotonic()))
            stop.set()
            for coro in await asyncio.gather(*loops, return_exceptions=True):
                if isinstance(coro, BaseException):
                    stats.loop_crashes.append(f"{type(coro).__name__}: {coro}")
            wall_load = time.monotonic() - load_t0

            # 排空：等待全部在途任务生命周期结束（各自 240s 上限；drain>0 时延长至 drain 截止）
            drain_t0 = time.monotonic()
            for res in await asyncio.gather(*lifecycles, return_exceptions=True):
                if isinstance(res, BaseException):
                    stats.loop_crashes.append(f"lifecycle {type(res).__name__}: {res}")
            drain_wall = time.monotonic() - drain_t0

            # 排水归因（纯函数二分，W29 卡 B）：收编/仍超时仅作报告归因，终态率口径不变
            drain_rescued, drain_still_running = _drain_split(
                [cap for cap, _fin in drain_events],
                drain_sec,
                [fin for _cap, fin in drain_events],
                deadline,
            )
            if drain_sec > 0:
                print(f"[soak-long] 排水：drain={drain_sec:.0f}s 收编 {drain_rescued} 任务，仍 {drain_still_running} 在途")

            # 结束时任务表规模快照（清理前取数）
            final_total = -1
            resp, _ms = await _do(client, stats, "GET", "/api/health")
            if resp is not None and resp.status_code == 200:
                final_total = int(resp.json().get("audits", {}).get("total", -1))

            # 兜底 DELETE（生命周期已删大半；404 属已清理）
            for audit_id in stats.created_ids:
                await _do(client, stats, "DELETE", f"/api/audits/{audit_id}")
    finally:
        server.stop()
    wall_total = time.monotonic() - load_t0

    # 判定（三态口径 + 离线金丝雀）
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
        checks.append(("RSS 斜率 < 1 MB/min", "SKIP", f"有效样本 {n_slope} < {MIN_SLOPE_SAMPLES}，跳过判定"))
    elif slope < RSS_SLOPE_LIMIT:
        checks.append(
            (
                "RSS 稳态斜率 < 1 MB/min（剔除预热）",
                "PASS",
                f"稳态斜率={slope:.3f} MB/min（{n_slope} 个 ≥{RSS_WARMUP_SEC}s 样本）；全窗口参考={slope_full:.3f}",
            )
        )
    elif rss_corr is not None and rss_corr >= RSS_TASK_CORR_DEFER:
        checks.append(
            (
                "RSS 稳态斜率 < 1 MB/min（剔除预热）",
                "DEFER",
                f"稳态斜率={slope:.3f} MB/min 超限，但与累计终态任务数 Pearson r={rss_corr:.2f}"
                f"（≥{RSS_TASK_CORR_DEFER}）→ 疑似每任务常数缓存；FIFO 容量兜底下有界",
            )
        )
    else:
        checks.append(
            (
                "RSS 稳态斜率 < 1 MB/min（剔除预热）",
                "FAIL",
                f"稳态斜率={slope:.3f} MB/min 超限且与任务数相关性弱"
                f"（r={'-' if rss_corr is None else f'{rss_corr:.2f}'}）→ 时间泄漏特征",
            )
        )
    if final_total < 0:
        checks.append(("结束时 total <= 50", "FAIL", "结束时 /api/health 快照失败"))
    else:
        checks.append(
            ("结束时 total <= 50", "PASS" if final_total <= FIFO_CAPACITY else "FAIL", f"total={final_total}（FIFO 容量 {FIFO_CAPACITY}）")
        )
    if canary_checked == 0:
        checks.append(("离线金丝雀（三重断言）", "SKIP", "金丝雀任务未落地"))
    else:
        checks.append(
            (
                "离线金丝雀（三重断言）",
                "PASS" if canary_bad == 0 else "FAIL",
                f"检查 {canary_checked}，违例 {canary_bad}"
                + ("（疑似静默在线或明文 Key 落库，见运行日志细分）" if canary_bad else ""),
            )
        )

    overall = "PASS" if all(r != "FAIL" for _n, r, _note in checks) else "FAIL"
    if overall == "PASS" and any(r == "DEFER" for _n, r, _note in checks):
        overall = "PASS（含 DEFER）"

    rss_vals = [s["rss"] for s in stats.samples if s["rss"] > 0]
    verdict: dict[str, Any] = {
        "wall_load": wall_load,
        "wall_total": wall_total,
        "drain_wall": drain_wall,
        "drain_sec": drain_sec,
        "drain_rescued": drain_rescued,
        "drain_still_running": drain_still_running,
        "terminal_rate": rate,
        "rss_slope": (slope, n_slope, slope_full),
        "rss_corr": rss_corr,
        "rss_first": rss_vals[0] if rss_vals else -1.0,
        "rss_last": rss_vals[-1] if rss_vals else -1.0,
        "final_total": final_total,
        "canary_checked": canary_checked,
        "canary_bad": canary_bad,
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
        "task_period": f"{TASK_PERIOD_SEC:.0f}",
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_render_md(env, stats, verdict), encoding="utf-8")

    print(f"[soak-long] 报告已写入：{out}")
    print(f"[soak-long] 判定：{overall}｜" + "；".join(f"{n}={r}" for n, r, _note in checks))
    return 0 if overall == "PASS" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="长窗 soak 压测（W12-A4，真实 HTTP，离线，三态判定）")
    parser.add_argument("--duration", type=float, default=600.0, help="负载时长（秒，默认 600）")
    parser.add_argument("--quick", action="store_true", help="长窗冒烟口径：时长固定 300s")
    parser.add_argument("--port", type=int, default=8947, help="自起服务端口（默认 8947）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="markdown 报告输出路径")
    parser.add_argument(
        "--drain-sec",
        type=float,
        default=None,
        help="接纳窗口关闭后的排水等待秒数（缺省读环境变量 CODEAUDIT_SOAK_DRAIN_SEC，再缺省 60；0=关闭）",
    )
    args = parser.parse_args(argv)
    duration = 300.0 if args.quick else (args.duration if args.duration > 0 else 600.0)
    drain_sec = _resolve_drain_sec(args.drain_sec)
    return asyncio.run(run(args.port, Path(args.out), duration, bool(args.quick), drain_sec))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
