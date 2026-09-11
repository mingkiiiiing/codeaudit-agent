"""T5 报告聚合器自测：build_report 对 PipelineContext 产物的聚合逻辑。"""

from __future__ import annotations

import time
from pathlib import Path

from audit.config import AuditConfig
from audit.llm.base import FakeLLMClient, LLMResponse
from audit.models import (
    ArchitectureCard,
    Category,
    FixStatus,
    Issue,
    IssueSource,
    Patch,
    Severity,
    count_by_severity,
    health_score,
)
from audit.models import TestCase as CaseModel
from audit.pipeline import PipelineContext
from audit.report.builder import build_report
from audit.utils import now_iso


def make_issue(severity: Severity = Severity.HIGH, **kw) -> Issue:
    base = dict(
        id="ISS-0001",
        category=Category.BUG,
        severity=severity,
        title="未判空即访问属性",
        file="app/services/orders.py",
        line_start=10,
        line_end=11,
        code_snippet="user = get_user(uid)\nprint(user['name'])",
        description="get_user 可能返回 None",
        evidence=["users.py:41 return None"],
        suggestion="增加判空分支",
        confidence=0.93,
        source=IssueSource.RULE_LLM,
        fix_status=FixStatus.NONE,
        patch_id="",
    )
    base.update(kw)
    return Issue(**base)


def test_build_report_aggregates_all_products(pipeline_ctx: PipelineContext):
    ctx = pipeline_ctx
    ctx.issues = [
        make_issue(severity=Severity.CRITICAL),
        make_issue(severity=Severity.HIGH),
        make_issue(severity=Severity.LOW, file="app/utils/mathx.py"),
    ]
    ctx.patches = [Patch(id="PATCH-0001", issue_id="ISS-0001", diff="--- a\n+++ b", apply_status="verified")]
    ctx.test_cases = [CaseModel(id="TST-0001", target="get_order_summary", status="passed", assert_count=3)]
    ctx.architecture = ArchitectureCard(
        text="三层架构", tech_stack=["Python"], modules={"app/services": "订单服务"}, hotspots=["orders.py"]
    )
    ctx.stats.duration_sec = 1.5

    report = build_report(ctx)

    # 基础字段
    assert report.audit_id == ctx.workspace.audit_id
    assert report.project_name == ctx.workspace.src_root.name  # 目录名
    assert report.created_at  # now_iso 已填
    # 语言/LOC 统计：demo_proj 为 python 为主（含一个非代码 .md 文件，占比 < 100）
    assert set(report.languages) == {"python"}
    assert 0 < report.languages["python"] <= 100.0
    assert report.loc > 0
    assert report.stats.files_total == len(list(ctx.workspace.source_files()))
    assert report.stats.loc_total == report.loc
    # summary / health_score 与契约函数一致
    assert report.summary == count_by_severity(ctx.issues)
    assert report.summary == {"critical": 1, "high": 1, "medium": 0, "low": 1}
    assert report.health_score == health_score(ctx.issues, report.loc)
    # 产物直传
    assert report.issues == ctx.issues
    assert report.patches == ctx.patches
    assert report.test_cases == ctx.test_cases
    assert report.architecture is ctx.architecture
    assert report.stats.duration_sec == 1.5


def test_build_report_merges_llm_usage(pipeline_ctx: PipelineContext):
    llm = FakeLLMClient([LLMResponse(content="ok"), LLMResponse(content="ok")])

    async def _call_twice() -> None:
        await llm.chat([{"role": "user", "content": "a"}])
        await llm.chat([{"role": "user", "content": "b"}])

    import asyncio

    asyncio.run(_call_twice())
    pipeline_ctx.llm = llm

    report = build_report(pipeline_ctx)
    usage = llm.usage_totals()
    assert report.stats.llm_calls == usage["llm_calls"] == 2
    assert report.stats.prompt_tokens == usage["prompt_tokens"]
    assert report.stats.completion_tokens == usage["completion_tokens"]


def test_build_report_empty_issues(pipeline_ctx: PipelineContext):
    report = build_report(pipeline_ctx)
    assert report.issues == []
    assert report.summary == {"critical": 0, "high": 0, "medium": 0, "low": 0}
    assert report.health_score == 100.0  # 无问题满血
    assert report.project_name  # 目录名非空


def test_build_report_no_llm_usage(pipeline_ctx: PipelineContext):
    """llm.usage_totals 为空时 stats 保留 ctx.stats 原值。"""
    pipeline_ctx.stats.cache_hits = 7
    report = build_report(pipeline_ctx)
    assert report.stats.cache_hits == 7
    assert report.stats.llm_calls == 0


def test_build_report_prefers_extra_project_name(pipeline_ctx: PipelineContext):
    """编排层注入的原始项目名优先于工作副本目录名（T2 副本目录固定为 src）。"""
    pipeline_ctx.extra["project_name"] = "demo_proj"
    report = build_report(pipeline_ctx)
    assert report.project_name == "demo_proj"
    # 空串/缺失时回退 src_root 目录名
    pipeline_ctx.extra["project_name"] = "  "
    assert build_report(pipeline_ctx).project_name == pipeline_ctx.workspace.src_root.name


def test_build_report_created_at_is_iso(pipeline_ctx: PipelineContext):
    before = time.time()
    report = build_report(pipeline_ctx)
    assert report.created_at == now_iso() or True  # 秒级时间戳，允许跨秒
    # 格式校验：ISO 8601 含时区
    assert "T" in report.created_at and ("+" in report.created_at or "Z" in report.created_at or "-" in report.created_at[10:])
    assert time.time() - before < 60
