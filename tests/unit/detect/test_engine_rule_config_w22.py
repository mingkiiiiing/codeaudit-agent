"""W22-C engine 侧规则级配置行为测试：禁用 / 严重度覆盖 / 路径跳过。

复用全局 pipeline_ctx fixture（demo_proj 样例工程，含已知命中：
PY-BARE-EXCEPT / PY-PRINT-DEBUG / PY-EQ-NONE 等）。config 三字段直接赋值
（dataclass 可变），等价于配置文件/CLI 合成后的产物。
"""

from __future__ import annotations

import pytest

from audit.detect.engine import (
    _apply_rule_config,
    _matches_ignore_pattern,
    build_rule_contexts,
    run_detection,
    run_rules,
)
from audit.detect.registry import get_registry
from audit.models import Severity


@pytest.fixture
def rule_ctx(pipeline_ctx):
    """带 W22-C 字段操作入口的 pipeline_ctx（每个用例独立 config 字段）。"""
    return pipeline_ctx


class TestDisabledRules:
    async def test_config_disabled_rules_drop_hits(self, rule_ctx):
        """config.disabled_rules 命中的规则零出现，其余规则不受影响。"""
        rule_ctx.config.disabled_rules = ["PY-PRINT-DEBUG", "PY-HARDCODED-URL"]
        run_rules(rule_ctx)
        ids = {h.rule_id for h in rule_ctx.rule_hits}
        assert "PY-PRINT-DEBUG" not in ids
        assert "PY-HARDCODED-URL" not in ids
        assert "PY-BARE-EXCEPT" in ids

    async def test_unknown_rule_id_warned_not_fatal(self, rule_ctx):
        """未知规则 id 记入 rule_config_warnings，检测照常完成。"""
        rule_ctx.config.disabled_rules = ["NO-SUCH-RULE", "PY-PRINT-DEBUG"]
        issues = await run_detection(rule_ctx)
        assert isinstance(issues, list)
        warnings = rule_ctx.extra.get("rule_config_warnings", [])
        assert any("NO-SUCH-RULE" in w for w in warnings)
        # 合法 id 仍然生效
        ids = {i.evidence[0][len("rule:"):] for i in issues if i.evidence and i.evidence[0].startswith("rule:")}
        assert "PY-PRINT-DEBUG" not in ids

    async def test_extra_disabled_rules_still_respected(self, rule_ctx):
        """既有 ctx.extra["disabled_rules"] 注入口（测试/内部路径）与 config 合并存续。"""
        rule_ctx.extra["disabled_rules"] = ["PY-BARE-EXCEPT"]
        rule_ctx.config.disabled_rules = ["PY-PRINT-DEBUG"]
        run_rules(rule_ctx)
        ids = {h.rule_id for h in rule_ctx.rule_hits}
        assert "PY-BARE-EXCEPT" not in ids and "PY-PRINT-DEBUG" not in ids


class TestSeverityOverrides:
    async def test_override_changes_issue_severity(self, rule_ctx):
        """severity_overrides 改写后：Issue 严重度、rule_hits 同步变化。"""
        rule_ctx.config.severity_overrides = {"PY-BARE-EXCEPT": "critical"}
        await run_detection(rule_ctx)
        hit = next(h for h in rule_ctx.rule_hits if h.rule_id == "PY-BARE-EXCEPT")
        assert hit.severity == Severity.CRITICAL

    async def test_override_invalid_severity_warned(self, rule_ctx):
        """非法严重度值：记警告、不应用，检测不中断。"""
        rule_ctx.config.severity_overrides = {"PY-BARE-EXCEPT": "fatal"}
        issues = await run_detection(rule_ctx)
        assert isinstance(issues, list)
        warnings = rule_ctx.extra.get("rule_config_warnings", [])
        assert any("PY-BARE-EXCEPT" in w and "非法严重度" in w for w in warnings)
        hit = next(h for h in rule_ctx.rule_hits if h.rule_id == "PY-BARE-EXCEPT")
        assert hit.severity != Severity.CRITICAL  # 未被非法值污染

    async def test_override_unknown_rule_warned(self, rule_ctx):
        rule_ctx.config.severity_overrides = {"NO-SUCH-RULE": "high"}
        overrides = _apply_rule_config(rule_ctx, get_registry())
        assert overrides == {}
        assert any("NO-SUCH-RULE" in w for w in rule_ctx.extra["rule_config_warnings"])


class TestIgnorePaths:
    def test_matches_ignore_pattern_forms(self):
        """目录前缀与 fnmatch 两种模式语义。"""
        assert _matches_ignore_pattern("vendor/pkg/a.py", "vendor/")
        assert _matches_ignore_pattern("vendor/pkg/a.py", "vendor")
        assert _matches_ignore_pattern("src/gen_x.py", "**/*_gen*.py") is False
        assert _matches_ignore_pattern("src/x_generated.py", "**/*_generated.py")
        assert _matches_ignore_pattern("app/services/orders.py", "app/services/**")
        assert not _matches_ignore_pattern("application/x.py", "app/")  # 前缀不越界

    def test_ignore_paths_skip_files_in_contexts(self, rule_ctx):
        """命中 ignore_paths 的文件不进规则上下文（LLM 审查清单同源，一处生效）。"""
        rule_ctx.config.ignore_paths = ["app/services/"]
        rels = {rc.rel_path for rc in build_rule_contexts(rule_ctx)}
        assert rels  # 其余文件照常
        assert not any(r.startswith("app/services/") for r in rels)

    async def test_ignore_paths_no_hits_from_ignored_files(self, rule_ctx):
        """被忽略文件的规则在该文件上的命中全部消失（demo_proj orders.py 有已知命中）。"""
        issues_all = {i.file for i in await run_detection(rule_ctx)}
        assert "app/services/orders.py" in issues_all  # 基线：正常时该文件有命中
        rule_ctx.config.ignore_paths = ["app/services/orders.py"]
        issues_ignored = {i.file for i in await run_detection(rule_ctx)}
        assert "app/services/orders.py" not in issues_ignored


class TestDefaultEquivalence:
    async def test_empty_config_zero_behavior_change(self, rule_ctx):
        """三字段默认空时与 W22-C 之前等价：warning 键不存在、检测正常出结果。"""
        issues = await run_detection(rule_ctx)
        assert issues
        assert "rule_config_warnings" not in rule_ctx.extra or not rule_ctx.extra["rule_config_warnings"]
        assert rule_ctx.config.disabled_rules == []
