"""Stage「refactor」重构方案生成（W7-A2，契约 v1.7）。

流程：聚合生成（heuristics，确定性零 LLM）→ 可选 LLM 增强（enable_llm_review
且 llm_available 时对 top ≤5 方案深化）→ 写 ctx.refactor_proposals → emit 汇总
事件。事件文案「重构方案：N 条」不含「跳过/skipped」等 Web 阶段判定关键字，
也不含「完成/已生成」，Web 端将该阶段保持 active 至后续阶段推进（安全）。

W15-E（重构执行闭环）：enable_llm_review && llm_available && do_fix 三条件同时
成立时，把 confidence 最高的 ≤3 条启发式方案合成为 Issue（source=LLM、
severity=MEDIUM、title 带 "[Refactor] " 前缀）注入 ctx.issues，喂给既有 fix
阶段管线（LLM 生成 diff → 校验 → 应用 → 语法双校验 → 沙箱测试 → 四态
FixStatus/回滚），产物自然进 report.patches——models.py 契约零改动。离线或未
开 --fix 时不合成，ctx.issues 零变化（既有行为不变）。
"""

from __future__ import annotations

import re

from audit.models import Category, Issue, IssueSource, RefactorProposal, Severity
from audit.pipeline import PipelineContext
from audit.refactor.heuristics import generate_proposals
from audit.refactor.llm import enhance_proposals
from audit.utils import make_id

__all__ = ["run_refactor_stage"]

_LLM_TOP_N = 5  # LLM 增强的方案数上限

# W15-E：合成 Issue 的 title 前缀——fix 阶段据此放行 medium 候选（公开常量，
# audit.fix.stage 导入共用，单一事实源）
REFACTOR_ISSUE_TITLE_PREFIX = "[Refactor] "
# W15-E：注入 fix 管线的方案数上限（confidence 降序取 top N）
_REFACTOR_ISSUE_TOP_N = 3
# W15-E：从既有 Issue.id 恢复编号水位（make_id("ISS", n) 形态），合成 Issue 续号防撞
_ISS_ID_RE = re.compile(r"^ISS-(\d+)$")


def _target_file_of(proposal: RefactorProposal, ctx: PipelineContext) -> str | None:
    """W15-E：proposal.target → 工作副本内相对文件路径；映射不了返回 None。

    target 形如 "app/services/orders.py::create_order"（decompose）或
    "app/services/orders.py"（dedup/split-module）；"project:imports" 这类
    非文件目标与工作副本中不存在的路径都返回 None（调用方跳过并计数）。
    """
    rel = str(proposal.target or "").split("::", 1)[0].strip().replace("\\", "/")
    if not rel or ":" in rel:  # "project:imports" 等非文件目标（:: 已在上一步剥离）
        return None
    try:
        if not ctx.workspace.abs_path(rel).is_file():
            return None
    except (OSError, ValueError):
        return None
    return rel


def _next_issue_id(ctx: PipelineContext) -> str:
    """W15-E：扫描既有 Issue 编号水位，返回不冲突的 ISS-XXXX（保持既有编号风格）。"""
    max_no = 0
    for issue in ctx.issues:
        m = _ISS_ID_RE.match(issue.id or "")
        if m:
            max_no = max(max_no, int(m.group(1)))
    return make_id("ISS", max_no + 1)


def _synthesize_refactor_issues(ctx: PipelineContext, proposals: list[RefactorProposal]) -> tuple[int, int]:
    """W15-E：把 top-N 启发式方案合成为可修复 Issue 注入 ctx.issues。

    只取 source 以 "heuristic" 开头的方案（含增强后的 heuristic+llm），按
    confidence 降序取 top ≤3；target 映射不到工作副本文件的跳过并计数。
    返回 (注入数, 跳过数)。三条件门控（enable_llm_review && llm_available &&
    do_fix）由调用方保证，其余路径不调用本函数。
    """
    candidates = sorted(
        (p for p in proposals if str(p.source).startswith("heuristic")),
        key=lambda p: -p.confidence,
    )[:_REFACTOR_ISSUE_TOP_N]
    injected = skipped = 0
    for proposal in candidates:
        rel = _target_file_of(proposal, ctx)
        if rel is None:
            skipped += 1
            continue
        ctx.issues.append(
            Issue(
                id=_next_issue_id(ctx),
                # W15-E：重构属结构治理，归 STYLE；severity=medium，fix 侧按
                # title 前缀放行（普通 medium 仍不进修复管线）
                category=Category.STYLE,
                severity=Severity.MEDIUM,
                title=f"{REFACTOR_ISSUE_TITLE_PREFIX}{proposal.title}",
                file=rel,
                line_start=1,
                line_end=1,
                code_snippet="",
                description=proposal.rationale,
                evidence=[
                    f"refactor:{proposal.id}",
                    f"kind:{proposal.kind}",
                    f"target:{proposal.target}",
                ],
                suggestion="\n".join(proposal.steps),
                confidence=proposal.confidence,
                source=IssueSource.LLM,
            )
        )
        injected += 1
    return injected, skipped


async def run_refactor_stage(ctx: PipelineContext) -> None:
    """重构方案阶段入口：编排层在 detect 之后、fix 之前调用（契约 v1.7）。

    产出：
    - ctx.refactor_proposals：结构化方案列表（heuristic / heuristic+llm）；
    - ctx.extra["refactor_stats"] = {"proposals", "enhanced", "degraded", "llm_targets"}；
      W15-E：三条件门控命中时追加 {"refactor_fix_issues", "refactor_fix_skipped"}；
    - ctx.emit("refactor", ...) 进度事件。
    """
    proposals = generate_proposals(ctx)
    stats = {"proposals": len(proposals), "enhanced": 0, "degraded": 0, "llm_targets": 0}

    if proposals and ctx.config.enable_llm_review and ctx.config.llm_available:
        try:
            enhance_stats = await enhance_proposals(ctx, proposals, top_n=_LLM_TOP_N)
            stats["enhanced"] = enhance_stats["enhanced"]
            stats["degraded"] = enhance_stats["degraded"]
            stats["llm_targets"] = enhance_stats["targets"]
            if enhance_stats["targets"]:
                await ctx.emit(
                    "refactor",
                    f"LLM 增强：{enhance_stats['enhanced']}/{enhance_stats['targets']} 条方案已深化"
                    f"（降级 {enhance_stats['degraded']} 条保留启发式原文）",
                )
        except Exception as exc:  # noqa: BLE001 —— 增强失败降级为纯启发式结果
            stats["degraded"] = stats["llm_targets"] = 0
            await ctx.emit(
                "refactor",
                f"LLM 增强不可用，保留启发式方案：{type(exc).__name__}: {exc}",
                warning=True,
            )

    # W15-E（重构执行闭环）：三条件同时成立才把方案转修复候选——离线（无 LLM）
    # 或未开 --fix 时零行为变化（不合成、不加统计键，既有 stats 形状不变）。
    if proposals and ctx.config.enable_llm_review and ctx.config.llm_available and ctx.config.do_fix:
        injected, skipped = _synthesize_refactor_issues(ctx, proposals)
        if injected or skipped:
            stats["refactor_fix_issues"] = injected
            stats["refactor_fix_skipped"] = skipped
            # 文案避开「跳过/skipped」关键字（refactor 通道 Web 阶段判定安全）
            await ctx.emit(
                "refactor",
                f"重构执行：{injected} 条方案已注入修复候选（{skipped} 条无文件目标未注入）",
                refactor_fix_issues=injected,
                refactor_fix_skipped=skipped,
            )

    ctx.refactor_proposals = proposals
    ctx.extra["refactor_stats"] = stats
    await ctx.emit("refactor", f"重构方案：{len(proposals)} 条", total=len(proposals), **stats)
