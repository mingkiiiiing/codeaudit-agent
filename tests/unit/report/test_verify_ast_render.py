"""W24-B 检测质量观测节自测：verify_stats 四态 + ast_wiring 接线的渲染两态。

- 有数据（ctx.extra 带 verify_stats/ast_wiring）→ md/html 渲染九节；
- 无数据（老报告 / 离线 / from_dict 重建）→ 节整体省略（"有则渲染无则省略"）；
- JSON 通道零变化：to_dict 不含新字段。
"""

from __future__ import annotations

import json

import pytest

from audit.models import AuditReport
from audit.pipeline import PipelineContext
from audit.report.builder import build_report
from audit.report.render import render_html, render_markdown

VERIFY_STATS = {"checked": 5, "confirmed": 3, "rejected": 1, "uncertain": 1}
AST_WIRING = {"enabled": True, "parsed": 90, "degraded": 10, "reason": None}


@pytest.fixture
def ctx_with_quality(pipeline_ctx: PipelineContext) -> PipelineContext:
    """注入 verify_stats / ast_wiring（对齐 detect 引擎写入 ctx.extra 的口径）。"""
    pipeline_ctx.extra["verify_stats"] = dict(VERIFY_STATS)
    pipeline_ctx.extra["ast_wiring"] = dict(AST_WIRING)
    return pipeline_ctx


# ---------------------------------------------------------------- 有数据：渲染


def test_markdown_renders_quality_section(ctx_with_quality: PipelineContext) -> None:
    """有数据：md 九节含四态计数与 AST parsed/degraded 及生效率。"""
    report = build_report(ctx_with_quality)
    md = render_markdown(report)
    assert "九、检测质量观测" in md
    assert "Verify Agent 复核" in md
    for token in ("复核数", "确认", "驳回", "存疑"):
        assert token in md, token
    assert "| 5 | 3 | 1 | 1 |" in md  # checked/confirmed/rejected/uncertain
    assert "AST 解析接线" in md
    assert "已启用" in md
    assert "90" in md and "10" in md
    assert "90.0%" in md  # parsed/(parsed+degraded)


def test_html_renders_quality_section(ctx_with_quality: PipelineContext) -> None:
    """有数据：html 同样渲染九节（与 md 同一视图模型）。"""
    report = build_report(ctx_with_quality)
    html = render_html(report)
    assert "九、检测质量观测" in html
    assert "Verify Agent 复核" in html
    assert "90.0%" in html


def test_quality_section_numbering_keeps_duration_last(
    ctx_with_quality: PipelineContext,
) -> None:
    """新节插入后原"耗时与 Token 统计"顺延为第十节，顺序稳定。"""
    report = build_report(ctx_with_quality)
    md = render_markdown(report)
    assert md.index("九、检测质量观测") < md.index("十、耗时与 Token 统计")


# ---------------------------------------------------------------- 无数据：节省略


def test_markdown_omits_quality_section_without_data(pipeline_ctx: PipelineContext) -> None:
    """无数据（离线 / verify 未启用 / 引擎未写 extra）：节整体省略，不残留空标题。"""
    report = build_report(pipeline_ctx)
    md = render_markdown(report)
    html = render_html(report)
    assert "检测质量观测" not in md
    assert "检测质量观测" not in html
    assert "十、耗时与 Token 统计" in md  # 后续节不受影响


def test_old_report_roundtrip_omits_section(ctx_with_quality: PipelineContext) -> None:
    """老报告兼容：to_dict 无新字段；from_dict 重建后渲染时节省略。"""
    report = build_report(ctx_with_quality)
    data = report.to_dict()
    assert "verify_stats" not in data and "ast_wiring" not in data  # JSON 通道零变化
    restored = AuditReport.from_dict(json.loads(json.dumps(data)))
    md = render_markdown(restored)
    assert "检测质量观测" not in md
    assert "十、耗时与 Token 统计" in md


# ---------------------------------------------------------------- 边界形态


def test_ast_disabled_reason_rendered(pipeline_ctx: PipelineContext) -> None:
    """AST 关闭（reason 非空）：如实标注"未启用（原因）"，全降级时生效率 0.0%。"""
    pipeline_ctx.extra["ast_wiring"] = {"enabled": False, "parsed": 0, "degraded": 4, "reason": "解析器不可用"}
    report = build_report(pipeline_ctx)
    md = render_markdown(report)
    assert "九、检测质量观测" in md
    assert "未启用（解析器不可用）" in md
    assert "0.0%" in md


def test_verify_only_or_ast_only_still_renders(pipeline_ctx: PipelineContext) -> None:
    """只有单侧数据：节仍渲染，缺省子块不出现。"""
    pipeline_ctx.extra["verify_stats"] = dict(VERIFY_STATS)
    report = build_report(pipeline_ctx)
    md = render_markdown(report)
    assert "九、检测质量观测" in md
    assert "Verify Agent 复核" in md
    assert "AST 解析接线" not in md
