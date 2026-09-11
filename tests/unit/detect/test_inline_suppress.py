"""行内抑制（`# codeaudit: ignore[ID]`，W4-A2）单测：filter_suppressed 三态 + run_detection 集成。"""

from __future__ import annotations

from audit.detect.base import RuleContext
from audit.detect.engine import filter_suppressed, run_detection
from audit.models import Category, Issue, IssueSource, RuleHit, Severity


def make_hit(rule_id: str = "PY-SQL-INJECTION", **kw) -> RuleHit:
    base = dict(
        rule_id=rule_id,
        category=Category.SECURITY,
        severity=Severity.CRITICAL,
        file="m.py",
        line_start=2,
        line_end=2,
        message="检出问题",
        snippet="x = 1",
        meta={},
    )
    base.update(kw)
    return RuleHit(**base)


def make_ctx(source: str, rel_path: str = "m.py") -> RuleContext:
    lines = source.splitlines()
    return RuleContext(rel_path=rel_path, language="python", source=source, lines=lines)


# ---------------------------------------------------------------- 三态单测


class TestFilterSuppressed:
    def test_bare_ignore_drops_all_rules_on_that_line(self):
        lines = ["x = 1", "y = eval(s)  # codeaudit: ignore", "z = 3"]
        hits = [
            make_hit(rule_id="PY-SQL-INJECTION", line_start=2, line_end=2),
            make_hit(rule_id="PY-BARE-EXCEPT", line_start=2, line_end=2),
            make_hit(rule_id="PY-EVAL-EXEC", line_start=3, line_end=3),
        ]
        kept = filter_suppressed(hits, lines)
        assert [h.rule_id for h in kept] == ["PY-EVAL-EXEC"]  # 第 2 行全部抑制，第 3 行保留

    def test_id_ignore_drops_only_listed_rules(self):
        lines = [
            "x = 1",
            "q = 'SELECT ' + col  # codeaudit: ignore[PY-SQL-INJECTION, PY-BARE-EXCEPT]",
        ]
        hits = [
            make_hit(rule_id="PY-SQL-INJECTION", line_start=2, line_end=2),
            make_hit(rule_id="PY-BARE-EXCEPT", line_start=2, line_end=2),
            make_hit(rule_id="PY-EQ-NONE", line_start=2, line_end=2),  # 不在列表 → 保留
            make_hit(rule_id="PY-SQL-INJECTION", line_start=1, line_end=1),  # 别的行 → 保留
        ]
        kept = filter_suppressed(hits, lines)
        assert [(h.rule_id, h.line_start) for h in kept] == [("PY-EQ-NONE", 2), ("PY-SQL-INJECTION", 1)]

    def test_ids_with_spaces_and_case_insensitive_keyword(self):
        lines = ["q = 'SELECT ' + col  # CODEAUDIT: IGNORE[ PY-SQL-INJECTION ,  PY-EQ-NONE ]"]
        hits = [make_hit(rule_id="PY-SQL-INJECTION", line_start=1, line_end=1)]
        assert filter_suppressed(hits, lines) == []

    def test_unrelated_comment_keeps_hit(self):
        lines = ["x = 1  # 普通注释", "y = 2  # codeaudit: ignored 不是抑制注释", "q = 'SELECT ' + col"]
        hits = [make_hit(rule_id="PY-SQL-INJECTION", line_start=3, line_end=3)]
        assert filter_suppressed(hits, lines) == hits  # "ignored" 不触发（词边界）

    def test_span_hit_suppressed_by_any_line_in_range(self):
        lines = ["def f():", "    try:", "    # codeaudit: ignore", "        q = 'SELECT ' + col", "    return 1"]
        hits = [
            make_hit(rule_id="PY-SQL-INJECTION", line_start=2, line_end=4),
            make_hit(rule_id="PY-SQL-INJECTION", line_start=5, line_end=5),
        ]
        kept = filter_suppressed(hits, lines)
        # 命中区间 2-4 覆盖第 3 行（含 ignore）→ 抑制；命中第 5 行 → 保留
        assert [h.line_start for h in kept] == [5]

    def test_no_annotation_returns_hits_unchanged_in_order(self):
        lines = ["x = 1", "y = 2"]
        hits = [make_hit(line_start=1, line_end=1), make_hit(rule_id="PY-EQ-NONE", line_start=2, line_end=2)]
        kept = filter_suppressed(hits, lines)
        assert kept == hits

    def test_empty_inputs(self):
        assert filter_suppressed([], ["# codeaudit: ignore"]) == []
        assert filter_suppressed([make_hit()], []) == [make_hit()]

    def test_line_start_zero_clamped_to_first_line(self):
        lines = ["# codeaudit: ignore"]
        hit = make_hit(line_start=0, line_end=1)
        # 非法行号 0 不崩溃：区间 clamp 到第 1 行，按该行的裸 ignore 抑制
        assert filter_suppressed([hit], lines) == []


# ---------------------------------------------------------------- run_detection 集成


class TestRunDetectionInlineSuppress:
    async def test_ignore_id_removes_sql_hit_but_keeps_others(self, pipeline_ctx):
        """给 demo_proj 的 SQL 拼接行（orders.py:11）加 ignore[ID]：该规则 hit 消失、其他保留。"""
        orders = pipeline_ctx.workspace.abs_path("app/services/orders.py")
        lines = orders.read_text(encoding="utf-8").splitlines()
        assert "SELECT * FROM orders" in lines[10]  # 1-based 第 11 行
        lines[10] = lines[10] + "  # codeaudit: ignore[PY-SQL-INJECTION]"
        orders.write_text("\n".join(lines) + "\n", encoding="utf-8")

        issues = await run_detection(pipeline_ctx, review_fn=None)
        hits = pipeline_ctx.rule_hits
        orders_hits = [h for h in hits if h.file == "app/services/orders.py"]

        # PY-SQL-INJECTION 的 hit 被抑制（原 G2/G12 命中于第 11 行）
        assert [h for h in orders_hits if h.rule_id == "PY-SQL-INJECTION"] == []
        # 其他规则命中保留：裸 except（33 行）、list 成员判断（20 行）、open 未关闭（23 行）
        kept_ids = {h.rule_id for h in orders_hits}
        assert {"PY-BARE-EXCEPT", "PY-LIST-MEMBERSHIP", "PY-OPEN-NO-CLOSE"} <= kept_ids
        # Issue 通道同样不再产出该问题
        assert all(
            not (i.file == "app/services/orders.py" and "SQL" in i.title) for i in issues
        )

    async def test_bare_ignore_removes_bare_except_hit(self, pipeline_ctx):
        orders = pipeline_ctx.workspace.abs_path("app/services/orders.py")
        lines = orders.read_text(encoding="utf-8").splitlines()
        assert lines[32].strip() == "except:  # line 33: 裸 except 吞掉所有异常"
        lines[32] = lines[32] + "  # codeaudit: ignore"
        orders.write_text("\n".join(lines) + "\n", encoding="utf-8")

        await run_detection(pipeline_ctx, review_fn=None)
        orders_hits = [h for h in pipeline_ctx.rule_hits if h.file == "app/services/orders.py"]
        assert [h for h in orders_hits if h.rule_id == "PY-BARE-EXCEPT"] == []
        assert any(h.rule_id == "PY-SQL-INJECTION" for h in orders_hits)  # 其他行不受影响

    async def test_llm_results_not_filtered(self, pipeline_ctx):
        """llm_only 模式：LLM 返回落在带抑制注释行上的 Issue 不经 filter_suppressed。"""
        orders = pipeline_ctx.workspace.abs_path("app/services/orders.py")
        lines = orders.read_text(encoding="utf-8").splitlines()
        lines[32] = lines[32] + "  # codeaudit: ignore"
        orders.write_text("\n".join(lines) + "\n", encoding="utf-8")

        pipeline_ctx.config.llm_only_mode = True

        async def review_fn(workspace, file_path, hints):
            if file_path != "app/services/orders.py":
                return []
            return [
                Issue(
                    id="",
                    category=Category.BUG,
                    severity=Severity.MEDIUM,
                    title="裸 except 吞掉所有异常",
                    file=file_path,
                    line_start=33,
                    line_end=33,
                    description="描述",
                    confidence=0.9,
                    source=IssueSource.LLM,
                )
            ]

        issues = await run_detection(pipeline_ctx, review_fn=review_fn)
        assert pipeline_ctx.rule_hits == []  # llm_only：规则通道整体关闭
        assert any(i.line_start == 33 and i.source == IssueSource.LLM for i in issues)  # LLM 结果保留
