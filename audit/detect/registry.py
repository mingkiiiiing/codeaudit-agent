"""全局规则注册表：import 即完成全部内置规则的注册。"""

from __future__ import annotations

from audit.detect.base import Rule, RuleRegistry
from audit.detect.rules import (
    build_javascript_rules,
    build_python_rules,
    build_typescript_rules,
)

DEFAULT_REGISTRY: RuleRegistry = RuleRegistry()
DEFAULT_REGISTRY.register_all(build_python_rules())
DEFAULT_REGISTRY.register_all(build_javascript_rules() + build_typescript_rules())


def get_registry() -> RuleRegistry:
    """返回全局默认注册表；集成方可继续向其注册自定义/扩展规则。"""
    return DEFAULT_REGISTRY


def register_rule(rule: Rule) -> Rule:
    """向默认注册表追加单条规则（便于外部扩展）。"""
    return DEFAULT_REGISTRY.register(rule)
