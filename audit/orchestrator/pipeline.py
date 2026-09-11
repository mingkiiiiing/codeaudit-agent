"""七阶段审计流水线编排：Orchestrator。

设计要点（见 docs/06 协作铁律与 docs/02 §8 时序）：
- 所有跨包模块（T2 ingest/indexer、T3 glm_client/agents.review、T4 understand/detect）
  一律延迟导入 + try/except ImportError：模块未集成时 emit 跳过事件并继续，
  保证本包在只有契约层的仓库状态下可独立测试。
- 单阶段失败只记入 ctx.extra["stage_errors"]，审计永不整体失败。
- 各阶段开始/结束均通过 emitter 发进度事件。
"""

from __future__ import annotations

import inspect
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from audit.config import AuditConfig
from audit.llm.base import FakeLLMClient, LLMClient
from audit.models import AuditReport, count_by_severity
from audit.pipeline import EventEmitter, PipelineContext
from audit.utils import new_audit_id

_SKIP_MSG = "stage skipped (module not integrated)"


async def _maybe_await(result: Any) -> Any:
    """兼容同步/异步两种跨包实现：返回 awaitable 时等待之。"""
    if inspect.isawaitable(result):
        return await result
    return result


def _record_stage_error(ctx: PipelineContext, stage: str, exc: BaseException) -> None:
    """把阶段失败记入 ctx.extra['stage_errors']（不中断流水线）。"""
    errors: list[dict[str, str]] = ctx.extra.setdefault("stage_errors", [])
    errors.append({"stage": stage, "error": f"{type(exc).__name__}: {exc}"})


async def _run_stage(ctx: PipelineContext, stage: str, body: Callable[[], Any]) -> bool:
    """执行单个阶段：统一 emit 开始/结束事件并兜底全部异常。

    body 内部做延迟导入；抛 ImportError 视为"模块未集成"（发 skip 事件），
    其余异常记入 stage_errors。返回 True 表示阶段有实际产出。
    """
    await ctx.emit(stage, f"阶段 {stage} 开始")
    try:
        result = body()
        result = await _maybe_await(result)
    except ImportError as exc:
        await ctx.emit(stage, _SKIP_MSG, reason=str(exc))
        return False
    except Exception as exc:  # noqa: BLE001 —— 审计永不整体失败
        _record_stage_error(ctx, stage, exc)
        await ctx.emit(stage, f"stage error: {type(exc).__name__}: {exc}", error=True)
        return False
    await ctx.emit(stage, f"阶段 {stage} 完成")
    return True


def _make_llm(config: AuditConfig) -> tuple[LLMClient, str | None]:
    """LLM 选择：返回 (客户端, 警告消息|None)。

    - config.llm_available 且启用 review → 延迟导入 GlmClient；
      T3 未集成时回退 FakeLLM 并给出"模块未集成"警告；
    - 否则 FakeLLMClient 纯规则模式（未配置 api_key 时发"LLM 未配置"警告）。
    """
    if config.llm_available and config.enable_llm_review:
        try:
            from audit.llm.glm_client import GlmClient

            return GlmClient(config), None
        except ImportError:
            return FakeLLMClient(), "LLM 模块未集成，运行纯规则模式"
    if not config.llm_available:
        return FakeLLMClient(), "LLM 未配置，运行纯规则模式"
    return FakeLLMClient(), "LLM review 未启用，运行纯规则模式"


def _make_review_fn(ctx: PipelineContext) -> Callable[..., Awaitable[list]]:
    """把 T3 的 review_file 适配为 T4 run_detection 期待的 review_fn。

    T3 签名未知：这里前置注入 ctx，其余参数透传，并兼容同步/异步实现；
    集成时如签名不符只需调整此闭包。
    """
    from audit.agents.review import review_file  # 延迟导入：T3 未集成时走 ImportError 分支

    async def review_fn(*args: Any, **kwargs: Any) -> list:
        # review_file 真实签名为 (workspace, index, llm, file_path, hints, ...)；
        # 引擎侧调用 review_fn(workspace, file_path, hints)，此处做参数转接
        result = review_file(ctx.workspace, ctx.index, ctx.llm, *args, **kwargs)
        return await _maybe_await(result)  # type: ignore[no-any-return]

    return review_fn


async def _run_fix_stage(ctx: PipelineContext) -> None:
    """Stage 5：audit.fix.run_fix_stage（W2-A1）。未集成时降级为占位事件。"""
    try:
        from audit.fix import run_fix_stage
    except ImportError:
        await ctx.emit("fix", "skipped: wave2", reason="audit.fix 未集成")
        return
    try:
        await _maybe_await(run_fix_stage(ctx))
    except Exception as exc:  # noqa: BLE001 —— 单阶段失败不中断流水线
        _record_stage_error(ctx, "fix", exc)
        await ctx.emit("fix", f"stage error: {type(exc).__name__}: {exc}", error=True)


async def _run_testgen_stage(ctx: PipelineContext) -> None:
    """Stage 6：audit.testgen.run_testgen_stage（W2-A2）。未集成时降级为占位事件。"""
    try:
        from audit.testgen import run_testgen_stage
    except ImportError:
        await ctx.emit("testgen", "skipped: wave2", reason="audit.testgen 未集成")
        return
    try:
        await _maybe_await(run_testgen_stage(ctx))
    except Exception as exc:  # noqa: BLE001
        _record_stage_error(ctx, "testgen", exc)
        await ctx.emit("testgen", f"stage error: {type(exc).__name__}: {exc}", error=True)


async def run_audit(config: AuditConfig, emitter: EventEmitter) -> AuditReport:
    """执行七阶段审计流水线，返回最终 AuditReport（审计永不整体失败）。

    阶段：ingest → index → understand → detect → fix(可选) → testgen(可选) → report。
    总耗时写入 ctx.stats.duration_sec（time.monotonic）。
    """
    started = time.monotonic()
    audit_id = new_audit_id()

    # 先建"临时"工作区：ingest 成功后替换为真实工作副本；
    # ingest 未集成/失败时，后续依赖工作区的阶段自行跳过，报告阶段仍产出空报告。
    from audit.workspace import WorkspaceContext

    src = Path(config.source_path).resolve() if config.source_path else Path.cwd()
    work_root = (Path(config.work_root) if config.work_root else Path(".codeaudit")).resolve()
    task_root = work_root / audit_id
    workspace = WorkspaceContext(
        audit_id=audit_id,
        src_root=src,
        work_root=task_root,
        db_path=task_root / "index.db",
    )
    llm, llm_warning = _make_llm(config)
    if llm_warning:
        await emitter({"type": "progress", "stage": "init", "message": llm_warning, "warning": True})

    ctx = PipelineContext(config=config, workspace=workspace, llm=llm, emitter=emitter)
    ctx.extra["audit_id"] = audit_id
    # 原始项目名（剥掉 zip 后缀）：T2 工作副本目录名固定为 src，报告应展示真实项目名
    ctx.extra["project_name"] = src.name[: -len(".zip")] if src.name.endswith(".zip") else src.name

    # -------- Stage 1: ingest
    async def _do_ingest() -> None:
        from audit.ingest import ingest

        result = await _maybe_await(ingest(config.source_path, Path(config.work_root)))
        ctx.workspace = result  # 替换为真实工作副本
        manifests = getattr(result, "manifests", None) or []
        await ctx.emit("ingest", f"工作副本就绪：{result.src_root}", files=len(manifests))

    await _run_stage(ctx, "ingest", _do_ingest)

    # -------- Stage 2: index
    async def _do_index() -> None:
        from audit.indexer import create_index

        store = await _maybe_await(create_index(ctx.workspace))
        build_result = store.build()
        if inspect.isawaitable(build_result):
            await build_result
        ctx.workspace.index = store  # 注入工作区
        ctx.index = store
        try:
            stats = await _maybe_await(store.stats())
            if isinstance(stats, dict):
                await ctx.emit("index", f"索引构建完成：{stats}", **stats)
            else:
                await ctx.emit("index", "索引构建完成")
        except Exception:  # noqa: BLE001 —— stats 仅用于进度展示
            await ctx.emit("index", "索引构建完成")

    await _run_stage(ctx, "index", _do_index)

    # -------- Stage 3: understand
    async def _do_understand() -> None:
        from audit.understand.architecture import build_architecture

        card = await _maybe_await(build_architecture(ctx))
        ctx.architecture = card
        await ctx.emit("understand", "架构理解完成")

    await _run_stage(ctx, "understand", _do_understand)

    # -------- Stage 4: detect（契约 v1.2：review_mode 双模式 + verify_fn 复核）
    async def _do_detect() -> None:
        import inspect as _inspect

        from audit.detect.engine import run_detection

        review_fn = None
        if config.enable_llm_review and config.llm_available:
            try:
                if config.review_mode == "tools":
                    from audit.agents.review import make_tools_review_fn

                    review_fn = make_tools_review_fn(ctx)
                else:
                    review_fn = _make_review_fn(ctx)
            except ImportError:
                review_fn = _make_review_fn(ctx)  # tools 模式未集成 → 回退 simple
        verify_fn = None
        if config.enable_verify and config.llm_available:
            try:
                from audit.agents.verify import verify_issue

                async def _verify_fn(workspace: Any, issue: Any) -> Any:
                    return await _maybe_await(verify_issue(workspace, ctx.index, ctx.llm, issue))

                verify_fn = _verify_fn
            except ImportError:
                verify_fn = None  # W2-A3 未集成
        kwargs: dict[str, Any] = {"review_fn": review_fn}
        if verify_fn is not None and "verify_fn" in _inspect.signature(run_detection).parameters:
            kwargs["verify_fn"] = verify_fn  # 引擎未升级 verify_fn 参数时向后兼容
        issues = await _maybe_await(run_detection(ctx, **kwargs))
        ctx.issues = list(issues or [])
        await ctx.emit("detect", f"检测完成：{len(ctx.issues)} 个问题", total=len(ctx.issues))

    await _run_stage(ctx, "detect", _do_detect)

    # -------- Stage 5: fix（契约 v1.2：模块存在则真实执行）
    if config.do_fix:
        await _run_fix_stage(ctx)
    else:
        await ctx.emit("fix", "跳过（未启用 --fix）")

    # -------- Stage 6: testgen（契约 v1.2：模块存在则真实执行）
    if config.do_tests:
        await _run_testgen_stage(ctx)
    else:
        await ctx.emit("testgen", "跳过（未启用 --tests）")

    # -------- Stage 7: report
    async def _do_report() -> None:
        from audit.report.builder import build_report
        from audit.report.render import write_report

        ctx.stats.duration_sec = time.monotonic() - started
        report = build_report(ctx)
        paths = write_report(report, config.resolve_out_dir())
        ctx.extra["_report"] = report
        ctx.extra["report_paths"] = {k: str(v) for k, v in paths.items()}
        await ctx.emit(
            "report",
            f"报告已生成：{paths['md']}",
            health_score=report.health_score,
            issues=len(report.issues),
        )

    await _run_stage(ctx, "report", _do_report)

    ctx.stats.duration_sec = time.monotonic() - started

    # 报告阶段万一失败也兜底返回内存报告，保证调用方拿到结果
    report = ctx.extra.get("_report")
    if report is None:
        from audit.report.builder import build_report

        report = build_report(ctx)

    await ctx.emit(
        "done",
        f"审计完成：summary={count_by_severity(ctx.issues)} issues={len(ctx.issues)}",
        duration_sec=round(ctx.stats.duration_sec, 2),
    )
    return report


async def run_audit_simple(config: AuditConfig) -> AuditReport:
    """便捷入口：事件收集到内部 list（no-op emitter），供 CLI / 测试使用。"""
    events: list[dict[str, Any]] = []

    async def collector(event: dict[str, Any]) -> None:
        events.append(event)

    return await run_audit(config, collector)
