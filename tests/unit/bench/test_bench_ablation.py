"""bench.ablation 单元测试：7 组配置齐全、占位键不与 AuditConfig 字段冲突。"""

from __future__ import annotations

import dataclasses

from audit.config import AuditConfig

from bench.ablation import ABLATION_CONFIGS, plan_ablation

MINUS = "\u2212"  # 任务书中的减号（U+2212）


def test_eight_configs_present():
    """W22-E 起为 8 组（新增 llm_focus，见 bench/ablation.py）。"""
    expected = {
        "full",
        f"{MINUS}verify",
        f"{MINUS}rule_hints",
        f"{MINUS}symbol_context",
        f"{MINUS}cache",
        "rules_only",
        "llm_only",
        "llm_focus",
    }
    assert set(ABLATION_CONFIGS) == expected
    assert len(ABLATION_CONFIGS) == 8


def test_plan_ablation_ordered_copies():
    plan = plan_ablation()
    assert len(plan) == 8
    assert [name for name, _ in plan] == list(ABLATION_CONFIGS)
    for name, overrides in plan:
        assert isinstance(name, str) and isinstance(overrides, dict)
        # 返回的是拷贝，改它不影响原表
        overrides["__probe__"] = 1
    assert all("__probe__" not in v for v in ABLATION_CONFIGS.values())


def test_rules_only_expressible_in_config():
    overrides = ABLATION_CONFIGS["rules_only"]
    config = AuditConfig(source_path="x", **overrides)
    assert config.enable_llm_review is False


def test_placeholder_keys_are_not_config_fields():
    valid = {f.name for f in dataclasses.fields(AuditConfig)}
    for _name, overrides in ABLATION_CONFIGS.items():
        for key in overrides:
            assert key in valid or key.startswith("_"), f"非法键 {key}（既非字段也非占位）"
