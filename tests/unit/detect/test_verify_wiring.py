"""W2-A3 单测：run_detection 的 Verify 复核接线与并发审查集成（契约 v1.2）。

- verify_fn 接入：critical/high 逐条复核，false_positive（confidence<=0）从最终
  ctx.issues 剔除但保留在 ctx.candidates；uncertain 降置信度保留；
  统计写 ctx.extra["verify_stats"]；
- verify_fn=None 时与旧行为完全一致（回归）；
- review_fn 非空且文件数 >1 时走 review_files_parallel 文件级并发。
"""

from __future__ import annotations

import asyncio
from typing import Any

from audit.detect.engine import run_detection
from audit.models import Category, Issue, IssueSource, Severity

CRITICAL_TITLE = "订单金额未校验即入库（critical 目标）"
MEDIUM_TITLE = "配置项建议收敛为枚举（medium 不复核）"


def _llm_issue(
    title: str,
    file: str,
    line: int,
    severity: Severity,
    category: Category = Category.PERFORMANCE,
) -> Issue:
    return Issue(
        id="",
        category=category,
        severity=severity,
        title=title,
        file=file,
        line_start=line,
        line_end=line,
        description="LLM 审查通道产出的候选",
        evidence=[f"{file}:{line} 证据"],
        suggestion="建议处理",
        confidence=0.9,
        source=IssueSource.LLM,
    )


def _review_fn_factory(extra_state: dict[str, Any] | None = None):
    """按文件注入候选：orders.py 注入 critical + medium，其余文件为空。"""

    async def review_fn(workspace, file_path, hints):
        if extra_state is not None:
            extra_state["reviewed"].append(file_path)
        if file_path == "app/services/orders.py":
            return [
                _llm_issue(CRITICAL_TITLE, "app/services/orders.py", 39, Severity.CRITICAL),
                _llm_issue(MEDIUM_TITLE, "app/config.py", 5, Severity.MEDIUM),
            ]
        return []

    return review_fn


def _rejection_verify_fn(calls: list[str], target_title: str = CRITICAL_TITLE):
    """假 verify_fn：目标问题置 confidence=0（驳回），其余原样确认（兼容 async）。"""

    async def verify_fn(workspace, issue):
        calls.append(issue.title)
        out = Issue.from_dict(issue.to_dict())
        if issue.title == target_title:
            out.confidence = 0.0
            out.description = (out.description + "\n[Verify 驳回] 所有调用方均已防护").strip()
        return out

    return verify_fn


class TestVerifyWiring:
    async def test_rejected_critical_removed_from_issues_kept_in_candidates(self, pipeline_ctx):
        """critical 被置 confidence=0 → ctx.issues 剔除、ctx.candidates 保留、统计正确。"""
        calls: list[str] = []
        issues = await run_detection(
            pipeline_ctx,
            review_fn=_review_fn_factory(),
            verify_fn=_rejection_verify_fn(calls),
        )
        ctx = pipeline_ctx
        final_titles = [i.title for i in ctx.issues]
        candidate_titles = [i.title for i in ctx.candidates]

        # 驳回的 critical：不在最终清单，但保留在候选（供误报率统计）
        assert CRITICAL_TITLE not in final_titles
        assert CRITICAL_TITLE in candidate_titles
        assert issues is ctx.issues

        # medium 不复核：不进 verify 调用，直接保留在最终清单
        assert MEDIUM_TITLE not in calls
        assert MEDIUM_TITLE in final_titles

        stats = ctx.extra["verify_stats"]
        assert stats["rejected"] == 1
        assert stats["checked"] >= 1
        assert stats["checked"] == stats["confirmed"] + stats["rejected"] + stats["uncertain"]
        # 只有 critical/high 才被复核
        assert all(t != MEDIUM_TITLE for t in calls)

    async def test_uncertain_kept_with_capped_confidence(self, pipeline_ctx):
        """uncertain 保留且 confidence 取 min(原值, 0.5)（verify_issue 语义在引擎中生效）。"""
        high_issue = _llm_issue("高危但证据不足", "app/services/orders.py", 20, Severity.HIGH)

        async def review_fn(workspace, file_path, hints):
            if file_path == "app/services/orders.py":
                return [high_issue]
            return []

        async def verify_fn(workspace, issue):
            out = Issue.from_dict(issue.to_dict())
            if issue.title == "高危但证据不足":
                out.confidence = 0.4
                out.description = (out.description + "\n[Verify:uncertain] 缺少调用方信息").strip()
            return out

        await run_detection(pipeline_ctx, review_fn=review_fn, verify_fn=verify_fn)
        kept = [i for i in pipeline_ctx.issues if i.title == "高危但证据不足"]
        assert len(kept) == 1
        assert kept[0].confidence == 0.4
        assert pipeline_ctx.extra["verify_stats"]["uncertain"] == 1
        assert pipeline_ctx.extra["verify_stats"]["rejected"] == 0

    async def test_sync_verify_fn_supported(self, pipeline_ctx):
        """verify_fn 兼容同步实现。"""

        def verify_fn(workspace, issue):
            out = Issue.from_dict(issue.to_dict())
            if issue.title == CRITICAL_TITLE:
                out.confidence = 0.0
            return out

        await run_detection(pipeline_ctx, review_fn=_review_fn_factory(), verify_fn=verify_fn)
        assert pipeline_ctx.extra["verify_stats"]["rejected"] == 1
        assert all(i.title != CRITICAL_TITLE for i in pipeline_ctx.issues)

    async def test_verify_fn_none_matches_legacy_behavior(self, pipeline_ctx):
        """回归：verify_fn=None 时 issues == candidates，无 verify_stats。"""
        issues = await run_detection(pipeline_ctx, review_fn=_review_fn_factory())
        ctx = pipeline_ctx
        assert [i.title for i in ctx.issues] == [i.title for i in ctx.candidates]
        assert CRITICAL_TITLE in [i.title for i in ctx.issues]  # 不复核即保留
        assert "verify_stats" not in ctx.extra
        assert issues is ctx.issues
        assert ctx.extra["detection"]["mode"] == "rule+llm"

    async def test_verify_error_keeps_candidate(self, pipeline_ctx):
        """verify_fn 抛异常时保底保留候选并记录 verify_errors。"""

        async def review_fn(workspace, file_path, hints):
            if file_path == "app/services/orders.py":
                return [_llm_issue(CRITICAL_TITLE, "app/services/orders.py", 39, Severity.CRITICAL)]
            return []

        async def verify_fn(workspace, issue):
            raise RuntimeError("verify down")

        await run_detection(pipeline_ctx, review_fn=review_fn, verify_fn=verify_fn)
        assert any(i.title == CRITICAL_TITLE for i in pipeline_ctx.issues)
        assert pipeline_ctx.extra["verify_stats"]["checked"] == 0
        assert any("verify down" in repr(e) for e in pipeline_ctx.extra["verify_errors"].values())

    async def test_engine_uses_parallel_review_for_multi_files(self, pipeline_ctx):
        """文件数 >1 时走 review_files_parallel：并发峰值受 config.concurrency 约束。"""
        pipeline_ctx.config.concurrency = 2
        state: dict[str, Any] = {"inflight": 0, "peak": 0}

        async def review_fn(workspace, file_path, hints):
            state["inflight"] += 1
            state["peak"] = max(state["peak"], state["inflight"])
            await asyncio.sleep(0.005)
            state["inflight"] -= 1
            if file_path == "app/services/orders.py":
                return [_llm_issue(CRITICAL_TITLE, "app/services/orders.py", 39, Severity.CRITICAL)]
            return []

        await run_detection(pipeline_ctx, review_fn=review_fn)
        assert state["peak"] <= 2
        info = pipeline_ctx.extra["detection"]
        assert info["mode"] == "rule+llm"
        assert info["files_reviewed"] >= 9  # demo_proj 全部 9 个源文件
        assert any(i.title == CRITICAL_TITLE for i in pipeline_ctx.issues)
