"""W7-A2 报告「重构方案」章节渲染自测（md/html + builder 聚通 + 占位文案）。"""

from __future__ import annotations

from audit.models import ArchitectureCard, Issue, RefactorProposal
from audit.pipeline import PipelineContext
from audit.report.builder import build_report
from audit.report.render import render_html, render_markdown, write_report


def _proposal(pid: str, kind: str, **kw) -> RefactorProposal:
    base = dict(
        id=pid,
        title=f"方案 {pid}",
        target="app/big.py::process_order",
        kind=kind,
        rationale="函数超 80 行，职责混杂",
        steps=["抽取校验簇", "抽取落库簇"],
        benefits="降低复杂度",
        related_issues=["ISS-0001"],
        source="heuristic+llm",
        confidence=0.72,
    )
    base.update(kw)
    return RefactorProposal(**base)


def _ctx_with(pipeline_ctx: PipelineContext, proposals: list[RefactorProposal]) -> PipelineContext:
    pipeline_ctx.issues = [Issue(id="ISS-0001", title="长函数", file="app/big.py", line_start=1)]
    pipeline_ctx.refactor_proposals = proposals
    pipeline_ctx.architecture = ArchitectureCard(text="分层架构")
    return pipeline_ctx


def test_builder_copies_proposals_to_report(pipeline_ctx: PipelineContext) -> None:
    proposals = [_proposal("REF-0001", "decompose")]
    report = build_report(_ctx_with(pipeline_ctx, proposals))
    assert report.refactor_proposals == proposals


def test_markdown_reconstruction_section_grouped_by_kind(pipeline_ctx: PipelineContext) -> None:
    proposals = [
        _proposal("REF-0001", "decompose"),
        _proposal("REF-0002", "dedup", target="app/dao.py", source="heuristic", confidence=0.65),
    ]
    md = render_markdown(build_report(_ctx_with(pipeline_ctx, proposals)))

    assert "## 六、重构方案" in md
    assert "### 长函数分解（decompose，1 条）" in md
    assert "### 重复模式归并（dedup，1 条）" in md
    assert "#### REF-0001　方案 REF-0001" in md
    assert "**目标**：`app/big.py::process_order`" in md
    assert "启发式+LLM" in md and "启发式" in md
    assert "72%" in md and "65%" in md
    assert "1. 抽取校验簇" in md  # 步骤有序列表
    assert "**关联问题**：ISS-0001" in md
    assert "**收益**：降低复杂度" in md
    # 后续章节顺延编号
    assert "## 七、Patch 与测试统计" in md
    assert "## 八、耗时与 Token 统计" in md


def test_markdown_placeholder_when_no_proposals(pipeline_ctx: PipelineContext) -> None:
    md = render_markdown(build_report(_ctx_with(pipeline_ctx, [])))
    assert "## 六、重构方案" in md
    assert "本次审计未生成重构方案（可配置启用）" in md


def test_html_reconstruction_section_and_placeholder(pipeline_ctx: PipelineContext) -> None:
    report = build_report(_ctx_with(pipeline_ctx, [_proposal("REF-0001", "decompose")]))
    html = render_html(report)
    assert "<h2>六、重构方案</h2>" in html
    assert "REF-0001" in html and "方案 REF-0001" in html
    assert "启发式+LLM" in html
    assert "抽取校验簇" in html

    empty = build_report(_ctx_with(pipeline_ctx, []))
    html_empty = render_html(empty)
    assert "<h2>六、重构方案</h2>" in html_empty
    assert "本次审计未生成重构方案" in html_empty


def test_markdown_related_issues_capped_with_count(pipeline_ctx: PipelineContext) -> None:
    """related_issues 超过 10 个时展示层截断并以「等 N 个」标注（数据保持全量）。"""
    proposals = [_proposal("REF-0001", "dedup", related_issues=[f"ISS-{n:04d}" for n in range(1, 16)])]
    report = build_report(_ctx_with(pipeline_ctx, proposals))
    assert len(report.refactor_proposals[0].related_issues) == 15  # 数据未截断
    md = render_markdown(report)
    assert "ISS-0010" in md
    assert "ISS-0011" not in md
    assert "等 5 个" in md


def test_write_report_roundtrip_includes_proposals(pipeline_ctx: PipelineContext, tmp_path) -> None:
    report = build_report(_ctx_with(pipeline_ctx, [_proposal("REF-0001", "decompose")]))
    paths = write_report(report, tmp_path / "out")
    import json

    data = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert data["refactor_proposals"][0]["id"] == "REF-0001"
    assert "重构方案" in paths["md"].read_text(encoding="utf-8")
    assert "重构方案" in paths["html"].read_text(encoding="utf-8")
