"""Wave 3 消融开关接线测试（契约 v1.3，docs/08 §3）。

覆盖 audit.detect.engine.run_detection 的两个运行期开关：
- llm_only_mode=True：跳过静态规则（ctx.rule_hits 保持空），Issue 全部来自
  review_fn；行号硬校验与去重合并对纯 LLM 结果照常生效；
- enable_rule_hints=False：传给 review_fn 的 hints 一律为空列表（单文件顺序、
  文件级并发、小切片批量三条路径），规则结果照常进入候选。

以及 bench.ablation 的 7 组配置真实化（零占位、键均可映射 AuditConfig 字段）。
全部基于 FakeLLM / 假 review_fn，零网络、零真实 LLM。
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from audit.config import AuditConfig
from audit.detect.engine import run_detection
from audit.models import Category, Issue, IssueSource, Severity
from bench.ablation import ABLATION_CONFIGS, is_placeholder, plan_ablation

TARGET = "app/services/orders.py"  # demo_proj 中含多条规则命中的文件


def _llm_issue(file: str = TARGET, line: int = 10, **overrides: Any) -> Issue:
    """构造一条来自 LLM 通道的合法 Issue（行号默认落在目标文件内）。"""
    base = dict(
        id="",
        category=Category.BUG,
        severity=Severity.HIGH,
        title="get_user 返回值未判空即访问属性",
        file=file,
        line_start=line,
        line_end=line,
        description="uid 不存在时 get_user 返回 None，后续访问属性会崩",
        evidence=["llm:e1"],
        suggestion="增加判空分支",
        confidence=0.9,
        source=IssueSource.LLM,
    )
    base.update(overrides)
    return Issue(**base)


def _spy_review_fn(
    hints_log: dict[str, list[str]],
    extra_hints_log: list[list[str]] | None = None,
    support_batch: bool = True,
):
    """假 review_fn：记录每个文件收到的 hints，仅对 TARGET 返回 1 条合法 Issue。

    - 附带 _supports_extra_files 标记（support_batch=True）时额外记录批量小切片
      路径收到的 hints，以覆盖 −rule_hints 在批量合并审查路径的行为；
    - support_batch=False 时不带标记（模拟 simple 路径），所有文件走独立审查，
      便于按文件精确断言 Issue 产出。
    """

    async def review_fn(workspace, file_path, hints, extra_files=None):
        hints_log[file_path] = list(hints)
        if extra_files is not None and extra_hints_log is not None:
            extra_hints_log.extend([list(h) for _, _, h in extra_files])
        if file_path == TARGET:
            return [_llm_issue()]
        return []

    if support_batch:
        review_fn._supports_extra_files = True  # type: ignore[attr-defined]
    return review_fn


# ---------------------------------------------------------------- llm_only_mode


class TestLlmOnlyMode:
    async def test_issues_come_from_review_and_rule_hits_empty(self, pipeline_ctx):
        pipeline_ctx.config.llm_only_mode = True
        hints_log: dict[str, list[str]] = {}

        async def review_fn(workspace, file_path, hints):
            hints_log[file_path] = list(hints)
            if file_path == TARGET:
                return [_llm_issue()]
            return []

        issues = await run_detection(pipeline_ctx, review_fn=review_fn)

        # 规则通道被跳过：rule_hits 保持空，Issue 全部来自 review
        assert pipeline_ctx.rule_hits == []
        assert len(issues) == 1
        assert issues[0].source == IssueSource.LLM
        assert issues[0].file == TARGET and issues[0].line_start == 10
        info = pipeline_ctx.extra["detection"]
        assert info["mode"] == "llm_only"
        assert info["rule_hits"] == 0 and info["rule_issues"] == 0
        assert info["llm_issues"] == 1
        # 无规则命中 → hints 自然为空
        assert all(h == [] for h in hints_log.values())

    async def test_no_review_fn_returns_empty_without_error(self, pipeline_ctx):
        pipeline_ctx.config.llm_only_mode = True

        issues = await run_detection(pipeline_ctx, review_fn=None)

        assert issues == []
        assert pipeline_ctx.issues == []
        assert pipeline_ctx.rule_hits == []
        info = pipeline_ctx.extra["detection"]
        assert info["mode"] == "llm_only(no review_fn)"

    async def test_line_validation_and_dedup_still_apply(self, pipeline_ctx):
        """纯 LLM 结果仍过行号硬校验与去重（不因 llm_only 而跳过）。"""
        pipeline_ctx.config.llm_only_mode = True

        async def review_fn(workspace, file_path, hints):
            if file_path == TARGET:
                return [
                    _llm_issue(line=10),
                    _llm_issue(line=12, title="同一问题附近的重复报告"),  # 与上一条 ±5 行同类别 → 去重
                    _llm_issue(line=9999, title="越界行号"),  # 行号校验 → 丢弃
                ]
            return []

        issues = await run_detection(pipeline_ctx, review_fn=review_fn)

        info = pipeline_ctx.extra["detection"]
        assert info["llm_filtered"] == 1  # 越界条目被拦截
        assert len(issues) == 1  # 同位置同类去重后仅剩 1 条
        assert issues[0].line_start == 10

    async def test_llm_only_takes_precedence_over_rule_mode(self, pipeline_ctx):
        """llm_only_mode=True 且 review_fn 存在时，规则结果不进入候选。"""
        pipeline_ctx.config.llm_only_mode = True

        async def review_fn(workspace, file_path, hints):
            return []

        issues = await run_detection(pipeline_ctx, review_fn=review_fn)
        assert issues == []
        # demo_proj 规则扫描本身有命中，但 llm_only 模式下不产出规则候选
        assert pipeline_ctx.extra["detection"]["rule_issues"] == 0


# ------------------------------------------------------------ enable_rule_hints


class TestRuleHintsSwitch:
    async def test_hints_empty_when_disabled(self, pipeline_ctx):
        """并发路径：enable_rule_hints=False 时所有文件收到的 hints 均为空。"""
        pipeline_ctx.config.enable_rule_hints = False
        hints_log: dict[str, list[str]] = {}
        review_fn = _spy_review_fn(hints_log)

        await run_detection(pipeline_ctx, review_fn=review_fn)

        assert set(hints_log)  # review 通道确实被调用
        assert all(h == [] for h in hints_log.values()), hints_log

    async def test_hints_nonempty_by_default(self, pipeline_ctx):
        """默认（True）：含规则命中的文件收到非空 hints。"""
        hints_log: dict[str, list[str]] = {}
        review_fn = _spy_review_fn(hints_log)

        await run_detection(pipeline_ctx, review_fn=review_fn)

        assert hints_log.get(TARGET), "orders.py 应被 review 通道覆盖"
        assert hints_log[TARGET], "orders.py 含规则命中（如 PY-BARE-EXCEPT@33），hints 不应为空"
        assert any(h.startswith("PY-") for h in hints_log[TARGET])

    async def test_rules_still_reported_when_hints_disabled(self, pipeline_ctx):
        """规则自己报：hints 关闭只影响 prompt 注入，规则结果照常进入候选。"""
        pipeline_ctx.config.enable_rule_hints = False
        review_fn = _spy_review_fn({}, support_batch=False)  # 独立审查便于按文件断言

        issues = await run_detection(pipeline_ctx, review_fn=review_fn)

        info = pipeline_ctx.extra["detection"]
        assert info["rule_issues"] > 0  # 规则结果照常产出
        rule_only = [i for i in issues if i.source == IssueSource.RULE]
        assert rule_only, "规则问题应保留在最终清单"
        # TARGET 文件的 LLM 条目仍在：独立保留（LLM）或与同位置规则命中合并（RULE_LLM）
        target_hits = [
            i
            for i in issues
            if i.file == TARGET and i.source in (IssueSource.LLM, IssueSource.RULE_LLM)
        ]
        assert target_hits, "LLM 条目不应因 hints 关闭而丢失"
        assert any("llm:e1" in i.evidence for i in target_hits)

    async def test_hints_empty_in_batch_slice_path(self, sample_workspace, fake_emitter):
        """小切片批量路径：合并组审查时首个文件与 extra_files 的 hints 均为空。"""
        from audit.llm.base import FakeLLMClient
        from audit.pipeline import PipelineContext

        config = AuditConfig(source_path=str(sample_workspace.src_root), enable_rule_hints=False)
        ctx = PipelineContext(
            config=config,
            workspace=sample_workspace,
            llm=FakeLLMClient(),
            emitter=fake_emitter,
        )
        hints_log: dict[str, list[str]] = {}
        extra_hints_log: list[list[str]] = []
        review_fn = _spy_review_fn(hints_log, extra_hints_log)

        await run_detection(ctx, review_fn=review_fn)

        assert extra_hints_log, "应至少触发一次批量合并审查"
        assert all(h == [] for h in extra_hints_log)
        assert all(h == [] for h in hints_log.values())


# ------------------------------------------------------------- bench 消融配置表


class TestAblationPlan:
    def test_seven_groups_all_real(self):
        plan = plan_ablation()
        assert len(plan) == 7
        assert set(ABLATION_CONFIGS) == {
            "full",
            "−verify",
            "−rule_hints",
            "−symbol_context",
            "−cache",
            "rules_only",
            "llm_only",
        }
        for _name, overrides in plan:
            assert not is_placeholder(overrides)

    def test_is_placeholder_semantics_preserved(self):
        assert not is_placeholder({})
        assert not is_placeholder({"enable_llm_cache": False})
        assert is_placeholder({"_todo": "legacy placeholder"})

    def test_override_keys_are_config_fields(self):
        valid = {f.name for f in dataclasses.fields(AuditConfig)}
        for name, overrides in ABLATION_CONFIGS.items():
            for key in overrides:
                assert key in valid, f"{name} 的键 {key} 不是 AuditConfig 字段"

    @pytest.mark.parametrize(
        ("name", "field_name", "expected"),
        [
            ("−verify", "enable_verify", False),
            ("−rule_hints", "enable_rule_hints", False),
            ("−symbol_context", "enable_symbol_context", False),
            ("−cache", "enable_llm_cache", False),
            ("rules_only", "enable_llm_review", False),
            ("llm_only", "llm_only_mode", True),
        ],
    )
    def test_overrides_apply_to_config(self, name, field_name, expected):
        overrides = ABLATION_CONFIGS[name]
        config = AuditConfig(source_path="x", **overrides)
        assert getattr(config, field_name) is expected
