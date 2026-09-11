"""检测层：静态规则契约（base，冻结）+ Python 规则库 + 注册表 + 检测流水线（T4）。"""

from __future__ import annotations

from audit.detect.base import Rule, RuleContext, RuleRegistry
from audit.detect.engine import (
    IssueReviewFn,
    build_rule_contexts,
    dedup_issues,
    hits_to_issues,
    run_detection,
    run_rules,
    validate_issue_lines,
)
from audit.detect.registry import DEFAULT_REGISTRY, get_registry, register_rule
from audit.detect.rules import build_python_rules, python_rule_count

__all__ = [
    "DEFAULT_REGISTRY",
    "IssueReviewFn",
    "Rule",
    "RuleContext",
    "RuleRegistry",
    "build_python_rules",
    "build_rule_contexts",
    "dedup_issues",
    "get_registry",
    "hits_to_issues",
    "python_rule_count",
    "register_rule",
    "run_detection",
    "run_rules",
    "validate_issue_lines",
]
