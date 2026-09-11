"""静态规则库包：按语言暴露规则构建函数与数量统计。

当前提供 Python 规则集（T4）与 JS/TS 规则集（Wave 2 A4）。
"""

from __future__ import annotations

from audit.detect.base import Rule
from audit.detect.rules.js.javascript import build_javascript_rules
from audit.detect.rules.python import build_python_rules
from audit.detect.rules.js.typescript import build_typescript_rules

__all__ = [
    "build_javascript_rules",
    "build_python_rules",
    "build_typescript_rules",
    "javascript_rule_count",
    "python_rule_count",
    "typescript_rule_count",
]


def python_rule_count() -> int:
    """当前内置 Python 规则数量。"""
    rules: list[Rule] = build_python_rules()
    return len(rules)


def javascript_rule_count() -> int:
    """当前内置 JavaScript 规则数量（含与 TypeScript 共享的安全规则）。"""
    rules: list[Rule] = build_javascript_rules()
    return len(rules)


def typescript_rule_count() -> int:
    """当前内置 TypeScript 专属规则数量（不含共享的 JS 安全规则）。"""
    rules: list[Rule] = build_typescript_rules()
    return len(rules)
