"""T5 报告渲染自测：Markdown / HTML 结构与落盘。"""

from __future__ import annotations

import json

from audit.models import (
    ArchitectureCard,
    Category,
    FixStatus,
    Issue,
    IssueSource,
    Patch,
    Severity,
)
from audit.models import TestCase as CaseModel
from audit.pipeline import PipelineContext
from audit.report.builder import build_report
from audit.report.render import render_html, render_markdown, write_report

KNOWN_FILE = "app/services/orders.py"
MD_SECTIONS = (
    "项目概览",
    "健康分",
    "问题总表",
    "重点问题详情",
    "架构摘要",
    "Patch 与测试统计",
    "耗时与 Token 统计",
)


def make_issue(severity: Severity = Severity.HIGH, **kw) -> Issue:
    base = dict(
        id="ISS-0001",
        category=Category.BUG,
        severity=severity,
        title="未判空即访问属性",
        file=KNOWN_FILE,
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


def make_report(ctx: PipelineContext):
    ctx.issues = [
        make_issue(
            severity=Severity.CRITICAL,
            id="ISS-0001",
            file=KNOWN_FILE,
            title="未判空即访问属性",
            code_snippet="user = get_user(uid)\nprint(user['name'])",
            evidence=["users.py:41 return None", "call_chain: orders.py:10 -> users.py:41"],
        ),
        make_issue(
            severity=Severity.HIGH,
            id="ISS-0002",
            category=Category.SECURITY,
            file=KNOWN_FILE,
            title="SQL 字符串拼接",
            line_start=11,
            line_end=11,
            code_snippet='query = "SELECT * FROM orders WHERE id = " + order_id',
        ),
        make_issue(severity=Severity.LOW, id="ISS-0003", file="app/utils/mathx.py", title="低级别风格问题"),
    ]
    ctx.patches = [Patch(id="PATCH-0001", issue_id="ISS-0001", diff="--- a/orders.py\n+++ b/orders.py\n@@ -10 +10 @@", rationale="补判空", apply_status="verified", tests_run=2, tests_passed=2)]
    ctx.test_cases = [CaseModel(id="TST-0001", target="get_order_summary", file="tests/generated/test_orders.py", status="passed", kind="normal", assert_count=3)]
    ctx.architecture = ArchitectureCard(
        text="典型的三层 Web 服务结构",
        tech_stack=["Python"],
        modules={"app/services": "业务服务层"},
        hotspots=["app/services/orders.py"],
    )
    return build_report(ctx)


def test_markdown_contains_all_sections(pipeline_ctx: PipelineContext):
    report = make_report(pipeline_ctx)
    md = render_markdown(report)
    for section in MD_SECTIONS:
        assert section in md, f"缺少小节：{section}"
    # 项目概览数据
    assert report.project_name in md
    assert "python" in md
    # 已知 issue 的文件名与 ID
    assert KNOWN_FILE in md
    assert "ISS-0001" in md and "ISS-0003" in md
    # 分级小节
    assert "critical" in md and "high" in md and "low" in md
    # 详情卡内容
    assert "get_user 可能返回 None" in md
    assert "users.py:41 return None" in md
    assert "增加判空分支" in md
    # 架构与统计
    assert "典型的三层 Web 服务结构" in md
    assert "PATCH-0001" in md and "TST-0001" in md


def test_markdown_skips_detail_for_low(pipeline_ctx: PipelineContext):
    """低级别问题只进总表、不生成详情卡标题行。"""
    pipeline_ctx.issues = [make_issue(severity=Severity.LOW, id="ISS-0003", title="仅总表")]
    report = build_report(pipeline_ctx)
    md = render_markdown(report)
    assert "ISS-0003" in md  # 总表仍有
    assert "### ISS-0003" not in md  # 无详情卡
    assert "无 critical / high 级别问题" in md


def test_html_contains_sections_and_highlight(pipeline_ctx: PipelineContext):
    report = make_report(pipeline_ctx)
    html = render_html(report)
    for section in MD_SECTIONS:
        assert section in html, f"缺少小节：{section}"
    assert "<style>" in html  # 内联 CSS
    assert KNOWN_FILE in html
    assert "ISS-0001" in html
    # pygments 高亮产物（noclasses 内联 style）
    assert "get_user" in html
    assert "<span" in html
    # diff 以 <pre> 呈现
    assert "<pre" in html and "orders.py" in html
    assert "PATCH-0001" in html


def test_html_escapes_user_text(pipeline_ctx: PipelineContext):
    """标题中的 HTML 特殊字符必须被转义（防注入）。"""
    pipeline_ctx.issues = [
        make_issue(severity=Severity.CRITICAL, id="ISS-000X", title="<script>alert(1)</script>")
    ]
    from audit.report.builder import build_report

    report = build_report(pipeline_ctx)
    html = render_html(report)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_write_report_creates_three_files(pipeline_ctx: PipelineContext, tmp_path):
    report = make_report(pipeline_ctx)
    out_dir = tmp_path / "out" / "reports"
    paths = write_report(report, out_dir)
    assert set(paths) == {"json", "md", "html"}
    for kind, path in paths.items():
        assert path.exists() and path.stat().st_size > 0, kind
        assert path.parent == out_dir
    # json 可解析且含关键字段
    data = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert data["project_name"] == report.project_name
    assert data["health_score"] == report.health_score
    assert len(data["issues"]) == 3
    assert data["issues"][0]["severity"] == "critical"
