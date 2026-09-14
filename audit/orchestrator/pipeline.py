"""七阶段审计流水线编排：Orchestrator。

设计要点（见 docs/06 协作铁律与 docs/02 §8 时序）：
- 所有跨包模块（T2 ingest/indexer、T3 glm_client/agents.review、T4 understand/detect）
  一律延迟导入 + try/except ImportError：模块未集成时 emit 跳过事件并继续，
  保证本包在只有契约层的仓库状态下可独立测试。
- 单阶段失败只记入 ctx.extra["stage_errors"]，审计永不整体失败。
- 各阶段开始/结束均通过 emitter 发进度事件。
- 全局 token 预算闸门（F8 / W12-A3）：llm 客户端统一包装 _BudgetGateLLM，
  调用前按 config.token_budget 熔断（抛 AgentBudgetError 走各消费方既有降级
  路径），首次熔断发"预算"warning 事件；熔断后 fix/testgen 纯 LLM 阶段跳过，
  done 事件携带 degraded + budget_tripped 标志。
"""

from __future__ import annotations

import inspect
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from audit.config import AuditConfig
from audit.confkit import apply_baseline, apply_diff_filter, changed_files, load_baseline, write_baseline
from audit.errors import AgentBudgetError
from audit.llm.base import FakeLLMClient, LLMClient, LLMResponse
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


class _BudgetGateLLM(LLMClient):
    """全局 token 预算闸门（F8 / W12-A3）：包装底层 LLM 客户端做调用前熔断。

    背景（docs/17 §1.2 F8）：既有熔断只有 agent 级一处（SimpleAgentRuntime 的
    轮次检查，stop_reason="budget"），且仅 tools 审查路径消费 config.token_budget
    （还按文件重置为 //10）；simple 审查 / verify / understand 增强 / refactor
    增强 / fix / testgen 均直接调用 llm.chat，全局预算完全不受控——在线模式下
    预算形同虚设。本闸门在编排层统一收口：

    - 计量：每次调用完成后从 resp.usage 累加 prompt+completion tokens（与
      runtime 同口径）。已在途的调用允许完成并计入，不中断已发出的请求；
    - 熔断点：发起新调用前，已耗尽（spent >= budget）或 budget<=0（零/负
      预算视为禁用 LLM）→ 抛 AgentBudgetError；各消费方（检测引擎逐文件、
      verify 逐条、各阶段兜底）按既有异常路径诚实降级，流水线不中断；
    - 语义差异说明：runtime 的轮次检查用严格 `>`（保证循环至少完成一轮，
      属既有对外语义，保持不变）；编排层闸门是跨阶段总闸，若同样用 `>` 则
      恰好用尽后还会再放行一次调用、预算必然被突破，故此处用 `>=`；
    - 告警：首次熔断时经 emitter 发 warning 事件（文案含"预算"），挂
      stage="init" 通道（与"LLM 未配置"警告同口径，避免钉死业务阶段），
      并把 budget_tripped / budget_used_tokens 写入 ctx.extra，供 done 事件
      携带降级标志、报告统计保持真实口径（usage_totals 委托底层客户端）。
    """

    def __init__(
        self,
        inner: LLMClient,
        budget: int,
        emitter: EventEmitter,
        extra: dict[str, Any],
    ) -> None:
        self._inner = inner
        self._budget = int(budget)
        self._emitter = emitter
        self._extra = extra
        self.spent_tokens = 0  # 已完成调用累计消耗（prompt+completion）
        self.rejected_calls = 0  # 熔断后被拒绝的调用次数（诊断用）

    @property
    def budget(self) -> int:
        return self._budget

    @property
    def tripped(self) -> bool:
        return bool(self._extra.get("budget_tripped", False))

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
        temperature: float = 0.2,
    ) -> LLMResponse:
        if self._budget <= 0 or self.spent_tokens >= self._budget:
            # 调用前已耗尽（含零/负预算防御）：熔断并拒绝本次调用
            if not self.tripped:
                await self._trip()
            self.rejected_calls += 1
            raise AgentBudgetError(
                f"Token 预算熔断：已消耗 {self.spent_tokens} tokens（预算 {self._budget}），"
                f"本次 LLM 调用被拒绝（累计第 {self.rejected_calls} 次），降级为本地结果"
            )
        resp = await self._inner.chat(messages, tools=tools, json_mode=json_mode, temperature=temperature)
        usage = resp.usage or {}
        self.spent_tokens += int(usage.get("prompt_tokens", 0) or 0) + int(
            usage.get("completion_tokens", 0) or 0
        )
        if not self.tripped and self.spent_tokens >= self._budget:
            # 预算恰好用尽：即时告警置位（此后任何新调用都会被拒绝）
            await self._trip()
        return resp

    def usage_totals(self) -> dict[str, int]:
        """委托底层客户端：报告统计保持真实口径（熔断拒绝的调用不计入）。"""
        return self._inner.usage_totals()

    async def aclose(self) -> None:
        """释放底层客户端资源（run_audit 的 finally 统一收口）。"""
        await _close_quietly(self._inner)

    async def _trip(self) -> None:
        """首次熔断：置标志、写 ctx.extra、发"预算"warning 事件（仅一次）。"""
        self._extra["budget_tripped"] = True
        self._extra["budget_used_tokens"] = self.spent_tokens
        self._extra["budget_total"] = self._budget
        try:
            await self._emitter(
                {
                    "type": "progress",
                    "stage": "init",
                    "message": (
                        f"Token 预算已耗尽：已消耗 {self.spent_tokens} tokens"
                        f"（预算 {self._budget}），预算熔断生效，后续 LLM 调用将被拒绝并降级"
                    ),
                    "warning": True,
                    "used_tokens": self.spent_tokens,
                    "token_budget": self._budget,
                }
            )
        except Exception:  # noqa: BLE001 —— 告警失败不影响熔断本身
            pass


def _budget_gate_of(ctx: PipelineContext) -> _BudgetGateLLM | None:
    """取 ctx 上的预算闸门（非闸门客户端返回 None，如测试直接注入裸 LLM）。"""
    return ctx.llm if isinstance(ctx.llm, _BudgetGateLLM) else None


def _budget_fused(ctx: PipelineContext) -> bool:
    """预算是否已熔断（或零/负预算注定熔断）：供后续 LLM 阶段门控跳过。

    tripped 在"预算耗尽"时即置位（不必等到下一次调用被拒绝），因此
    detect 恰好耗尽预算时，fix/testgen 也会被明确跳过并发事件。
    """
    gate = _budget_gate_of(ctx)
    if gate is None:
        return False
    return gate.tripped or gate.budget <= 0


def _make_review_fn(ctx: PipelineContext) -> Callable[..., Awaitable[list]]:
    """把 T3 的 review_file 适配为 T4 run_detection 期待的 review_fn。

    引擎侧调用约定（audit.detect.engine.IssueReviewFn）：review_fn(workspace, file_path,
    hints)；review_file 真实签名为 (workspace, index, llm, file_path, hints, ...)。
    此处按名转接（R1-1：历史上 *args 直接拼接导致 file_path 绑到 workspace 位），
    并兼容同步/异步实现。
    """
    from audit.agents.review import review_file  # 延迟导入：T3 未集成时走 ImportError 分支

    async def review_fn(
        workspace: Any, file_path: str, hints: list[str] | None = None, **kwargs: Any
    ) -> list:
        result = review_file(workspace, ctx.index, ctx.llm, file_path, hints, **kwargs)
        return await _maybe_await(result)  # type: ignore[no-any-return]

    return review_fn


async def _close_quietly(obj: Any) -> None:
    """尽力释放对象资源（close/aclose，兼容同步/异步）；失败静默（收尾不影响主流程）。"""
    for attr in ("aclose", "close"):
        closer = getattr(obj, attr, None)
        if callable(closer):
            break
    else:
        return
    try:
        result = closer()
        if inspect.isawaitable(result):
            await result
    except Exception:  # noqa: BLE001 —— 资源释放失败不改变审计结果
        pass


async def _skip_gated_stage(ctx: PipelineContext, stage: str, reason: str) -> None:
    """ingest 未成功时跳过依赖工作副本的阶段，发明确事件（R1-2 门控）。"""
    await ctx.emit(stage, f"跳过：{reason}（ingest 未成功，无可用工作副本）")


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


# ---------------------------------------------------------------- 契约 v1.4：diff 增量 / 基线（W4-A2）

_DIFF_UNSUPPORTED_MSG = "diff 增量仅支持本地 git 目录输入，本次跳过增量"


async def _apply_diff_increment(ctx: PipelineContext) -> None:
    """diff 增量剪枝（契约 v1.4，docs/09 §1 第 3 行）：ingest 阶段完成后调用。

    - zip 输入 / 非 git 目录（或 git 失败）：发 warning 事件并继续全量。注意该
      警告挂 stage="init" 通道（与"LLM 未配置"警告同口径）：文案含"跳过"二字，
      若挂 stage="ingest" 会把 Web 端已完成检测完成的 ingest 阶段钉死为 skipped；
    - 拿到变更集合：物理剪枝工作副本到变更文件，并发「增量模式」事件；
    - 变更集合与工作副本无交集（保留 0 个）：发「增量集合为空」警告并回退全量。
    """
    source = Path(ctx.config.source_path)
    if source.is_file():  # zip 入口：工作副本来自解压，无从对 git ref 求差
        await ctx.emit("init", _DIFF_UNSUPPORTED_MSG, warning=True)
        return
    changed = changed_files(source, ctx.config.diff_ref)
    if changed is None:  # 非 git 目录或 git 不可用/失败
        await ctx.emit("init", _DIFF_UNSUPPORTED_MSG, warning=True)
        return
    kept = apply_diff_filter(ctx.workspace, changed)
    if kept == 0:
        await ctx.emit("ingest", "增量集合为空，回退全量", warning=True)
        return
    await ctx.emit("ingest", f"增量模式：仅审计 {kept} 个变更文件", files=kept)


async def _apply_baseline_suppression(ctx: PipelineContext) -> None:
    """基线抑制（契约 v1.4，docs/09 §1 第 4 行）：detect 阶段完成后调用。

    - 命中基线指纹的问题从 ctx.issues 剔除，计入 ctx.stats.suppressed；
    - 事件文案「基线抑制：N 个已知问题」不含 Web 阶段判定关键字，安全。
    """
    fingerprints = load_baseline(Path(ctx.config.baseline_path))
    ctx.issues, suppressed = apply_baseline(ctx.issues, fingerprints)
    ctx.stats.suppressed = suppressed
    await ctx.emit("detect", f"基线抑制：{suppressed} 个已知问题", suppressed=suppressed)


async def _export_baseline(ctx: PipelineContext) -> None:
    """基线导出（契约 v1.4）：report 阶段完成后调用，把当前问题写为基线文件。

    事件文案含「基线已写入」；不改变既有 report 阶段的「报告已生成」事件。
    """
    out = write_baseline(ctx.issues, Path(ctx.config.report_baseline_out))
    await ctx.emit("report", f"基线已写入：{out}", baseline=str(out))


async def run_audit(config: AuditConfig, emitter: EventEmitter) -> AuditReport:
    """执行七阶段审计流水线，返回最终 AuditReport（审计永不整体失败）。

    阶段：ingest → index → understand → detect → refactor → fix(可选) → testgen(可选) → report。
    总耗时写入 ctx.stats.duration_sec（time.monotonic）。

    资源收口（R1-3/R1-4）：无论正常结束还是异常，finally 中统一关闭 LLM 客户端
    （aclose）与索引连接（store.close）；ingest 未成功时 index/understand/detect/
    fix 全部门控跳过（R1-2/R4-4），不会触碰用户原始输入目录；报告统计走空
    manifests（R4-4），done 事件携带 degraded 标志（R4-10）。
    """
    started = time.monotonic()
    audit_id = new_audit_id()

    # 先建"临时"工作区：ingest 成功后替换为真实工作副本；
    # ingest 未集成/失败时，后续依赖工作区的阶段被门控跳过，报告阶段仍产出空报告。
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
    # F8（W12-A3）：全局 token 预算闸门——包装任何客户端（真实 GlmClient 与
    # FakeLLM 同口径），调用前熔断 + 首次熔断发"预算"warning 事件；熔断标志
    # 写入 ctx.extra，done 事件据此携带降级信息。
    gate = _BudgetGateLLM(llm, config.token_budget, emitter, ctx.extra)
    ctx.llm = gate
    ctx.extra["audit_id"] = audit_id
    # 原始项目名（剥掉 zip 后缀）：T2 工作副本目录名固定为 src，报告应展示真实项目名
    ctx.extra["project_name"] = src.name[: -len(".zip")] if src.name.endswith(".zip") else src.name

    try:
        return await _run_audit_stages(ctx, started, audit_id)
    finally:
        # R1-4：关闭索引连接（index 阶段异常时其内部已兜底关闭，这里幂等收口）
        index = ctx.index if ctx.index is not None else ctx.workspace.index
        if index is not None:
            await _close_quietly(index)
        # R1-3：LLM 客户端（GlmClient 的 httpx 连接池）必须在结束时释放
        # （闸门 aclose 委托底层客户端；直接关底层等价，统一走闸门收口）
        await _close_quietly(gate)


async def _run_audit_stages(ctx: PipelineContext, started: float, audit_id: str) -> AuditReport:
    """run_audit 的阶段主体（资源收口由 run_audit 的 finally 负责）。"""
    config = ctx.config

    # -------- Stage 1: ingest
    async def _do_ingest() -> None:
        from audit.ingest import ingest

        # R1-17：把编排层生成的 audit_id 传入，保证工作副本落在同一 task_root
        result = await _maybe_await(ingest(config.source_path, Path(config.work_root), audit_id=audit_id))
        ctx.workspace = result  # 替换为真实工作副本
        manifests = getattr(result, "manifests", None) or []
        await ctx.emit("ingest", f"工作副本就绪：{result.src_root}", files=len(manifests))

    ingest_ok = await _run_stage(ctx, "ingest", _do_ingest)
    ctx.extra["ingest_ok"] = ingest_ok  # R1-2 门控标志

    # -------- Stage 1b: diff 增量剪枝（契约 v1.4，docs/09 §1 第 3 行）
    # 仅在 ingest 成功且 ctx.workspace 已替换为独立工作副本时执行：
    # ingest 失败时 ctx.workspace 仍指向原始输入目录，此时物理删文件会毁掉源项目。
    if config.diff_ref and ingest_ok and ctx.workspace.src_root != Path(config.source_path).resolve():
        try:
            await _apply_diff_increment(ctx)
        except Exception as exc:  # noqa: BLE001 —— 增量失败回退全量，不中断流水线
            _record_stage_error(ctx, "ingest", exc)
            await ctx.emit("ingest", f"stage error: {type(exc).__name__}: {exc}", error=True)

    # -------- Stage 2: index（R1-2：未 ingest 成功时跳过，不扫描原始目录）
    async def _do_index() -> None:
        from audit.indexer import create_index

        store = await _maybe_await(create_index(ctx.workspace))
        try:
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
        except BaseException:
            await _close_quietly(store)  # 构建失败也释放连接（R1-4）
            raise

    if ingest_ok:
        await _run_stage(ctx, "index", _do_index)
    else:
        await _skip_gated_stage(ctx, "index", "无工作副本可索引")

    # -------- Stage 3: understand（R4-4：随 ingest_ok 门控——ingest 失败时
    # workspace 仍指向用户原始目录，架构理解不得回扫用户输入）
    async def _do_understand() -> None:
        from audit.understand.architecture import build_architecture

        card = await _maybe_await(build_architecture(ctx))
        ctx.architecture = card
        await ctx.emit("understand", "架构理解完成")

    if ingest_ok:
        await _run_stage(ctx, "understand", _do_understand)
    else:
        await _skip_gated_stage(ctx, "understand", "不执行架构理解")

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

    if ingest_ok:
        await _run_stage(ctx, "detect", _do_detect)
    else:
        await _skip_gated_stage(ctx, "detect", "不执行检测")

    # -------- Stage 4b: 基线抑制（契约 v1.4，docs/09 §1 第 4 行）
    if config.baseline_path:
        try:
            await _apply_baseline_suppression(ctx)
        except Exception as exc:  # noqa: BLE001 —— 基线故障按零抑制继续
            _record_stage_error(ctx, "detect", exc)
            await ctx.emit("detect", f"stage error: {type(exc).__name__}: {exc}", error=True)

    # -------- Stage 4c: refactor（契约 v1.7：赛题要求 5「自动生成重构方案」；
    # R1-2：随 ingest 门控——无工作副本时既无索引也无检测结果）
    async def _do_refactor() -> None:
        from audit.refactor import run_refactor_stage

        await _maybe_await(run_refactor_stage(ctx))

    if ingest_ok:
        await _run_stage(ctx, "refactor", _do_refactor)
    else:
        await _skip_gated_stage(ctx, "refactor", "无索引与检测结果，不产出重构方案")

    # -------- Stage 5: fix（契约 v1.2：模块存在则真实执行；R1-2：未 ingest 成功时门控；
    # F8：预算熔断后补丁生成为纯 LLM 阶段，继续执行只会逐条刷异常，明确跳过）
    if not config.do_fix:
        await ctx.emit("fix", "跳过（未启用 --fix）")
    elif _budget_fused(ctx):
        await ctx.emit("fix", "跳过：Token 预算已耗尽，不生成修复补丁", budget_tripped=True)
    elif ingest_ok:
        await _run_fix_stage(ctx)
    else:
        await _skip_gated_stage(ctx, "fix", "不生成修复补丁")

    # -------- Stage 6: testgen（契约 v1.2：模块存在则真实执行；R1-2 同类门控：
    # 未 ingest 成功时 workspace 指向原始输入目录，写入生成测试会污染用户项目；
    # F8：预算熔断后单测生成为纯 LLM 阶段，明确跳过）
    if not config.do_tests:
        await ctx.emit("testgen", "跳过（未启用 --tests）")
    elif _budget_fused(ctx):
        await ctx.emit("testgen", "跳过：Token 预算已耗尽，不生成单测", budget_tripped=True)
    elif ingest_ok:
        await _run_testgen_stage(ctx)
    else:
        await _skip_gated_stage(ctx, "testgen", "不生成单测")

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

    # -------- Stage 7b: 基线导出（契约 v1.4）：把本次剩余问题写为基线文件
    if config.report_baseline_out:
        try:
            await _export_baseline(ctx)
        except Exception as exc:  # noqa: BLE001 —— 导出失败不影响报告
            _record_stage_error(ctx, "report", exc)
            await ctx.emit("report", f"stage error: {type(exc).__name__}: {exc}", error=True)

    ctx.stats.duration_sec = time.monotonic() - started

    # 报告阶段万一失败也兜底返回内存报告，保证调用方拿到结果
    report = ctx.extra.get("_report")
    if report is None:
        from audit.report.builder import build_report

        report = build_report(ctx)

    # done 事件（R4-10）：ingest 失败（降级运行）时携带 degraded 标志，
    # 供 CLI / Web 端明确提示"本次为降级审计"，而不是把空报告当成正常结果。
    # F8（W12-A3）：预算熔断同样是降级运行——degraded 置位并附 budget_tripped /
    # 实际消耗与预算值。used_tokens 取闸门最终累计值（而非熔断瞬间快照）：
    # 熔断后已在途的调用仍会完成并计入，最终口径必须与报告 stats 一致。
    degraded: dict[str, Any] = {} if ingest_ok else {"degraded": True}
    if ctx.extra.get("budget_tripped"):
        gate = _budget_gate_of(ctx)
        used = gate.spent_tokens if gate is not None else int(ctx.extra.get("budget_used_tokens", 0) or 0)
        degraded["degraded"] = True
        degraded["budget_tripped"] = True
        degraded["used_tokens"] = used
        degraded["token_budget"] = int(ctx.extra.get("budget_total", 0) or 0)
    await ctx.emit(
        "done",
        f"审计完成：summary={count_by_severity(ctx.issues)} issues={len(ctx.issues)}",
        duration_sec=round(ctx.stats.duration_sec, 2),
        **degraded,
    )
    return report


async def run_audit_simple(
    config: AuditConfig, events: list[dict[str, Any]] | None = None
) -> AuditReport:
    """便捷入口：事件收集到内部 list（no-op emitter），供 CLI / 测试使用。

    events 参数（R4-10）：调用方可传入列表原地收集事件流（如 CLI 据此
    判断 degraded 降级标志）；None 时行为与旧版完全一致。
    """
    collected = events if events is not None else []

    async def collector(event: dict[str, Any]) -> None:
        collected.append(event)

    return await run_audit(config, collector)
