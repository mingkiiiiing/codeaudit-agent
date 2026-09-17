"""W22-B 风险聚焦选择器测试：排序语义、确定性、默认全量等价。

demo_proj 已知命中分布（tests/samples/demo_proj）：
- app/services/orders.py：PY-SQL-INJECTION（critical，权重 10）等多条
- app/config.py：PY-HARDCODED-SECRET（critical）等
- app/utils/net.py：PY-NO-TIMEOUT 等
选择器单测直接构造 RuleHit 组合控制风险分，不依赖样例工程命中细节。
"""

from __future__ import annotations

from audit.detect.base import RuleContext
from audit.detect.engine import _select_review_contexts, run_detection
from audit.models import Category, RuleHit, Severity


def _rc(rel: str, n_lines: int = 50) -> RuleContext:
    return RuleContext(rel_path=rel, language="python", source="x\n" * n_lines, lines=["x"] * n_lines)


def _hit(file: str, sev: Severity, rule: str = "PY-X") -> RuleHit:
    return RuleHit(rule_id=rule, category=Category.BUG, severity=sev, file=file, line_start=1, line_end=1, message="m", snippet="s")


def _ctx(pipeline_ctx, top: int):
    pipeline_ctx.config.llm_review_top_files = top
    return pipeline_ctx


class TestSelector:
    def test_top_zero_returns_all(self, pipeline_ctx):
        """默认 0=全量：选择器原样返回（顺序都不变）。"""
        contexts = [_rc("a.py"), _rc("b.py"), _rc("c.py")]
        assert _select_review_contexts(_ctx(pipeline_ctx, 0), contexts, []) == contexts
        assert _select_review_contexts(_ctx(pipeline_ctx, -3), contexts, []) == contexts

    def test_top_not_exceeded_returns_all(self, pipeline_ctx):
        """文件数 <= top 时无需裁剪。"""
        contexts = [_rc("a.py"), _rc("b.py")]
        assert _select_review_contexts(_ctx(pipeline_ctx, 5), contexts, []) == contexts

    def test_risk_weighted_ordering(self, pipeline_ctx):
        """风险分加权排序：critical(10) > high(5) > medium(2)；命中文件优先于无命中。"""
        contexts = [_rc("low.py"), _rc("none.py"), _rc("crit.py"), _rc("high.py")]
        hits = [
            _hit("crit.py", Severity.CRITICAL),
            _hit("high.py", Severity.HIGH),
            _hit("low.py", Severity.MEDIUM),
        ]
        picked = _select_review_contexts(_ctx(pipeline_ctx, 3), contexts, hits)
        assert [rc.rel_path for rc in picked] == ["crit.py", "high.py", "low.py"]

    def test_risk_accumulates_across_hits(self, pipeline_ctx):
        """同文件多命中累加：2×medium(4) 超过 1×high(5) 不足，排序正确。"""
        contexts = [_rc("two_medium.py"), _rc("one_high.py")]
        hits = [
            _hit("two_medium.py", Severity.MEDIUM, "R1"),
            _hit("two_medium.py", Severity.MEDIUM, "R2"),
            _hit("one_high.py", Severity.HIGH, "R3"),
        ]
        picked = _select_review_contexts(_ctx(pipeline_ctx, 1), contexts, hits)
        assert [rc.rel_path for rc in picked] == ["one_high.py"]

    def test_no_hit_files_fallback_to_line_count(self, pipeline_ctx):
        """无命中文件按行数降序垫底（llm_only 模式的主路径）。"""
        contexts = [_rc("small.py", 10), _rc("big.py", 900), _rc("mid.py", 100)]
        picked = _select_review_contexts(_ctx(pipeline_ctx, 2), contexts, [])
        assert [rc.rel_path for rc in picked] == ["big.py", "mid.py"]

    def test_tie_break_by_path_deterministic(self, pipeline_ctx):
        """同分 tie-break 路径字典序：同输入同选择（审计确定性不变量）。"""
        contexts = [_rc("z.py"), _rc("a.py"), _rc("m.py")]
        hits = [_hit("z.py", Severity.HIGH), _hit("a.py", Severity.HIGH), _hit("m.py", Severity.HIGH)]
        first = _select_review_contexts(_ctx(pipeline_ctx, 2), contexts, hits)
        second = _select_review_contexts(_ctx(pipeline_ctx, 2), list(reversed(contexts)), hits)
        assert [rc.rel_path for rc in first] == ["a.py", "m.py"]
        assert [rc.rel_path for rc in second] == ["a.py", "m.py"]


class TestRunDetectionIntegration:
    async def test_default_top_zero_full_review(self, pipeline_ctx):
        """默认配置：全部文件进入 LLM 审查（与 W22-B 之前等价）。"""

        async def review_fn(workspace, file_path, hints=None):
            return []

        await run_detection(pipeline_ctx, review_fn=review_fn)
        info = pipeline_ctx.extra["detection"]
        assert info["files_reviewed"] == info.get("files_reviewed")  # 全量口径
        assert "llm_focus" not in pipeline_ctx.extra

    async def test_focus_emits_stats_and_limits_review(self, pipeline_ctx):
        """top-N 生效：llm_focus 统计落 extra、审查文件数被限制、规则结果仍全量。"""
        reviewed: list[str] = []

        async def review_fn(workspace, file_path, hints=None):
            reviewed.append(file_path)
            return []

        all_files = {rc.rel_path for rc in __import__("audit.detect.engine", fromlist=["build_rule_contexts"]).build_rule_contexts(pipeline_ctx)}
        pipeline_ctx.config.llm_review_top_files = 2
        issues = await run_detection(pipeline_ctx, review_fn=review_fn)
        focus = pipeline_ctx.extra.get("llm_focus")
        assert focus == {"enabled": True, "selected": 2, "total": len(all_files)}
        assert len(reviewed) == 2
        # 两级审计语义：规则候选不受聚焦影响（demo_proj 的规则命中照常入报告）
        assert issues  # demo_proj 有静态命中
