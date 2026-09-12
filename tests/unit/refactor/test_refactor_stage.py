"""W7-A2 refactor 阶段入口自测：聚合 → 可选 LLM 增强 → 写 ctx → emit 事件。

覆盖（docs/12 §3 契约 v1.7）：
- 事件 stage="refactor"、message="重构方案：N 条"（不含干扰 Web 阶段判定的关键字）；
- ctx.refactor_proposals / ctx.extra["refactor_stats"] 正确落位；
- enable_llm_review + api_key 时走增强分支（json_mode），否则零 LLM 调用；
- 干净项目（无方案）阶段照常收尾，不崩。
"""

from __future__ import annotations

import json
from pathlib import Path

from audit.config import AuditConfig
from audit.llm.base import FakeLLMClient
from audit.models import Category, Issue, IssueSource, Severity
from audit.pipeline import PipelineContext
from audit.refactor.stage import run_refactor_stage
from audit.workspace import WorkspaceContext


def _make_ctx(tmp_path: Path, llm: FakeLLMClient, **cfg) -> tuple[PipelineContext, list[dict]]:
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    ws = WorkspaceContext(
        audit_id="stage001", src_root=src, work_root=tmp_path / "work", db_path=tmp_path / "i.db"
    )
    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)

    config = AuditConfig(source_path=str(src), **cfg)
    ctx = PipelineContext(config=config, workspace=ws, llm=llm, emitter=emit)
    return ctx, events


def _dedup_issues() -> list[Issue]:
    issues = []
    for n, ln in enumerate((10, 22, 35), 1):
        issues.append(
            Issue(
                id=f"ISS-{n:04d}",
                category=Category.BUG,
                severity=Severity.HIGH,
                title="裸 except 吞掉所有异常",
                file="app/handlers.py",
                line_start=ln,
                line_end=ln,
                evidence=["rule:PY-BARE-EXCEPT", f"loc:app/handlers.py:{ln}-{ln}"],
                confidence=0.7,
                source=IssueSource.RULE,
            )
        )
    return issues


async def test_stage_writes_proposals_stats_and_event(tmp_path: Path) -> None:
    ctx, events = _make_ctx(tmp_path, FakeLLMClient(), enable_llm_review=False)
    ctx.issues = _dedup_issues()

    await run_refactor_stage(ctx)

    assert len(ctx.refactor_proposals) == 1
    assert ctx.refactor_proposals[0].kind == "dedup"
    assert ctx.extra["refactor_stats"] == {
        "proposals": 1,
        "enhanced": 0,
        "degraded": 0,
        "llm_targets": 0,
    }
    refactor_events = [e for e in events if e.get("stage") == "refactor"]
    assert any(e["message"] == "重构方案：1 条" for e in refactor_events)
    summary = refactor_events[-1]
    assert summary["total"] == 1
    # 文案不含「跳过/skipped」等会干扰 Web 阶段判定的关键字
    assert all("跳过" not in e["message"] and "skipped" not in e["message"] for e in refactor_events)


async def test_stage_llm_enhancement_path(tmp_path: Path) -> None:
    """enable_llm_review + api_key → 增强分支：json_mode 调用、source 升级、统计就位。"""
    payload = {"rationale": "增强理由", "steps": ["统一封装 except 语义"], "benefit": "收益一句话"}
    llm = FakeLLMClient([{"content": json.dumps(payload, ensure_ascii=False)}])
    ctx, events = _make_ctx(tmp_path, llm, enable_llm_review=True, api_key="fake-key")
    ctx.issues = _dedup_issues()

    await run_refactor_stage(ctx)

    assert len(llm.calls) == 1
    assert llm.calls[0]["json_mode"] is True
    assert ctx.refactor_proposals[0].source == "heuristic+llm"
    assert ctx.extra["refactor_stats"]["enhanced"] == 1
    assert any("LLM 增强" in e["message"] for e in events if e.get("stage") == "refactor")


async def test_stage_no_llm_when_disabled(tmp_path: Path) -> None:
    """enable_llm_review=False（或无 api_key）→ 纯启发式，零 LLM 调用。"""
    llm = FakeLLMClient()
    ctx, _ = _make_ctx(tmp_path, llm, enable_llm_review=False)
    ctx.issues = _dedup_issues()

    await run_refactor_stage(ctx)

    assert llm.calls == []
    assert ctx.refactor_proposals[0].source == "heuristic"


async def test_stage_llm_exhausted_degrades_gracefully(tmp_path: Path) -> None:
    """LLM 启用但脚本耗尽 → 全部降级为启发式原文，阶段不失败。"""
    llm = FakeLLMClient()  # 空脚本
    ctx, events = _make_ctx(tmp_path, llm, enable_llm_review=True, api_key="fake-key")
    ctx.issues = _dedup_issues()

    await run_refactor_stage(ctx)

    proposal = ctx.refactor_proposals[0]
    assert proposal.source == "heuristic"
    assert "统一异常处理" in proposal.title  # 启发式原文（配方标题）保留
    assert ctx.extra["refactor_stats"]["degraded"] == 1
    assert not any(e.get("error") for e in events if e.get("stage") == "refactor")


async def test_stage_clean_project_zero_proposals(tmp_path: Path) -> None:
    """干净项目：0 条方案 +「重构方案：0 条」事件，不崩、无 LLM 调用。"""
    llm = FakeLLMClient()
    ctx, events = _make_ctx(tmp_path, llm, enable_llm_review=True, api_key="fake-key")

    await run_refactor_stage(ctx)

    assert ctx.refactor_proposals == []
    assert ctx.extra["refactor_stats"]["proposals"] == 0
    assert any(e["message"] == "重构方案：0 条" for e in events if e.get("stage") == "refactor")
    assert llm.calls == []  # 无方案时不做任何增强调用
