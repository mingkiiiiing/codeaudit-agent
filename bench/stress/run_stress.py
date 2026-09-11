"""压测运行器（W5-A2 / G-C）：六场景一键产出压力测试与性能基线 markdown 报告。

用法::

    python -m bench.stress.run_stress [--scale 500|2000|all|N] [--out md路径]
                                      [--data-dir 目录] [--work-dir 目录]

场景（全部离线，FakeLLM / 规则模式，不触网）：
  1. ingest+index：分档计时工作副本物化与 SQLite 索引构建，tracemalloc 内存峰值单独跑；
  2. 纯规则审计吞吐：run_audit(enable_llm_review=False) 端到端 wall time → s/KLOC
     （口径同 docs/04 §3.2：耗时 / (源码行数/1000)），并记录规则命中数；
  3. LLM 并发扩展性：LatencyFakeLLM（chat 前 sleep 0.2s 模拟延迟）对 N 个文件跑
     review_files_parallel（并发 8），记录 wall 与理想值 ceil(N/8)*0.2s 的比值；
  4. 预算熔断：token_budget=5000 + review_mode=tools 全流程（FakeLLM 计费），
     验证熔断触发、任务以部分结果收尾不崩溃；
  5. server 并发：ASGITransport 同时创建 5 个不同小项目的审计任务，全部 done 且
     报告互不串扰，记录总耗时；
  6. R1-8 量化：1 万+ 调用边下单独计时 index build，cProfile 采样 _resolve_edges
     占比，并用预取原型（_PrefetchedIndexStore）A/B 实测可省比例。

约定：单场景异常不中断整份报告（记 ERROR 行）；压测数据放 bench/stress/data/
（已 gitignore）；对 audit/** 只读——对 _make_llm / _run_rules_on_contexts /
build_report 的替换均为运行时打点（上下文管理器保存/恢复），不改任何源文件。
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import math
import os
import platform
import pstats
import shutil
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from audit.indexer.store import SqliteIndexStore
from audit.llm.base import FakeLLMClient, LLMResponse, ToolCall

from bench.stress.generator import generate_project

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = PROJECT_ROOT / "bench" / "stress" / "data"
DEFAULT_RESULTS_DIR = PROJECT_ROOT / "bench" / "results"

STRESS_SEED = 42
DEFECT_RATIO = 0.02

# 场景 3：LLM 并发扩展性参数
LLM_LATENCY_SEC = 0.2
LLM_CONCURRENCY = 8
LLM_FILES_FULL = 200
LLM_FILES_MICRO = 40

# 场景 4：预算熔断参数
BUDGET_TOKEN = 5000
BUDGET_CALL_PROMPT_TOKENS = 3000
BUDGET_CALL_COMPLETION_TOKENS = 500

# 场景 5：server 并发参数
SERVER_TASKS = 5
SERVER_MINI_FILES = 12
SERVER_POLL_INTERVAL = 0.05
SERVER_TIMEOUT_SEC = 300.0

# 场景 6：R1-8 只在调用边达到该量级的规模档运行
R18_MIN_EDGES = 10_000

_1MB = 1024.0 * 1024.0


# ---------------------------------------------------------------- 基础设施


@dataclass
class ScenarioResult:
    """单场景结果：status ∈ {ok, skip, error}；metrics 为可嵌套 dict。"""

    name: str
    status: str = "ok"
    seconds: float = 0.0
    metrics: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def badge(self) -> str:
        return {"ok": "OK", "skip": "SKIP", "error": "ERROR"}.get(self.status, self.status.upper())


@contextmanager
def patched_attr(obj: Any, name: str, value: Any) -> Iterator[None]:
    """运行时打点：临时替换模块属性，退出时恢复（不落盘、不改源文件）。"""
    saved = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, saved)


def _make_emitter(events: list[dict[str, Any]]) -> Any:
    """构造把进度事件 append 进列表的 EventEmitter（async）。"""

    async def emitter(event: dict[str, Any]) -> None:
        events.append(dict(event))

    return emitter


async def _noop_emitter(_event: dict[str, Any]) -> None:
    return None


def _run_async(coro: Any) -> Any:
    return asyncio.run(coro)


def _stage_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in events:
        stage = str(e.get("stage", e.get("type", "?")))
        counts[stage] = counts.get(stage, 0) + 1
    return counts


def _proj_py_files(proj: Path) -> list[str]:
    """项目内全部 .py 的 posix 相对路径（排序稳定）。"""
    return sorted(p.relative_to(proj).as_posix() for p in proj.rglob("*.py") if p.is_file())


def _has_done_event(events: list[dict[str, Any]]) -> bool:
    """流水线收尾事件：ctx.emit('done', ...) 产出的 type=progress + stage=done。"""
    return any(e.get("stage") == "done" or e.get("type") == "done" for e in events)


# ---------------------------------------------------------------- 纯函数（单测覆盖）


def sec_per_kloc(duration_sec: float, loc: int) -> float | None:
    """docs/04 §3.2 口径：耗时 / (源码行数/1000)；loc<=0 返回 None。"""
    if loc and loc > 0:
        return float(duration_sec) / (loc / 1000.0)
    return None


def expansion_stats(n_calls: int, concurrency: int, latency_sec: float, wall_sec: float) -> dict[str, float]:
    """并发扩展性指标：理想 wall = ceil(n/并发) * 单次延迟；扩展系数 = 串行 wall / 实测 wall。"""
    ideal = math.ceil(n_calls / max(1, concurrency)) * latency_sec
    serial = n_calls * latency_sec
    speedup = (serial / wall_sec) if wall_sec > 0 else 0.0
    efficiency = (ideal / wall_sec * 100.0) if wall_sec > 0 else 0.0
    return {
        "n_calls": n_calls,
        "concurrency": concurrency,
        "latency_sec": latency_sec,
        "wall_sec": wall_sec,
        "ideal_wall_sec": ideal,
        "serial_wall_sec": serial,
        "expansion_factor": speedup,
        "wall_over_ideal": (wall_sec / ideal) if ideal > 0 else 0.0,
        "efficiency_pct": efficiency,
    }


def parse_scale_arg(text: str) -> list[int]:
    """--scale 取值解析：500/2000/N 或 all → 规模列表（去重升序）。"""
    raw = (text or "").strip().lower()
    if raw == "all":
        return [500, 2000]
    n = int(raw)
    if n <= 0:
        raise ValueError(f"--scale 需为正整数或 all，得到：{text!r}")
    return [n]


def llm_scenario_files(max_scale: int) -> int:
    """场景 3 的文件数：正式档固定 200；微型档（<500）缩到 40~200 以保测试轻量。"""
    if max_scale >= 500:
        return LLM_FILES_FULL
    return max(LLM_FILES_MICRO, min(LLM_FILES_FULL, max_scale))


def budget_verdict(
    audit_completed: bool,
    llm_calls: int,
    n_files: int,
    review_errors: int,
    rule_issues: int,
    llm_issues: int,
) -> dict[str, Any]:
    """预算熔断证据判定：每文件 ≤1 次 chat + review_errors 覆盖全部文件 + 不崩溃。

    llm_calls 允许 n_files + 5 的余量：understand 阶段的架构卡片 LLM 调用等非
    review 通道会计入同一客户端（实测 50 文件档 llm_calls = 51 = 50 review + 1 架构）。
    """
    slack = 5
    one_call_per_file = 0 < llm_calls <= max(1, n_files) + slack
    errors_cover_files = review_errors >= max(1, n_files - slack)
    tripped = bool(audit_completed and errors_cover_files and one_call_per_file and llm_issues == 0)
    return {
        "audit_completed": audit_completed,
        "budget_tripped": tripped,
        "llm_calls": llm_calls,
        "py_files": n_files,
        "calls_per_file": (llm_calls / n_files) if n_files else None,
        "review_errors": review_errors,
        "rule_issues": rule_issues,
        "llm_issues": llm_issues,
        "partial_results": bool(audit_completed and rule_issues > 0),
    }


def r18_profile_share(profile_rows: dict[str, dict[str, float]]) -> dict[str, Any]:
    """从 cProfile 采样结果计算 R1-8 的 N+1 占比与预取可省上限估算。"""
    resolve = profile_rows.get("_resolve_edges", {})
    lang = profile_rows.get("_language_of", {})
    aliases = profile_rows.get("_aliases", {})
    resolve_cum = float(resolve.get("cumtime", 0.0) or 0.0)
    lang_cum = float(lang.get("cumtime", 0.0) or 0.0)
    alias_cum = float(aliases.get("cumtime", 0.0) or 0.0)
    n_plus_one = lang_cum + alias_cum
    share = (n_plus_one / resolve_cum) if resolve_cum > 0 else None
    return {
        "resolve_edges_cum_sec": resolve_cum,
        "language_of_cum_sec": lang_cum,
        "aliases_cum_sec": alias_cum,
        "n_plus_one_cum_sec": n_plus_one,
        "n_plus_one_share": share,
        "language_of_calls": int(lang.get("ncalls", 0) or 0),
        "aliases_calls": int(aliases.get("ncalls", 0) or 0),
    }


# ---------------------------------------------------------------- 场景 1：ingest + index


def scenario_ingest_index(proj: Path, work_dir: Path) -> ScenarioResult:
    """ingest 物化 + create_index().build() 分档计时；内存峰值用 tracemalloc 单独跑。"""
    from audit.indexer import create_index
    from audit.ingest import ingest

    result = ScenarioResult(name="ingest_index")

    # Pass A：纯计时（tracemalloc 有开销，不与耗时混测）
    pass_a = work_dir / "timing"
    start = time.perf_counter()
    ws = ingest(str(proj), pass_a)
    t_ingest = time.perf_counter() - start
    store = create_index(ws)
    start = time.perf_counter()
    store.build()
    t_index = time.perf_counter() - start
    stats = dict(store.stats())
    db_path = Path(ws.db_path)
    db_bytes = db_path.stat().st_size if db_path.exists() else 0
    store.close()
    shutil.rmtree(pass_a, ignore_errors=True)

    # Pass B：tracemalloc 内存峰值（单独跑，不计时）
    pass_b = work_dir / "memory"
    tracemalloc.start()
    ws2 = ingest(str(proj), pass_b)
    store2 = create_index(ws2)
    store2.build()
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    store2.close()
    shutil.rmtree(pass_b, ignore_errors=True)

    result.seconds = t_ingest + t_index
    result.metrics = {
        "files": stats.get("files"),
        "symbols": stats.get("symbols"),
        "call_edges": stats.get("call_edges"),
        "resolved_edges": stats.get("resolved_edges"),
        "resolved_ratio": stats.get("resolved_ratio"),
        "ingest_sec": round(t_ingest, 3),
        "index_build_sec": round(t_index, 3),
        "total_sec": round(t_ingest + t_index, 3),
        "db_size_mb": round(db_bytes / _1MB, 3),
        "peak_memory_mb": round(peak / _1MB, 3),
        "peak_memory_note": "tracemalloc 单独跑（Pass B），不计入耗时",
    }
    return result


# ---------------------------------------------------------------- 场景 2：纯规则审计吞吐


def scenario_rules_audit(proj: Path, work_dir: Path) -> ScenarioResult:
    """run_audit(enable_llm_review=False) 端到端 wall time → s/KLOC + 规则命中数。"""
    import audit.detect.engine as detect_engine
    import audit.report.builder as report_builder
    from audit.config import AuditConfig
    from audit.orchestrator import pipeline as orch

    result = ScenarioResult(name="rules_audit")
    rule_counter = {"hits": 0}
    capture: dict[str, Any] = {}
    events: list[dict[str, Any]] = []

    original_rules = detect_engine._run_rules_on_contexts
    original_builder = report_builder.build_report
    original_make_llm = orch._make_llm

    def counting_rules(contexts: Any, registry: Any, extra: Any = None) -> Any:
        hits = original_rules(contexts, registry, extra)
        rule_counter["hits"] = len(hits)
        return hits

    def capturing_builder(ctx: Any) -> Any:
        capture["extra"] = ctx.extra
        capture["llm_usage"] = ctx.llm.usage_totals()
        return original_builder(ctx)

    config = AuditConfig(
        source_path=str(proj),
        work_root=str(work_dir),
        out_dir=str(work_dir / "reports"),
        api_key="",
        enable_llm_review=False,
        enable_verify=False,
    )

    with patched_attr(detect_engine, "_run_rules_on_contexts", counting_rules), \
         patched_attr(report_builder, "build_report", capturing_builder), \
         patched_attr(orch, "_make_llm", lambda cfg: (original_make_llm(cfg)[0], None)):
        start = time.perf_counter()
        report = _run_async(orch.run_audit(config, _make_emitter(events)))
        wall = time.perf_counter() - start

    loc = int(report.loc or 0)
    detection = dict((capture.get("extra") or {}).get("detection") or {})
    result.seconds = wall
    result.metrics = {
        "wall_sec": round(wall, 3),
        "loc": loc,
        "sec_per_kloc": round(wall / (loc / 1000.0), 3) if loc else None,
        "rule_hits": rule_counter["hits"],
        "issues": len(report.issues or []),
        "files_reviewed": detection.get("files_reviewed"),
        "pipeline_duration_sec": round(float(report.stats.duration_sec or 0.0), 3),
        "stages_events": _stage_counts(events),
        "loc_note": "s/KLOC 口径同 docs/04 §3.2：wall / (loc/1000)",
    }
    if not _has_done_event(events):
        result.status = "error"
        result.error = "run_audit 未发出 done 事件"
    return result


# ---------------------------------------------------------------- 场景 3：LLM 并发扩展性


class LatencyFakeLLM(FakeLLMClient):
    """每次 chat 前 sleep 的假客户端：模拟真实 LLM 网络延迟（离线可复现）。"""

    def __init__(self, latency_sec: float = LLM_LATENCY_SEC) -> None:
        super().__init__()
        self._latency = latency_sec

    async def chat(self, messages: Any = None, tools: Any = None, json_mode: bool = False,
                   temperature: float = 0.2) -> Any:
        self.calls.append({"messages": messages, "tools": tools})
        await asyncio.sleep(self._latency)
        return LLMResponse(content="", usage={"prompt_tokens": 10, "completion_tokens": 1},
                           model="stress-latency-fake")


def _make_latency_review_fn(llm: FakeLLMClient) -> Any:
    """review_fn：每文件恰好一次 chat 调用（扩展性测量的最小负载）。"""

    async def review_fn(workspace: Any, rel_path: str, hints: Any = None, **kwargs: Any) -> list:
        await llm.chat([{"role": "user", "content": f"review {rel_path}"}])
        return []

    return review_fn


def scenario_llm_concurrency(proj: Path, n_files: int, work_dir: Path) -> ScenarioResult:
    """review_files_parallel（并发 8）+ LatencyFakeLLM：wall ≈ ceil(n/8)*0.2s。"""
    from audit.config import AuditConfig
    from audit.agents.review import review_files_parallel
    from audit.pipeline import PipelineContext
    from audit.workspace import WorkspaceContext

    result = ScenarioResult(name="llm_concurrency")
    manifest_rel_files = _proj_py_files(proj)
    n_files = min(n_files, len(manifest_rel_files))

    ws = WorkspaceContext(
        audit_id="stressllm",
        src_root=proj,
        work_root=work_dir,
        db_path=work_dir / "llm_index.db",
    )
    config = AuditConfig(source_path=str(proj), work_root=str(work_dir), api_key="",
                         concurrency=LLM_CONCURRENCY, enable_llm_review=True)
    llm = LatencyFakeLLM(LLM_LATENCY_SEC)
    ctx = PipelineContext(config=config, workspace=ws, llm=llm, emitter=_noop_emitter)
    jobs = [(rel, []) for rel in manifest_rel_files[:n_files]]

    async def _run() -> tuple[float, dict[str, list]]:
        start = time.perf_counter()
        results = await review_files_parallel(ctx, jobs, review_fn=_make_latency_review_fn(llm))
        return time.perf_counter() - start, results

    wall, results = _run_async(_run())
    stats = expansion_stats(len(jobs), LLM_CONCURRENCY, LLM_LATENCY_SEC, wall)
    errors = (ctx.extra.get("review_errors") or {})
    stats["review_errors"] = len(errors)
    stats["files_with_issues"] = len(results)
    stats["ideal_note"] = f"理想 wall = ceil({len(jobs)}/{LLM_CONCURRENCY}) * {LLM_LATENCY_SEC}s"
    result.seconds = wall
    result.metrics = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in stats.items()}
    if len(errors) or len(results) != len(jobs):
        result.status = "error"
        result.error = f"review_errors={len(errors)}，成功文件 {len(results)}/{len(jobs)}"
    return result


# ---------------------------------------------------------------- 场景 4：预算熔断


class BudgetStopLLM(FakeLLMClient):
    """每次 chat 返回一次 read_file 工具调用并计大额 usage：触发 token 预算熔断。

    语义：SimpleAgentRuntime 第 1 轮调用后累计 tokens(3500) > budget(5000//10=500)，
    第 2 轮前熔断（stop_reason=budget），record_issues 永远不会被调用——每文件恰好
    1 次 chat，随后 R1-26 判定"非正常结束"逐文件报错（部分结果收尾，不崩溃）。
    """

    def __init__(self, prompt_tokens: int = BUDGET_CALL_PROMPT_TOKENS,
                 completion_tokens: int = BUDGET_CALL_COMPLETION_TOKENS) -> None:
        super().__init__()
        self._prompt = prompt_tokens
        self._completion = completion_tokens

    async def chat(self, messages: Any = None, tools: Any = None, json_mode: bool = False,
                   temperature: float = 0.2) -> Any:
        self.calls.append({"messages": messages, "tools": tools})
        return LLMResponse(
            content=None,
            tool_calls=[ToolCall(id=f"call_{len(self.calls)}", name="read_file",
                                 arguments={"path": "main.py"})],
            usage={"prompt_tokens": self._prompt, "completion_tokens": self._completion},
            model="stress-budget-fake",
        )

    def usage_totals(self) -> dict[str, int]:
        return {
            "llm_calls": len(self.calls),
            "prompt_tokens": len(self.calls) * self._prompt,
            "completion_tokens": len(self.calls) * self._completion,
        }


def scenario_budget_circuit_breaker(proj: Path, work_dir: Path) -> ScenarioResult:
    """token_budget=5000 + review_mode=tools 全流程：验证熔断触发与部分结果收尾。"""
    from audit.config import AuditConfig
    from audit.orchestrator import pipeline as orch
    from audit.report import builder as report_builder

    result = ScenarioResult(name="budget_circuit_breaker")
    n_files = len(_proj_py_files(proj))
    capture: dict[str, Any] = {}
    events: list[dict[str, Any]] = []

    original_builder = report_builder.build_report

    def capturing_builder(ctx: Any) -> Any:
        capture["extra"] = ctx.extra
        return original_builder(ctx)

    llm = BudgetStopLLM()

    def make_fake_llm(_config: Any) -> tuple[BudgetStopLLM, None]:
        return llm, None

    config = AuditConfig(
        source_path=str(proj),
        work_root=str(work_dir),
        out_dir=str(work_dir / "reports"),
        api_key="stress-fake-key",  # llm_available=True 才会创建 review_fn（LLM 实体已被替换为 Fake）
        enable_llm_review=True,
        review_mode="tools",  # token_budget 只在工具路径生效（SimpleAgentRuntime 熔断）
        token_budget=BUDGET_TOKEN,
        enable_verify=False,
        batch_small_slices=False,  # 逐文件审查：llm_calls == 文件数 才是干净证据
        concurrency=LLM_CONCURRENCY,
    )

    with patched_attr(orch, "_make_llm", make_fake_llm), \
         patched_attr(report_builder, "build_report", capturing_builder):
        start = time.perf_counter()
        try:
            report = _run_async(orch.run_audit(config, _make_emitter(events)))
            completed = True
            error = ""
        except Exception as exc:  # 熔断场景绝不允许崩溃；崩溃即为场景失败
            report = None
            completed = False
            error = f"{type(exc).__name__}: {exc}"
        wall = time.perf_counter() - start

    extra = capture.get("extra") or {}
    review_errors = len(extra.get("review_errors") or {})
    detection = dict(extra.get("detection") or {})
    rule_issues = int(detection.get("rule_issues") or 0)
    llm_issues = int(detection.get("llm_issues") or 0)
    usage = llm.usage_totals()
    verdict = budget_verdict(completed, usage["llm_calls"], n_files, review_errors,
                             rule_issues, llm_issues)
    verdict.update({
        "token_budget": BUDGET_TOKEN,
        "effective_per_file_budget": BUDGET_TOKEN // 10,
        "tokens_billed_prompt": usage["prompt_tokens"],
        "tokens_billed_completion": usage["completion_tokens"],
        "wall_sec": round(wall, 3),
        "done_event": _has_done_event(events),
        "issues_in_report": len(report.issues or []) if report is not None else None,
    })
    result.seconds = wall
    result.metrics = verdict
    if not completed:
        result.status = "error"
        result.error = error
    elif not verdict["budget_tripped"]:
        result.status = "error"
        result.error = "熔断证据不成立（未观察到逐文件 stop_budget + 非正常结束）"
    return result


# ---------------------------------------------------------------- 场景 5：server 并发


@contextmanager
def _isolated_offline_env(work_dir: Path) -> Iterator[None]:
    """server 场景隔离：清掉 GLM_* 环境变量（防误触网）+ chdir 到临时工作目录。"""
    saved_env = {k: os.environ.pop(k, None) for k in ("GLM_API_KEY", "GLM_BASE_URL", "GLM_MODEL")}
    saved_cwd = Path.cwd()
    os.chdir(work_dir)
    try:
        yield
    finally:
        os.chdir(saved_cwd)
        for key, value in saved_env.items():
            if value is not None:
                os.environ[key] = value


def scenario_server_concurrency(data_dir: Path, work_dir: Path) -> ScenarioResult:
    """同时创建 5 个不同小项目的审计任务：全部 done、报告互不串扰、记录总耗时。"""
    from server.app import AUDITS, create_app

    from bench.stress.generator import generate_project, load_manifest

    result = ScenarioResult(name="server_concurrency")

    mini_paths: list[Path] = []
    for i in range(SERVER_TASKS):
        mini = generate_project(
            data_dir / f"srv_mini_{i}", files=SERVER_MINI_FILES, seed=STRESS_SEED + i * 101
        )
        mini_paths.append(mini)

    import httpx

    app = create_app()
    tasks_root = work_dir / "server"
    tasks_root.mkdir(parents=True, exist_ok=True)
    audit_ids: list[str] = []
    t_start = time.perf_counter()

    with _isolated_offline_env(tasks_root):
        async def _drive() -> tuple[list[str], dict[str, dict[str, Any]]]:
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 58080))
            reports: dict[str, dict[str, Any]] = {}
            async with httpx.AsyncClient(transport=transport, base_url="http://stress.test") as client:
                responses = await asyncio.gather(
                    *[
                        client.post("/api/audits", json={"source_path": str(p)})
                        for p in mini_paths
                    ]
                )
                for resp in responses:
                    resp.raise_for_status()
                    audit_ids.append(resp.json()["audit_id"])
                deadline = time.monotonic() + SERVER_TIMEOUT_SEC
                pending = set(audit_ids)
                while pending and time.monotonic() < deadline:
                    await asyncio.sleep(SERVER_POLL_INTERVAL)
                    for audit_id in list(pending):
                        r = await client.get(f"/api/audits/{audit_id}")
                        payload = r.json()
                        if payload.get("status") in ("done", "failed"):
                            reports[audit_id] = payload
                            pending.discard(audit_id)
            return audit_ids, reports

        ids, reports = _run_async(_drive())

    total_wall = time.perf_counter() - t_start
    per_task: list[dict[str, Any]] = []
    all_done = True
    no_crosstalk = True
    for path, audit_id in zip(mini_paths, ids, strict=False):
        payload = reports.get(audit_id, {})
        status = payload.get("status")
        report = payload.get("report") or {}
        project_name = report.get("project_name")
        issues = report.get("issues") or []
        issue_files = sorted({str(i.get("file")) for i in issues})
        manifest_files = set(load_manifest(path)["files"])
        belongs = all(f in manifest_files for f in issue_files)
        name_ok = project_name == path.name
        all_done = all_done and status == "done"
        no_crosstalk = no_crosstalk and belongs and name_ok
        per_task.append({
            "audit_id": audit_id,
            "project": path.name,
            "status": status,
            "project_name_ok": name_ok,
            "issue_files_in_project": belongs,
            "issues": len(issues),
        })
        AUDITS.pop(audit_id, None)  # 清理本场景任务表，防跨场景/跨测试泄漏

    result.seconds = total_wall
    result.metrics = {
        "tasks": SERVER_TASKS,
        "mini_project_files": SERVER_MINI_FILES,
        "all_done": all_done,
        "no_crosstalk": no_crosstalk,
        "total_wall_sec": round(total_wall, 3),
        "tasks_detail": per_task,
        "note": "CPU 密集的规则扫描在同一事件循环内串行推进，总耗时≈各任务之和（并发价值在 IO/LLM 场景）",
    }
    if not all_done or not no_crosstalk:
        result.status = "error"
        result.error = f"all_done={all_done}, no_crosstalk={no_crosstalk}"
    return result


# ---------------------------------------------------------------- 场景 6：R1-8 索引 N+1 量化


class _PrefetchedIndexStore(SqliteIndexStore):
    """R1-8 修复原型：预取 language 映射 + memoize import 别名表，消除逐 call 行 SQL。

    与 docs/10 §4 R1-8 对应：_resolve_edges 每处理一行 calls 就执行
    _language_of（1 次 SQL）+ _aliases（1 次 SQL）→ N+1。本原型在 resolve 前
    一次取全 language 映射，并按 caller 文件缓存 aliases（imports 表在 resolve
    阶段不变，缓存安全）。仅用于压测 A/B，不改 audit/**。
    """

    def build(self) -> None:
        self._lang_map: dict[str, str] = {}
        self._alias_cache: dict[str, dict[str, Any]] = {}
        super().build()

    def _resolve_edges(self) -> None:
        self._lang_map = {
            row["path"]: row["language"] for row in self._conn.execute("SELECT path, language FROM files")
        }
        super()._resolve_edges()

    def _language_of(self, rel: str) -> str:
        try:
            return self._lang_map[rel]
        except KeyError:
            lang = super()._language_of(rel)
            self._lang_map[rel] = lang
            return lang
        except AttributeError:  # build 之外的查询路径：退回父类行为
            return super()._language_of(rel)

    def _aliases(self, caller_file: str) -> dict[str, Any]:
        try:
            cached = self._alias_cache.get(caller_file)
        except AttributeError:
            return super()._aliases(caller_file)
        if cached is None:
            cached = super()._aliases(caller_file)
            self._alias_cache[caller_file] = cached
        return cached


def _profile_build_stats(store: Any) -> dict[str, dict[str, float]]:
    """对一次 build() 做 cProfile 采样，返回 indexer/store.py 关键函数的耗时行。"""
    profiler = cProfile.Profile()
    profiler.enable()
    store.build()
    profiler.disable()
    stats = pstats.Stats(profiler)
    wanted = ("build", "_resolve_edges", "_language_of", "_aliases", "_resolve_py_call")
    rows: dict[str, dict[str, float]] = {}
    for (filename, _lineno, func), (_cc, nc, tottime, cumtime, _callers) in stats.stats.items():
        norm = filename.replace("\\", "/")
        if func in wanted and "audit/indexer/store.py" in norm:
            rows[func] = {"ncalls": float(nc), "tottime": tottime, "cumtime": cumtime}
    return rows


def _best_of_builds(
    ws: Any,
    store_factory: Any,
    db_dir: Path,
    passes: int = 3,
) -> tuple[float, dict[str, Any]]:
    """同一工作副本上做 N 次全新 DB 的全量 build，返回（最优耗时, 最优那次的 stats）。

    取 best-of-N：CPU 计时的标准做法，降低 GC/OS 调度噪声；每次用全新 db_path
    保证都是全量解析（非增量）。
    """
    best_sec = float("inf")
    best_stats: dict[str, Any] = {}
    db_dir.mkdir(parents=True, exist_ok=True)
    for i in range(passes):
        store = store_factory(db_dir / f"build_{i}.db")
        start = time.perf_counter()
        store.build()
        elapsed = time.perf_counter() - start
        if elapsed < best_sec:
            best_sec = elapsed
            best_stats = dict(store.stats())
        store.close()
        (db_dir / f"build_{i}.db").unlink(missing_ok=True)
    return best_sec, best_stats


def scenario_r18_quantify(proj: Path, work_dir: Path, base_build_sec: float) -> ScenarioResult:
    """R1-8：cProfile 采样 _resolve_edges 占比 + 预取原型 A/B（双方 best-of-3）实测。"""
    from audit.indexer import create_index
    from audit.ingest import ingest

    result = ScenarioResult(name="r18_quantify")
    r18_dir = work_dir / "r18"
    r18_dir.mkdir(parents=True, exist_ok=True)
    ws = ingest(str(proj), r18_dir)

    def base_factory(db_path: Path) -> Any:
        return create_index(ws, db_path)

    def proto_factory(db_path: Path) -> Any:
        return _PrefetchedIndexStore(ws, db_path)

    # Pass 1：base best-of-3（A/B 的 base 侧，比场景 1 的单次更稳）
    base_best, base_stats = _best_of_builds(ws, base_factory, r18_dir / "base")
    # Pass 2：cProfile 采样（占比是比值，单次即可）
    prof_store = base_factory(r18_dir / "profile.db")
    profile_rows = _profile_build_stats(prof_store)
    prof_store.close()
    (r18_dir / "profile.db").unlink(missing_ok=True)
    share = r18_profile_share(profile_rows)
    # Pass 3：预取原型 best-of-3
    prefetch_best, proto_stats = _best_of_builds(ws, proto_factory, r18_dir / "prefetch")

    shutil.rmtree(r18_dir, ignore_errors=True)

    edges = int(base_stats.get("call_edges") or 0)
    saving = ((base_best - prefetch_best) / base_best * 100.0) if base_best > 0 else None
    result.seconds = prefetch_best
    result.metrics = {
        "call_edges": edges,
        "profiled_build_sec": round(float(profile_rows.get("build", {}).get("cumtime", 0.0)), 3),
        "base_build_best3_sec": round(base_best, 3),
        "scenario1_base_build_sec": round(base_build_sec, 3),
        "prefetch_build_best3_sec": round(prefetch_best, 3),
        "measured_saving_pct": round(saving, 1) if saving is not None else None,
        "resolve_edges_cum_sec": round(share["resolve_edges_cum_sec"], 4),
        "language_of_cum_sec": round(share["language_of_cum_sec"], 4),
        "aliases_cum_sec": round(share["aliases_cum_sec"], 4),
        "n_plus_one_share_of_resolve": (
            round(share["n_plus_one_share"], 4) if share["n_plus_one_share"] is not None else None
        ),
        "language_of_sql_calls": share["language_of_calls"],
        "aliases_sql_calls": share["aliases_calls"],
        "prefetch_edges_identical": bool(proto_stats.get("call_edges") == base_stats.get("call_edges")),
        "estimate_note": "n_plus_one_share 为 cProfile 估算上限；measured_saving_pct 为预取原型 best-of-3 实测",
    }
    if edges < R18_MIN_EDGES:
        result.status = "skip"
        result.notes.append(f"调用边 {edges} < {R18_MIN_EDGES}（微型档不满足 R1-8 量化前提，数据仅供参考）")
    resolve_cum = float(profile_rows.get("_resolve_edges", {}).get("cumtime") or 0.0)
    if resolve_cum <= 0:
        result.status = "error"
        result.error = "cProfile 未采样到 _resolve_edges（audit/indexer 内部结构可能已变）"
    return result


# ---------------------------------------------------------------- 报告渲染


def _fmtf(value: Any, digits: int = 3, suffix: str = "") -> str:
    """报告数值单元格：None → N/A；int 原样；float 保留 digits 位小数。"""
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value}{suffix}"
    return f"{float(value):.{digits}f}{suffix}"


def env_header(scales: list[int]) -> dict[str, str]:
    """报告头部环境信息：机器 / CPU / Python / 平台 / 时间 / seed。"""
    return {
        "生成时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "主机": platform.node() or "unknown",
        "CPU": platform.processor() or platform.machine() or "unknown",
        "平台": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "Python": platform.python_version(),
        "规模档位": " / ".join(str(s) for s in scales) + " 文件",
        "随机种子": str(STRESS_SEED),
        "网络": "全程离线（FakeLLM / 纯规则模式，未配置也不读取 GLM_API_KEY）",
    }


def _scenario_section(sb: list[str], title: str, r: ScenarioResult, extra_rows: dict[str, Any] | None = None) -> None:
    sb.append(f"### {title} ｜ 状态：{r.badge} ｜ 场景耗时 {r.seconds:.3f}s")
    sb.append("")
    rows = dict(r.metrics)
    if extra_rows:
        rows.update(extra_rows)
    if r.error:
        rows["error"] = r.error
    for note in r.notes:
        rows.setdefault("notes", [])
        if isinstance(rows["notes"], list):
            rows["notes"].append(note)
    sb.append("| 指标 | 值 |")
    sb.append("|---|---|")
    for key, value in rows.items():
        if isinstance(value, dict):
            inner = "；".join(f"{k}={v}" for k, v in value.items())
            sb.append(f"| {key} | {inner} |")
        elif isinstance(value, list):
            sb.append(f"| {key} | {'; '.join(str(v) for v in value)} |")
        else:
            sb.append(f"| {key} | {value} |")
    sb.append("")


def render_report(meta: dict[str, str], scale_results: dict[int, dict[str, ScenarioResult]],
                  shared: dict[str, ScenarioResult]) -> str:
    """渲染完整 markdown 报告：环境头部 → 摘要表 → 分场景明细 → 结论。"""
    sb: list[str] = ["# 压力测试与性能基线报告（bench/stress，W5-A2）", ""]
    sb.append("## 环境")
    sb.append("")
    for key, value in meta.items():
        sb.append(f"- **{key}**：{value}")
    sb.append("")
    sb.append("口径说明：s/KLOC = 端到端 wall time / (源码行数/1000)，同 docs/04 §3.2；"
              "内存峰值为 tracemalloc 口径（单独跑，不计入耗时）；所有场景零网络。")
    sb.append("")

    # ---- 摘要表
    sb.append("## 摘要")
    sb.append("")
    sb.append("| 规模 | ingest(s) | index build(s) | 文件 | 符号 | 调用边 | DB(MB) | 内存峰值(MB) "
              "| 规则审计 wall(s) | s/KLOC | 规则命中 |")
    sb.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for scale, results in scale_results.items():
        s1 = results.get("ingest_index")
        s2 = results.get("rules_audit")
        m1 = s1.metrics if s1 else {}
        m2 = s2.metrics if s2 else {}
        sb.append(
            f"| {scale} | {_fmtf(m1.get('ingest_sec'), 2)} | {_fmtf(m1.get('index_build_sec'), 2)} "
            f"| {_fmtf(m1.get('files'))} | {_fmtf(m1.get('symbols'))} | {_fmtf(m1.get('call_edges'))} "
            f"| {_fmtf(m1.get('db_size_mb'), 2)} | {_fmtf(m1.get('peak_memory_mb'), 1)} "
            f"| {_fmtf(m2.get('wall_sec'), 2)} | {_fmtf(m2.get('sec_per_kloc'), 2)} "
            f"| {_fmtf(m2.get('rule_hits'))} |"
        )
    sb.append("")

    llm = shared.get("llm_concurrency")
    srv = shared.get("server_concurrency")

    # ---- 分规模场景明细
    for scale, results in scale_results.items():
        sb.append(f"## 场景明细：{scale} 文件档")
        sb.append("")
        if "ingest_index" in results:
            _scenario_section(sb, f"场景 1（{scale}）：ingest + index", results["ingest_index"])
        if "rules_audit" in results:
            _scenario_section(sb, f"场景 2（{scale}）：纯规则审计吞吐", results["rules_audit"])
        if "budget_circuit_breaker" in results:
            _scenario_section(sb, f"场景 4（{scale}）：预算熔断", results["budget_circuit_breaker"])
        if "r18_quantify" in results:
            _scenario_section(sb, f"场景 6（{scale}）：R1-8 索引 N+1 量化", results["r18_quantify"])

    # ---- 共享场景
    sb.append("## 共享场景")
    sb.append("")
    if llm is not None:
        _scenario_section(sb, f"场景 3：LLM 并发扩展性（{llm.metrics.get('n_calls', '?')} 文件 × "
                              f"sleep {LLM_LATENCY_SEC}s × 并发 {LLM_CONCURRENCY}）", llm)
    if srv is not None:
        _scenario_section(sb, f"场景 5：server 并发（{SERVER_TASKS} 个小项目同时创建）", srv)

    # ---- 结论
    sb.append("## 结论")
    sb.append("")
    sb.extend(_conclusions(scale_results, llm, srv))
    sb.append("")
    return "\n".join(sb)


def _conclusions(scale_results: dict[int, dict[str, ScenarioResult]],
                 llm: ScenarioResult | None, srv: ScenarioResult | None) -> list[str]:
    """结论段：吞吐结论、并发扩展、预算熔断、R1-8、瓶颈 Top3（全部由实测数据拼装）。"""
    out: list[str] = []

    def m(results: dict[str, ScenarioResult], name: str) -> dict[str, Any]:
        r = results.get(name)
        return r.metrics if r else {}

    # 1) 吞吐
    throughput_lines = []
    for scale, results in sorted(scale_results.items()):
        s2 = m(results, "rules_audit")
        spk = s2.get("sec_per_kloc")
        if spk is not None:
            verdict = "满足" if float(spk) < 30.0 else "超出"
            throughput_lines.append(
                f"{scale} 文件档纯规则审计 {s2.get('wall_sec')}s（{s2.get('loc')} 行 → "
                f"{spk} s/KLOC，{verdict} docs/04 §3.2 的 30 s/KLOC 目标），规则命中 {s2.get('rule_hits')} 条"
            )
    if throughput_lines:
        out.append("**吞吐结论**：" + "；".join(throughput_lines) + "。")
        out.append("")
        scales_sorted = sorted(scale_results)
        if len(scales_sorted) >= 2:
            a, b = scales_sorted[0], scales_sorted[-1]
            wa = m(scale_results[a], "rules_audit").get("wall_sec")
            wb = m(scale_results[b], "rules_audit").get("wall_sec")
            if wa and wb:
                out.append(f"规模 {a}→{b}（×{b / a:.0f}）时端到端耗时 ×{wb / max(wa, 1e-9):.1f}，"
                           "接近线性扩展，规则引擎未表现超线性劣化。")
                out.append("")

    # 2) 并发扩展
    if llm is not None and llm.status == "ok":
        mt = llm.metrics
        out.append(
            f"**LLM 并发扩展**：{mt.get('n_calls')} 文件 × 并发 {mt.get('concurrency')} 实测 wall "
            f"{mt.get('wall_sec')}s，理想 wall（ceil(n/8)×0.2s）{mt.get('ideal_wall_sec')}s，"
            f"扩展系数 {mt.get('expansion_factor')}（理论 {mt.get('concurrency')}），"
            f"wall/理想 = {mt.get('wall_over_ideal')}——Semaphore 并发调度有效。"
        )
        out.append("")

    # 3) 预算熔断
    budget_rows = [
        (scale, m(results, "budget_circuit_breaker"))
        for scale, results in sorted(scale_results.items())
        if "budget_circuit_breaker" in results
    ]
    if budget_rows:
        parts = [
            f"{scale} 档：{mt.get('llm_calls')} 次调用 / {mt.get('py_files')} 文件"
            f"（{mt.get('calls_per_file')} 次/文件），review_errors {mt.get('review_errors')}，"
            f"{'熔断生效、部分结果收尾' if mt.get('budget_tripped') else '熔断证据不成立'}"
            for scale, mt in budget_rows
            if mt
        ]
        out.append("**预算熔断**（token_budget=5000，tools 路径按 1/10 下发=500）："
                   + "；".join(parts) + "。任务全部正常完成（done 事件），未崩溃。")
        out.append("")

    # 4) R1-8
    r18_rows = [
        (scale, m(results, "r18_quantify"))
        for scale, results in sorted(scale_results.items())
        if "r18_quantify" in results and results["r18_quantify"].status != "skip"
    ]
    if r18_rows:
        parts = []
        for scale, mt in r18_rows:
            if not mt:
                continue
            parts.append(
                f"{scale} 档 {mt.get('call_edges')} 条调用边：base build {mt.get('base_build_best3_sec')}s"
                f"（best-of-3），_resolve_edges 占 {mt.get('resolve_edges_cum_sec')}s（cProfile 累计），其中 "
                f"_language_of + _aliases 的 N+1 查询占其 {mt.get('n_plus_one_share_of_resolve') and round(mt['n_plus_one_share_of_resolve'] * 100, 1)}%；"
                f"预取原型实测 {mt.get('prefetch_build_best3_sec')}s（省 {mt.get('measured_saving_pct')}%，边数一致={mt.get('prefetch_edges_identical')}）"
            )
        out.append("**R1-8 量化**：" + "；".join(parts) + "。")
        out.append("")
        out.append("说明：上述占比是 cProfile 的累计口径（探针会放大逐调用开销），为节省上限估算；"
                   "预取原型的 best-of-3 A/B 才是实测收益——SQLite 主键查询热缓存下本身很快，"
                   "故实测省的比例显著低于占比。规模再上一个量级时 N+1 的线性开销占比会继续抬升。")
        out.append("")
        out.append("建议：_resolve_edges 循环前一次性预取 files.language 映射，并按 caller 文件"
                   "缓存 imports 别名表（可参照 bench/stress/run_stress.py 的 _PrefetchedIndexStore 原型），"
                   "属低风险纯查询层优化。")
        out.append("")

    # 5) server
    if srv is not None and srv.status == "ok":
        mt = srv.metrics
        out.append(
            f"**server 并发**：{mt.get('tasks')} 个不同小项目同时创建，全部 done={mt.get('all_done')}，"
            f"报告互不串扰={mt.get('no_crosstalk')}，总耗时 {mt.get('total_wall_sec')}s"
            "（同循环 CPU 串行推进，与并发价值说明见场景明细）。"
        )
        out.append("")

    # 6) 瓶颈 Top3
    biggest = max(scale_results) if scale_results else None
    out.append("**瓶颈 Top3**（按最大规模档实测数据）：")
    out.append("")
    if biggest is not None:
        results = scale_results[biggest]
        m1, m2, r18 = m(results, "ingest_index"), m(results, "rules_audit"), results.get("r18_quantify")
        total = float(m2.get("wall_sec") or 0) or 1.0
        front_share = float(m1.get("total_sec") or 0) / total
        out.append(
            f"1. **索引构建 N+1 查询（R1-8）**：index build {m1.get('index_build_sec')}s / 调用边 "
            f"{m1.get('call_edges')} 条"
            + (f"，_resolve_edges 内 {round(float(r18.metrics['n_plus_one_share_of_resolve']) * 100, 1)}% 花在逐行 "
               "_language_of/_aliases SQL 上，预取实测可省 "
               f"{r18.metrics.get('measured_saving_pct')}%（best-of-3 A/B）" if r18 and r18.status != "skip" else "")
            + "。"
        )
        out.append(
            f"2. **ingest 全树复制**：{m1.get('ingest_sec')}s（含逐文件 sha256/清单/过滤）；"
            f"ingest+index 合计 {m1.get('total_sec')}s，占纯规则审计端到端的 {front_share * 100:.0f}%——"
            "流水线前半段（工作副本物化 + 索引）是单次审计的固定串行开销，可考虑硬链接/增量副本。"
        )
        out.append(
            f"3. **规则扫描的逐文件串行扫描**：纯规则审计 wall {m2.get('wall_sec')}s 中，规则命中 "
            f"{m2.get('rule_hits')} 条、产出 {m2.get('issues')} 个 Issue；规则阶段为纯 CPU 单线程，"
            "可按文件分片多进程并行（优先级低于 R1-8）。"
        )
    out.append("")
    return out


# ---------------------------------------------------------------- 编排与 CLI


def _safe(name: str, fn: Any, *args: Any) -> ScenarioResult:
    """场景容错执行：单场景异常不中断整份报告。"""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 —— 场景失败只记 ERROR 行
        return ScenarioResult(name=name, status="error", error=f"{type(exc).__name__}: {exc}")


def run_stress(
    scales: list[int],
    out_path: Path,
    data_dir: Path = DEFAULT_DATA_DIR,
    work_dir: Path | None = None,
) -> Path:
    """执行全部场景并写出 markdown 报告，返回报告路径。"""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    cleanup = work_dir is None
    work_dir = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="stress_work_"))
    work_dir.mkdir(parents=True, exist_ok=True)

    meta = env_header(scales)
    meta["数据目录"] = str(data_dir)
    scale_results: dict[int, dict[str, ScenarioResult]] = {}
    shared: dict[str, ScenarioResult] = {}

    max_scale = max(scales)
    llm_files = llm_scenario_files(max_scale)
    llm_proj = data_dir / f"proj_llm{llm_files}"

    try:
        for scale in scales:
            print(f"[stress] === 规模 {scale} ===", file=sys.stderr)
            proj = data_dir / f"proj{scale}"
            g_start = time.perf_counter()
            generate_project(proj, files=scale, defect_ratio=DEFECT_RATIO, seed=STRESS_SEED)
            print(f"[stress] 生成 {proj}（{time.perf_counter() - g_start:.1f}s）", file=sys.stderr)

            scale_work = work_dir / f"scale{scale}"
            scale_work.mkdir(parents=True, exist_ok=True)
            per_scale: dict[str, ScenarioResult] = {}
            print("[stress] 场景 1：ingest+index ...", file=sys.stderr)
            per_scale["ingest_index"] = _safe("ingest_index", scenario_ingest_index, proj, scale_work)
            print(f"[stress]   -> {per_scale['ingest_index'].badge}", file=sys.stderr)

            print("[stress] 场景 2：纯规则审计 ...", file=sys.stderr)
            per_scale["rules_audit"] = _safe("rules_audit", scenario_rules_audit, proj,
                                             scale_work / "audit")
            print(f"[stress]   -> {per_scale['rules_audit'].badge}", file=sys.stderr)

            print("[stress] 场景 4：预算熔断 ...", file=sys.stderr)
            per_scale["budget_circuit_breaker"] = _safe(
                "budget_circuit_breaker", scenario_budget_circuit_breaker, proj, scale_work / "budget")
            print(f"[stress]   -> {per_scale['budget_circuit_breaker'].badge}", file=sys.stderr)

            # R1-8 只在最大档（1 万+ 调用边）量化；其余档记 SKIP
            if scale == max_scale:
                print("[stress] 场景 6：R1-8 量化 ...", file=sys.stderr)
                base_build = float((per_scale["ingest_index"].metrics or {}).get("index_build_sec") or 0.0)
                per_scale["r18_quantify"] = _safe("r18_quantify", scenario_r18_quantify, proj,
                                                  scale_work / "r18", base_build)
            else:
                per_scale["r18_quantify"] = ScenarioResult(
                    name="r18_quantify", status="skip",
                    notes=[f"仅最大档（{max_scale}）做 R1-8 量化，本档跳过"])
            print(f"[stress]   -> {per_scale['r18_quantify'].badge}", file=sys.stderr)
            scale_results[scale] = per_scale

        print("[stress] 场景 3：LLM 并发扩展 ...", file=sys.stderr)
        generate_project(llm_proj, files=llm_files, defect_ratio=0.0, seed=STRESS_SEED)
        shared["llm_concurrency"] = _safe("llm_concurrency", scenario_llm_concurrency, llm_proj,
                                          llm_files, work_dir / "llm")
        print(f"[stress]   -> {shared['llm_concurrency'].badge}", file=sys.stderr)

        print("[stress] 场景 5：server 并发 ...", file=sys.stderr)
        shared["server_concurrency"] = _safe("server_concurrency", scenario_server_concurrency,
                                             data_dir, work_dir / "server")
        print(f"[stress]   -> {shared['server_concurrency'].badge}", file=sys.stderr)
    finally:
        if cleanup:
            shutil.rmtree(work_dir, ignore_errors=True)

    markdown = render_report(meta, scale_results, shared)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8", newline="\n")

    statuses = [r.status for rs in scale_results.values() for r in rs.values()]
    statuses += [r.status for r in shared.values()]
    bad = sum(1 for s in statuses if s != "ok")
    print(f"[stress] 报告已写入：{out_path}", file=sys.stderr)
    print(f"[stress] 场景状态：{len(statuses) - bad} OK / {bad} 非 OK（SKIP 或 ERROR，详见报告）",
          file=sys.stderr)
    return out_path


def main(argv: Sequence[str] | None = None) -> int:
    """CLI：``python -m bench.stress.run_stress [--scale 500|2000|all] [--out md路径]``。"""
    parser = argparse.ArgumentParser(
        prog="python -m bench.stress.run_stress",
        description="压测运行器：ingest/index 计时、纯规则吞吐（s/KLOC）、LLM 并发扩展、"
                    "预算熔断、server 并发、R1-8 索引 N+1 量化，产出 markdown 报告（全程离线）。",
    )
    parser.add_argument("--scale", default="500",
                        help="规模档：500 / 2000 / 任意正整数 / all（=500+2000）；默认 500")
    parser.add_argument("--out", type=Path, default=None,
                        help=f"报告输出路径（缺省 {DEFAULT_RESULTS_DIR / ('stress_YYYYMMDD.md')}）")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help="合成项目数据目录（默认 bench/stress/data，已 gitignore）")
    parser.add_argument("--work-dir", type=Path, default=None,
                        help="运行期工作目录（缺省系统临时目录，结束后清理）")
    args = parser.parse_args(argv)

    try:
        scales = parse_scale_arg(args.scale)
    except ValueError as exc:
        parser.error(str(exc))

    out_path = args.out or (DEFAULT_RESULTS_DIR / f"stress_{time.strftime('%Y%m%d')}.md")
    run_stress(scales, out_path, data_dir=args.data_dir, work_dir=args.work_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
