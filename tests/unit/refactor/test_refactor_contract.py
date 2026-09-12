"""契约 v1.7 自测：RefactorProposal 序列化回环与存量兼容（纯追加默认字段）。

保证：旧 JSON（无 refactor_proposals 键）反序列化不受影响；
to_dict/from_dict 对 RefactorProposal 及其容器字段无损回环。
"""

from __future__ import annotations

from audit.models import AuditReport, Issue, RefactorProposal
from audit.pipeline import PipelineContext


def _proposal(**kw) -> RefactorProposal:
    base = dict(
        id="REF-0001",
        title="分解长函数 process_order",
        target="app/big.py::process_order",
        kind="decompose",
        rationale="函数超 80 行，职责混杂",
        steps=["抽取校验簇", "抽取落库簇", "保留编排入口"],
        benefits="降低复杂度，便于测试复用",
        related_issues=["ISS-0001", "ISS-0002"],
        source="heuristic+llm",
        confidence=0.72,
    )
    base.update(kw)
    return RefactorProposal(**base)


def test_refactor_proposal_roundtrip():
    p = _proposal()
    d = p.to_dict()
    assert d["kind"] == "decompose"
    assert d["source"] == "heuristic+llm"
    assert d["steps"] == ["抽取校验簇", "抽取落库簇", "保留编排入口"]
    assert RefactorProposal.from_dict(d) == p


def test_refactor_proposal_partial_from_dict():
    p = RefactorProposal.from_dict({"id": "REF-0002", "title": "t"})
    assert p.kind == "other"  # 缺省字段走默认值
    assert p.steps == []
    assert p.source == "heuristic"
    assert p.confidence == 0.0


def test_audit_report_carries_proposals_roundtrip():
    report = AuditReport(
        audit_id="abc",
        project_name="demo",
        issues=[Issue(id="ISS-0001", title="t")],
        refactor_proposals=[_proposal()],
    )
    restored = AuditReport.from_dict(report.to_dict())
    assert restored.refactor_proposals == [_proposal()]
    assert restored.issues[0].id == "ISS-0001"


def test_old_report_json_without_proposals_still_loads():
    """存量兼容：v1.6 时代的 report JSON（无 refactor_proposals 键）反序列化不崩。"""
    old = {
        "audit_id": "legacy",
        "project_name": "old-proj",
        "issues": [{"id": "ISS-0001", "title": "t"}],
    }
    report = AuditReport.from_dict(old)
    assert report.refactor_proposals == []
    assert report.issues[0].id == "ISS-0001"


def test_pipeline_context_default_empty():
    """PipelineContext 默认字段：新建 ctx 的 refactor_proposals 为空列表。"""
    from audit.config import AuditConfig
    from audit.llm.base import FakeLLMClient
    from audit.workspace import WorkspaceContext

    ws = WorkspaceContext(audit_id="x", src_root=".", work_root=".", db_path="i.db")
    ctx = PipelineContext(
        config=AuditConfig(source_path="."), workspace=ws, llm=FakeLLMClient(), emitter=None
    )  # type: ignore[arg-type]
    assert ctx.refactor_proposals == []
