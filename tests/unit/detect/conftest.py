"""T4 检测测试公共 fixtures：直接构造 RuleContext（不依赖索引）。"""

from __future__ import annotations

import pytest

from audit.detect.base import Rule, RuleContext, RuleRegistry
from audit.detect.rules import build_python_rules


@pytest.fixture
def make_ctx():
    """构造 RuleContext 的工厂：source -> lines，语言默认 python。"""

    def _make(source: str, rel_path: str = "m.py", language: str = "python") -> RuleContext:
        lines = source.splitlines()
        return RuleContext(rel_path=rel_path, language=language, source=source, lines=lines)

    return _make


@pytest.fixture
def registry() -> RuleRegistry:
    """一个只装内置 Python 规则的全新注册表（与全局 DEFAULT_REGISTRY 隔离）。"""
    reg = RuleRegistry()
    reg.register_all(build_python_rules())
    return reg


@pytest.fixture
def rule_of(registry: RuleRegistry):
    """按规则 id 取规则实例，便于逐规则正反例测试。"""

    def _get(rule_id: str) -> Rule:
        for r in registry.all_rules:
            if r.id == rule_id:
                return r
        raise KeyError(f"rule not found: {rule_id}")

    return _get


def lines_of(hits) -> list[int]:
    """提取命中行号（1-based）列表。"""
    return [h.line_start for h in hits]
