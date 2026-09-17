"""静态规则库包：按语言暴露规则构建函数与数量统计。

当前提供 Python 规则集（T4）、JS/TS 规则集（Wave 2 A4）、扩展集（W6/W15）
W16 补缺集（命名规范 / PII / JS 命令注入 / 分层架构违规）
与 Java 规则集（W24-A）。
"""

from __future__ import annotations

from audit.detect.base import Rule
from audit.detect.rules.arch_layers import build_arch_layer_rules
from audit.detect.rules.java import build_java_rules
from audit.detect.rules.js.javascript import build_javascript_rules
from audit.detect.rules.js.js_security_ext import build_js_security_ext_rules
from audit.detect.rules.pii_rules import build_pii_rules
from audit.detect.rules.py_naming import build_py_naming_rules
from audit.detect.rules.python import build_python_rules
from audit.detect.rules.js.typescript import build_typescript_rules

__all__ = [
    "build_arch_layer_rules",
    "build_java_rules",
    "build_javascript_rules",
    "build_js_security_ext_rules",
    "build_pii_rules",
    "build_py_naming_rules",
    "build_python_rules",
    "build_typescript_rules",
    "java_rule_count",
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


def java_rule_count() -> int:
    """当前内置 Java 规则数量（W24-A）。"""
    rules: list[Rule] = build_java_rules()
    return len(rules)
