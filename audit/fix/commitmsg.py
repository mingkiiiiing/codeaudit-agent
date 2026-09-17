"""commit message 与 PR 描述自动生成（P0-8）：纯字符串拼接，零 LLM token。

背景：apply-to-source 回路（P0-2）把补丁应用回用户源码后，提交信息与 PR 描述
此前要用户手工撰写。本模块从「选中补丁集 + 报告 Issue 列表」确定性拼接建议文案：

- build_commit_message：单补丁模板 ``fix(<category>): <title> (<rule_id>)``；
  多补丁聚合为多行并按 (category, rule_id) 去重（同类规则只出一行，先到先得）；
- build_pr_description：Markdown 聚合 PR 描述——变更摘要（命中规则计数）、
  涉及规则清单、逐补丁验证状态表（apply_status 与 tests_run/tests_passed 如实
  呈现，needs-review / failed 显式标注）、兼容性说明（compat_notes 非空才列）。

诚实降级：patch.issue_id 反查不到 Issue 时，category 降级为 general、rule_id
降级为 unknown-rule、标题取 rationale 首行（截断 72 字符）。

rule_id 来源说明：Issue 契约无 rule_id 字段；遵循 sarif 导出的同款约定——
detect 引擎在 Issue.evidence 写入形如 ``rule:PY-XXX`` 的证据行，从此提取；
无规则证据（纯 LLM 问题）回退 unknown-rule。
"""

from __future__ import annotations

from audit.models import Issue, Patch

__all__ = [
    "build_commit_message",
    "build_pr_description",
]

_EVIDENCE_RULE_PREFIX = "rule:"

_TITLE_MAX = 72  # 降级标题（rationale 首行）截断长度

_UNKNOWN_RULE = "unknown-rule"

# apply_status 的诚实标注（如实呈现原始值，括号内为人工可读说明；未知状态不标注）
_STATUS_LABEL: dict[str, str] = {
    "pending": "待验证",
    "verified": "已通过验证",
    "needs-review": "需人工复核",
    "syntax-ok": "仅语法校验通过",
    "failed": "验证失败",
}


def _enum_value(value: object) -> str:
    """枚举或字符串统一取字符串值（Issue 反序列化前后形态一致）。"""
    return str(getattr(value, "value", value))


def _lookup_issues(issues: list[Issue]) -> dict[str, Issue]:
    """Issue id -> Issue 索引（跳过空 id，防误命中）。"""
    return {issue.id: issue for issue in issues if issue.id}


def _rule_id_of(issue: Issue | None) -> str:
    """从 Issue.evidence 提取规则 id（``rule:PY-XXX`` 证据行）；无则 unknown-rule。"""
    if issue is None:
        return _UNKNOWN_RULE
    for item in issue.evidence or []:
        text = str(item)
        if text.startswith(_EVIDENCE_RULE_PREFIX):
            rule_id = text[len(_EVIDENCE_RULE_PREFIX) :].strip()
            if rule_id:
                return rule_id
    return _UNKNOWN_RULE


def _first_line(text: str, limit: int = _TITLE_MAX) -> str:
    """取 text 首行并截断到 limit 字符（降级标题用；空串安全）。"""
    lines = (text or "").strip().splitlines()
    return lines[0].strip()[:limit] if lines else ""


def _commit_line(patch: Patch, issue: Issue | None) -> str:
    """单条 commit message 行：命中模板或诚实降级。"""
    if issue is None:
        return f"fix(general): {_first_line(patch.rationale)} ({_UNKNOWN_RULE})"
    return f"fix({_enum_value(issue.category)}): {issue.title} ({_rule_id_of(issue)})"


def build_commit_message(patches: list[Patch], issues: list[Issue]) -> str:
    """由选中补丁集拼接建议 commit message（多补丁按 (category, rule_id) 去重）。

    Args:
        patches: 选中的 Patch 列表（顺序即输出行序）。
        issues: 报告的全部 Issue（用于按 patch.issue_id 反查 category/title/rule_id）。

    Returns:
        commit message 文案；空补丁集返回空串。
    """
    by_id = _lookup_issues(issues)
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for patch in patches:
        issue = by_id.get(patch.issue_id)
        category = _enum_value(issue.category) if issue is not None else "general"
        key = (category, _rule_id_of(issue))
        if key in seen:
            continue
        seen.add(key)
        lines.append(_commit_line(patch, issue))
    return "\n".join(lines)


def build_pr_description(patches: list[Patch], issues: list[Issue]) -> str:
    """由选中补丁集聚合 Markdown PR 描述（验证状态与兼容性说明如实呈现）。

    Args:
        patches: 选中的 Patch 列表。
        issues: 报告的全部 Issue（用于按 patch.issue_id 反查）。

    Returns:
        Markdown 文案；空补丁集返回空串。
    """
    if not patches:
        return ""
    by_id = _lookup_issues(issues)
    # 逐补丁解析展示三元组：(patch, category, rule_id, 标题)；反查失败诚实降级
    resolved: list[tuple[Patch, str, str, str]] = []
    for patch in patches:
        issue = by_id.get(patch.issue_id)
        if issue is None:
            resolved.append((patch, "general", _UNKNOWN_RULE, _first_line(patch.rationale)))
        else:
            resolved.append(
                (patch, _enum_value(issue.category), _rule_id_of(issue), issue.title)
            )

    # 变更摘要：命中规则计数（保持首次出现顺序）
    counts: dict[str, int] = {}
    for _patch, _category, rule_id, _title in resolved:
        counts[rule_id] = counts.get(rule_id, 0) + 1
    lines: list[str] = [
        "## 变更摘要",
        "",
        f"本 PR 由 CodeAudit Agent 自动修复生成，共 {len(patches)} 个补丁，"
        f"命中 {len(counts)} 条规则：",
        "",
    ]
    lines += [f"- {rule_id} × {count}" for rule_id, count in counts.items()]

    # 涉及规则清单（按 rule_id 去重，标题取首个补丁对应的 Issue）
    lines += ["", "## 涉及规则", ""]
    seen_rules: set[str] = set()
    for _patch, category, rule_id, title in resolved:
        if rule_id in seen_rules:
            continue
        seen_rules.add(rule_id)
        lines.append(f"- `{rule_id}`（{category}）{title}")

    # 逐补丁验证状态表：apply_status / tests_run / tests_passed 如实呈现
    lines += ["", "## 验证状态", "", "| # | 补丁 | 规则 | 状态 | 测试 |", "| --- | --- | --- | --- | --- |"]
    for index, (patch, _category, rule_id, _title) in enumerate(resolved):
        label = _STATUS_LABEL.get(patch.apply_status, "")
        status = f"{patch.apply_status}（{label}）" if label else patch.apply_status
        tests = f"{patch.tests_passed}/{patch.tests_run}" if patch.tests_run > 0 else "未运行"
        lines.append(f"| {index} | `{patch.id}` | {rule_id} | {status} | {tests} |")

    # 兼容性说明：compat_notes 全空则整节不出现（非空才列）
    notes = [
        (patch, note) for patch, *_rest in resolved for note in (patch.compat_notes or [])
    ]
    if notes:
        lines += ["", "## 兼容性说明", ""]
        lines += [f"- `{patch.id}`：{note}" for patch, note in notes]
    return "\n".join(lines)
