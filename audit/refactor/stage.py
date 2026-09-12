"""Stage「refactor」重构方案生成（W7-A2，契约 v1.7）。

流程：聚合生成（heuristics，确定性零 LLM）→ 可选 LLM 增强（enable_llm_review
且 llm_available 时对 top ≤5 方案深化）→ 写 ctx.refactor_proposals → emit 汇总
事件。事件文案「重构方案：N 条」不含「跳过/skipped」等 Web 阶段判定关键字，
也不含「完成/已生成」，Web 端将该阶段保持 active 至后续阶段推进（安全）。
"""

from __future__ import annotations

from audit.pipeline import PipelineContext
from audit.refactor.heuristics import generate_proposals
from audit.refactor.llm import enhance_proposals

__all__ = ["run_refactor_stage"]

_LLM_TOP_N = 5  # LLM 增强的方案数上限


async def run_refactor_stage(ctx: PipelineContext) -> None:
    """重构方案阶段入口：编排层在 detect 之后、fix 之前调用（契约 v1.7）。

    产出：
    - ctx.refactor_proposals：结构化方案列表（heuristic / heuristic+llm）；
    - ctx.extra["refactor_stats"] = {"proposals", "enhanced", "degraded", "llm_targets"}；
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

    ctx.refactor_proposals = proposals
    ctx.extra["refactor_stats"] = stats
    await ctx.emit("refactor", f"重构方案：{len(proposals)} 条", total=len(proposals), **stats)
