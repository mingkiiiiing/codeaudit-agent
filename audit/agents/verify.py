"""Verify Agent（证据核验角色，T3；Wave 2 A3 接入版本化 prompt v2）。

按 docs/03 §3.2 的复核清单构造 prompt（audit/agents/prompts.py 的 VERIFY_PROMPT_V2，
补充反幻觉硬约束），json_mode 单次调用，解析裁定：
  {"verdict": "confirmed|false_positive|uncertain", "reason": "...",
   "confidence": 0~1, "severity": "critical|high|medium|low"}

裁定语义：
- confirmed：应用响应的 severity（合法时）与 confidence，reason 追加进 evidence；
- false_positive：confidence 置 0，description 附加驳回原因；severity 保持原值
  （docs/03 §3.2：驳回的问题也提交，供报告统计误报率）；
- uncertain / 未知 verdict：confidence 取 min(原值, 响应值)；响应未给则封顶 0.5；
- 解析失败：整体降级为 uncertain，confidence 取 min(原值, 0.5)。

返回 Issue 的拷贝（不原地修改入参）。
"""

from __future__ import annotations

import json
from typing import Any

from audit.agents.prompts import PROMPT_VERSION, VERIFY_PROMPT_V2  # noqa: F401（verify prompt 本波未变）
from audit.llm.base import LLMClient, Message
from audit.models import Issue, Severity
from audit.utils import extract_json
from audit.workspace import WorkspaceContext

__all__ = [
    "PROMPT_VERSION",
    "VERIFY_PROMPT_V2",
    "VERIFY_SYSTEM_PROMPT_TEMPLATE",
    "build_verify_prompt",
    "verify_issue",
]

_VALID_SEVERITIES = {s.value for s in Severity}
_CONTEXT_MARGIN = 5  # 报告行上下文的前后扩展行数

# 版本化 prompt（v2）：本模块保留旧常量名作为别名，历史引用不断链
VERIFY_SYSTEM_PROMPT_TEMPLATE = VERIFY_PROMPT_V2


def build_verify_prompt(issue_json: str) -> str:
    """按 docs/03 §3.2 复核清单组装 Verify System Prompt。"""
    return VERIFY_SYSTEM_PROMPT_TEMPLATE.format(issue_json=issue_json)


def _context_text(workspace: WorkspaceContext, issue: Issue) -> str:
    """报告行 ±5 行的带行号源码上下文；文件缺失时如实说明。"""
    try:
        total = workspace.line_count(issue.file)
        start = max(1, int(issue.line_start or 1) - _CONTEXT_MARGIN)
        end = min(total, int(issue.line_end or issue.line_start or 1) + _CONTEXT_MARGIN)
        lines = workspace.read_lines(issue.file, start, end)
    except OSError:
        return "（问题文件缺失，无法提供源码上下文）"
    numbered = "\n".join(f"{i}: {line}" for i, line in enumerate(lines, start=start))
    return f"{issue.file} 第 {start}-{end} 行：\n{numbered}"


def _parse_verdict(text: str) -> dict[str, Any] | None:
    """解析裁定 JSON；失败返回 None。"""
    try:
        data = extract_json(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or "verdict" not in data:
        return None
    return data


def _safe_confidence(value: Any) -> float | None:
    """校验响应里的 confidence；非法/缺失返回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    conf = float(value)
    if not 0.0 <= conf <= 1.0:
        return None
    return conf


async def verify_issue(
    workspace: WorkspaceContext,
    index: Any,
    llm: LLMClient,
    issue: Issue,
) -> Issue:
    """复核单条 Issue，返回裁定后的拷贝（index 参数保留给未来跨文件取证扩展）。"""
    issue_json = json.dumps(issue.to_dict(), ensure_ascii=False)
    system_prompt = build_verify_prompt(issue_json)
    messages: list[Message] = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": f"[报告行源码上下文]\n{_context_text(workspace, issue)}\n\n请按复核清单输出 JSON 裁定。",
        },
    ]
    resp = await llm.chat(messages, json_mode=True)

    result = Issue.from_dict(issue.to_dict())  # 拷贝，不原地修改入参
    data = _parse_verdict(resp.content or "")
    if data is None:
        # 解析失败：降级 uncertain，confidence 封顶 min(原值, 0.5)
        result.confidence = min(result.confidence, 0.5)
        result.description = (result.description + "\n[Verify 降级] 复核结果解析失败，按 uncertain 处理。").strip()
        return result

    reason = str(data.get("reason", "")).strip()
    response_confidence = _safe_confidence(data.get("confidence"))
    response_severity = str(data.get("severity", ""))
    verdict = str(data.get("verdict", "")).strip().lower()

    if verdict == "confirmed":
        if response_confidence is not None:
            result.confidence = response_confidence
        if response_severity in _VALID_SEVERITIES:
            result.severity = Severity(response_severity)
        if reason:
            result.evidence = list(result.evidence) + [f"[verify:confirmed] {reason}"]
    elif verdict == "false_positive":
        result.confidence = 0.0  # severity 保持原值，供报告统计误报率
        result.description = (result.description + f"\n[Verify 驳回] {reason or '未给出驳回理由'}").strip()
    else:  # uncertain 或未知 verdict：置信度只降不升
        cap = response_confidence if response_confidence is not None else 0.5
        result.confidence = min(result.confidence, cap)
        if reason:
            result.description = (result.description + f"\n[Verify:uncertain] {reason}").strip()
    return result
