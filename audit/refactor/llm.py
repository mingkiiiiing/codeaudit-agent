"""重构方案的 LLM 增强层（W7-A2，契约 v1.7）。

对启发式方案中的 top N（≤5，按置信度）调用 llm.chat(json_mode=True)，请求
{"rationale": "...", "steps": ["..."], "benefit": "..."} 以深化理由与步骤：

- 解析失败重试 1 次，仍失败则安全降级为启发式原文（FakeLLM 脚本耗尽返回空
  content → 解析失败 → 降级，保证纯离线/无 LLM 场景行为稳定）；
- 成功则替换 rationale/steps/benefits（字段级校验，非法字段保留原值），
  source 升级为 "heuristic+llm"。
"""

from __future__ import annotations

import json
from typing import Any

from audit.models import RefactorProposal
from audit.pipeline import PipelineContext
from audit.utils import extract_json, truncate

__all__ = ["enhance_proposals", "parse_enhancement"]

_TOP_N = 5  # 最多增强的方案数（docs/12 §3 契约 v1.7）
_MAX_ATTEMPTS = 2  # 首次 + 重试 1 次
_INSTRUCTIONS = (
    "你是资深重构顾问。请针对给定的重构方案，输出更具体、可执行的深化建议。\n"
    "只输出一个 JSON 对象，不要输出任何其他文字，格式为：\n"
    '{"rationale": "为什么这样重构（2-4 句，引用具体代码事实）", '
    '"steps": ["步骤1", "步骤2", ...], "benefit": "收益一句话"}\n'
    "steps 为字符串数组（3-6 步，每步一个可验证的动作）。"
)

_SYSTEM_ROLE = "重构顾问"


def _proposal_payload(proposal: RefactorProposal) -> str:
    """把方案要点序列化为请求上下文（含关联问题标题，供模型引用代码事实）。"""
    return json.dumps(
        {
            "title": proposal.title,
            "target": proposal.target,
            "kind": proposal.kind,
            "current_rationale": proposal.rationale,
            "current_steps": proposal.steps,
            "related_issue_count": len(proposal.related_issues),
        },
        ensure_ascii=False,
    )


def parse_enhancement(content: str | None) -> dict[str, Any] | None:
    """解析并校验 LLM 增强响应；非法返回 None（调用方降级）。

    合法标准：JSON 对象，steps 为非空字符串数组；rationale/benefit 允许为空
    字符串（此时保留启发式原值）。steps 中非字符串元素被剔除。
    """
    try:
        data = extract_json(content)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    steps_raw = data.get("steps")
    if not isinstance(steps_raw, list):
        return None
    steps = [str(s).strip() for s in steps_raw if isinstance(s, str) and str(s).strip()]
    if not steps:
        return None
    rationale = data.get("rationale")
    benefit = data.get("benefit")
    return {
        "rationale": rationale.strip() if isinstance(rationale, str) else "",
        "steps": steps,
        "benefit": benefit.strip() if isinstance(benefit, str) else "",
    }


def _messages(proposal: RefactorProposal, retry_hint: str = "") -> list[dict[str, Any]]:
    user = f"{_INSTRUCTIONS}\n\n待深化方案：\n{_proposal_payload(proposal)}"
    if retry_hint:
        user += f"\n\n注意：{retry_hint}"
    return [
        {"role": "system", "content": _SYSTEM_ROLE},
        {"role": "user", "content": user},
    ]


async def enhance_proposals(
    ctx: PipelineContext, proposals: list[RefactorProposal], top_n: int = _TOP_N
) -> dict[str, int]:
    """对置信度最高的 top_n 个方案做 LLM 增强（原地修改），返回统计。

    统计键：targets（进入增强的方案数）/ enhanced（成功升级）/ degraded（降级保留原文）。
    单方案增强失败不阻断后续；整体异常由 stage 层兜底。
    """
    stats = {"targets": 0, "enhanced": 0, "degraded": 0}
    targets = sorted(proposals, key=lambda p: -p.confidence)[: max(0, int(top_n))]
    stats["targets"] = len(targets)
    for proposal in targets:
        enhanced = await _enhance_one(ctx, proposal)
        if enhanced:
            stats["enhanced"] += 1
        else:
            stats["degraded"] += 1
    return stats


async def _enhance_one(ctx: PipelineContext, proposal: RefactorProposal) -> bool:
    """单个方案的增强闭环：请求 → 解析（失败重试 1 次）→ 字段替换或降级。"""
    messages = _messages(proposal)
    retry_hint = ""
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = await ctx.llm.chat(messages, json_mode=True)
            parsed = parse_enhancement(resp.content)
        except Exception:  # noqa: BLE001 —— LLM 调用/解析异常一律降级，不阻断阶段
            parsed = None
        if parsed is not None:
            if parsed["rationale"]:
                proposal.rationale = parsed["rationale"]
            proposal.steps = parsed["steps"]
            if parsed["benefit"]:
                proposal.benefits = parsed["benefit"]
            proposal.source = "heuristic+llm"
            return True
        retry_hint = "上次输出不可解析，请严格只输出一个 JSON 对象（含 rationale/steps/benefit 三个键）。"
        messages = _messages(proposal, retry_hint)
    # 降级：保留启发式原文（source 不变），仅截断超长内容以防报告失控
    proposal.rationale = truncate(proposal.rationale, 2000)
    return False
