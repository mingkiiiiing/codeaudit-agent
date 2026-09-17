"""检测流水线（engine）测试：上下文构建、行号校验、转换、去重与总入口。"""

from __future__ import annotations

import pytest

from audit.detect.base import RuleContext
from audit.detect.engine import (
    build_rule_contexts,
    dedup_issues,
    hits_to_issues,
    run_detection,
    run_rules,
    validate_issue_lines,
)
from audit.models import Category, Issue, IssueSource, RuleHit, Severity

EXPECTED_FILES = {
    "main.py",
    "app/__init__.py",
    "app/config.py",
    "app/services/__init__.py",
    "app/services/orders.py",
    "app/services/users.py",
    "app/utils/__init__.py",
    "app/utils/mathx.py",
    "app/utils/net.py",
}


def make_hit(**kw) -> RuleHit:
    base = dict(
        rule_id="PY-EQ-NONE",
        category=Category.BUG,
        severity=Severity.LOW,
        file="m.py",
        line_start=11,
        line_end=11,
        message="第 11 行使用 == 与 None 比较，应使用 is None。",
        snippet="if a == None:",
        meta={},
    )
    base.update(kw)
    return RuleHit(**base)


def make_issue(**kw) -> Issue:
    base = dict(
        id="",
        category=Category.BUG,
        severity=Severity.HIGH,
        title="LLM 发现的问题",
        file="m.py",
        line_start=11,
        line_end=11,
        description="描述",
        evidence=["llm:e1"],
        confidence=0.9,
        source=IssueSource.LLM,
    )
    base.update(kw)
    return Issue(**base)


# ---------------------------------------------------------------- 上下文构建


class TestBuildContexts:
    def test_collects_only_source_files(self, pipeline_ctx):
        contexts = build_rule_contexts(pipeline_ctx)
        rels = {rc.rel_path for rc in contexts}
        assert rels == EXPECTED_FILES
        # README.md / GOLDEN_ISSUES.md 不在源文件内
        assert all(rc.language == "python" for rc in contexts)

    def test_lines_align_and_meta_ready(self, pipeline_ctx):
        contexts = {rc.rel_path: rc for rc in build_rule_contexts(pipeline_ctx)}
        rc = contexts["app/utils/mathx.py"]
        assert rc.lines[3] == "def accumulate(items, bucket=[]):  # line 4: 可变默认参数"
        assert rc.source.splitlines() == rc.lines
        assert rc.meta.get("pyscan") is not None  # 预计算掩码
        assert rc.symbols == []  # 无索引时为空
        # P0-3 AST 接线：接线开启时 python 文件解析 tree（无解析器/失败降级 None 不阻断）
        wiring = pipeline_ctx.extra.get("ast_wiring") or {}
        if wiring.get("enabled"):
            assert wiring.get("degraded") == 0
            assert rc.tree is not None
        else:
            assert rc.tree is None

    def test_symbols_from_fake_index(self, pipeline_ctx):
        from audit.models import Symbol

        class FakeIndex:
            def symbols_for_file(self, rel_path: str) -> list[Symbol]:
                return [Symbol(name="f", kind="function", file=rel_path, line_start=1, line_end=2)]

        pipeline_ctx.index = FakeIndex()
        contexts = {rc.rel_path: rc for rc in build_rule_contexts(pipeline_ctx)}
        assert contexts["app/utils/mathx.py"].symbols[0].name == "f"

    def test_broken_index_is_tolerated(self, pipeline_ctx):
        class BrokenIndex:
            def symbols_for_file(self, rel_path: str) -> list:
                raise RuntimeError("index not ready")

        pipeline_ctx.index = BrokenIndex()
        contexts = build_rule_contexts(pipeline_ctx)
        assert len(contexts) == len(EXPECTED_FILES)


# ---------------------------------------------------------------- 行号校验


class TestValidateIssueLines:
    def test_valid(self):
        issue = make_issue(line_start=2, line_end=3)
        assert validate_issue_lines(issue, ["a", "b", "c"]) is True

    def test_out_of_range(self):
        issue = make_issue(line_start=4, line_end=4)
        assert validate_issue_lines(issue, ["a", "b", "c"]) is False

    def test_reversed_range(self):
        issue = make_issue(line_start=3, line_end=1)
        assert validate_issue_lines(issue, ["a", "b", "c"]) is False

    def test_zero_line(self):
        issue = make_issue(line_start=0, line_end=0)
        assert validate_issue_lines(issue, ["a"]) is False

    def test_empty_file(self):
        issue = make_issue(line_start=1, line_end=1)
        assert validate_issue_lines(issue, []) is False


# ---------------------------------------------------------------- 转换与去重


class TestHitsToIssues:
    def test_field_mapping_and_ids(self):
        issues = hits_to_issues([make_hit()])
        issue = issues[0]
        assert issue.id == "ISS-0001"
        assert issue.source == IssueSource.RULE
        assert issue.category == Category.BUG
        assert issue.severity == Severity.LOW
        assert issue.confidence == pytest.approx(0.7)  # meta 缺省 0.7
        assert issue.file == "m.py"
        assert issue.line_start == 11
        assert issue.title.startswith("第 11 行")
        assert "PY-EQ-NONE" in issue.description
        assert any(e.startswith("rule:PY-EQ-NONE") for e in issue.evidence)
        assert issue.suggestion

    def test_confidence_from_meta_and_id_start(self):
        issues = hits_to_issues([make_hit(meta={"confidence": 0.85})], id_start=5)
        assert issues[0].id == "ISS-0005"
        assert issues[0].confidence == pytest.approx(0.85)


class TestDedupIssues:
    def test_merge_same_location_same_category(self):
        a = make_issue(
            id="ISS-0001", source=IssueSource.RULE, confidence=0.7,
            line_start=10, line_end=12, evidence=["rule:X"],
        )
        b = make_issue(
            id="ISS-0002", source=IssueSource.LLM, confidence=0.9,
            line_start=13, line_end=13, evidence=["llm:Y"],
        )
        merged = dedup_issues([a, b])
        assert len(merged) == 1
        m = merged[0]
        assert m.id == "ISS-0002"  # 置信度高者为主体
        assert m.source == IssueSource.RULE_LLM  # 跨源升级
        assert m.confidence == pytest.approx(1.0)  # 0.9 + 0.1 封顶
        assert "rule:X" in m.evidence and "llm:Y" in m.evidence

    def test_no_merge_across_category_or_file(self):
        a = make_issue(category=Category.BUG, file="a.py", line_start=10, line_end=10)
        b = make_issue(category=Category.STYLE, file="a.py", line_start=10, line_end=10)
        c = make_issue(category=Category.BUG, file="b.py", line_start=10, line_end=10)
        merged = dedup_issues([a, b, c])
        assert len(merged) == 3

    def test_no_merge_beyond_tolerance(self):
        a = make_issue(line_start=10, line_end=10)
        b = make_issue(line_start=20, line_end=20)
        assert len(dedup_issues([a, b])) == 2

    def test_rule_rule_merge_keeps_rule_source(self):
        a = make_issue(source=IssueSource.RULE, confidence=0.7, line_start=30, line_end=30)
        b = make_issue(source=IssueSource.RULE, confidence=0.6, line_start=31, line_end=31)
        merged = dedup_issues([a, b])
        assert len(merged) == 1
        assert merged[0].source == IssueSource.RULE
        assert merged[0].confidence == pytest.approx(0.7)


# ---------------------------------------------------------------- 总入口


class TestRunDetection:
    async def test_pure_rule_mode(self, pipeline_ctx):
        issues = await run_detection(pipeline_ctx, review_fn=None)
        assert issues, "纯规则模式必须产出问题"
        assert pipeline_ctx.rule_hits
        assert pipeline_ctx.candidates == pipeline_ctx.issues
        assert all(i.source == IssueSource.RULE for i in pipeline_ctx.issues)
        info = pipeline_ctx.extra["detection"]
        assert info["mode"] == "rule"
        assert info["rule_issues"] == len(pipeline_ctx.issues)

    async def test_review_fn_injection(self, pipeline_ctx):
        async def review_fn(workspace, file_path, hints):
            assert isinstance(hints, list)
            if file_path != "app/services/orders.py":
                return []
            return [
                # 与规则命中（PY-BARE-EXCEPT @33）同位置同类 → 合并
                make_issue(
                    category=Category.BUG, file=file_path,
                    line_start=33, line_end=33,
                    title="裸 except 会吞掉所有异常", confidence=0.9,
                ),
                # 越界行号 → 必须被过滤
                make_issue(category=Category.BUG, file=file_path, line_start=999, line_end=999),
                # 独立问题 → 保留
                make_issue(
                    category=Category.STYLE, file=file_path,
                    line_start=5, line_end=5, title="缺类型标注",
                ),
            ]

        issues = await run_detection(pipeline_ctx, review_fn=review_fn)
        info = pipeline_ctx.extra["detection"]
        assert info["mode"] == "rule+llm"
        assert info["llm_filtered"] == 1
        assert info["files_reviewed"] >= 1

        # 越界问题被拦截
        assert all(i.line_start < 900 for i in issues)
        # 同位置合并：source 升级为 rule+llm
        merged = [i for i in issues if i.file == "app/services/orders.py" and i.line_start == 33]
        assert merged and merged[0].source == IssueSource.RULE_LLM
        # 独立 LLM 问题保留
        kept = [i for i in issues if i.line_start == 5 and i.file == "app/services/orders.py"]
        assert kept and kept[0].source == IssueSource.LLM

    async def test_review_fn_sync_and_error_tolerant(self, pipeline_ctx):
        def review_fn(workspace, file_path, hints):
            if file_path == "app/config.py":
                raise RuntimeError("llm down")
            return []

        issues = await run_detection(pipeline_ctx, review_fn=review_fn)
        assert issues  # LLM 故障不影响规则结果
        assert "app/config.py" in pipeline_ctx.extra["review_errors"]

    async def test_budget_truncation(self, pipeline_ctx):
        # 生成 2200 行 assert 的文件 -> ASSERT-IN-PROD 命中 2200 次，触发截断保护
        bulk = "\n".join(f"assert i_{n} is not None" for n in range(2200))
        target = pipeline_ctx.workspace.abs_path("bulk_checks.py")
        target.write_text(bulk, encoding="utf-8")

        issues = await run_detection(pipeline_ctx, review_fn=None)
        from audit.detect.engine import MAX_ISSUES

        assert len(issues) == MAX_ISSUES
        info = pipeline_ctx.extra["detection"]
        assert info["rule_issues"] >= 2200  # demo_proj 自身命中 + 2200 条 assert
        assert info["truncated_from"] == info["rule_issues"]

    async def test_disabled_rules_switch(self, pipeline_ctx):
        pipeline_ctx.extra["disabled_rules"] = ["PY-PRINT-DEBUG", "PY-HARDCODED-URL"]
        run_rules(pipeline_ctx)
        ids = {h.rule_id for h in pipeline_ctx.rule_hits}
        assert "PY-PRINT-DEBUG" not in ids
        assert "PY-HARDCODED-URL" not in ids
        assert "PY-BARE-EXCEPT" in ids  # 其他规则不受影响
