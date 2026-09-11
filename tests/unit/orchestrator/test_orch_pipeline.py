"""T5 Orchestrator 自测。

策略：
A) T2/T3/T4 模块全部缺失时 run_audit 不崩：产出空 Report 且有 skip 事件；
B) 用 monkeypatch.setitem(sys.modules, ...) 注入假模块，验证编排顺序、
   事件序列与报告聚合正确（模拟 T2/T4 已集成的联调形态）。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from audit.config import AuditConfig
from audit.models import Category, Issue, Severity
from audit.orchestrator.pipeline import run_audit, run_audit_simple
from audit.workspace import WorkspaceContext


def _fake_module(name: str, **attrs) -> types.ModuleType:
    """构造带属性的假模块（用于注入 sys.modules）。"""
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _block_modules(monkeypatch, *names: str) -> None:
    """把跨包模块从导入系统中屏蔽（sys.modules 置 None → import 抛 ImportError）。

    无论 T2/T3/T4 是否已合入本目录，都能确定性地模拟"模块未集成"状态。
    """
    for name in names:
        monkeypatch.setitem(sys.modules, name, None)


def _stages(events: list[dict]) -> list[str]:
    """从事件流提取各阶段名（按出现顺序去重）。"""
    seen: list[str] = []
    for e in events:
        stage = e.get("stage")
        if stage and stage not in seen:
            seen.append(stage)
    return seen


def _skip_messages(events: list[dict]) -> list[str]:
    return [e["message"] for e in events if "skip" in str(e.get("message", ""))]


# ---------------------------------------------------------------- A) 全缺失降级


async def test_all_modules_missing_produces_empty_report(fake_emitter, tmp_path: Path, monkeypatch):
    """T2/T4 未集成：run_audit 不崩，产出空报告并发出 skip 事件。"""
    _block_modules(
        monkeypatch,
        "audit.ingest",
        "audit.indexer",
        "audit.understand.architecture",
        "audit.detect.engine",
    )
    src = tmp_path / "proj"
    src.mkdir()
    (src / "main.py").write_text("print('hi')\n", encoding="utf-8")
    config = AuditConfig(
        source_path=str(src), work_root=str(tmp_path / "work"), out_dir=str(tmp_path / "out")
    )
    report = await run_audit(config, fake_emitter)

    assert report.project_name == "proj"
    assert report.issues == []
    assert report.loc == 1
    assert report.languages == {"python": 100.0}
    assert report.stats.duration_sec > 0
    assert report.created_at

    events = fake_emitter.events
    skips = _skip_messages(events)
    # ingest / index / understand / detect 四个跨包阶段均被跳过
    assert len(skips) >= 4
    assert all("module not integrated" in m for m in skips)
    # 报告阶段真实产出
    assert any(e["stage"] == "report" and "报告已生成" in e["message"] for e in events)
    assert (tmp_path / "out" / "report.json").exists()
    assert (tmp_path / "out" / "report.md").exists()
    assert (tmp_path / "out" / "report.html").exists()


async def test_llm_not_configured_emits_warning(fake_emitter, tmp_path: Path):
    config = AuditConfig(source_path=str(tmp_path), work_root=str(tmp_path / "w"))
    await run_audit(config, fake_emitter)
    warnings = [e for e in fake_emitter.events if e.get("warning")]
    assert any("LLM 未配置" in e["message"] for e in warnings)


async def test_llm_key_without_glm_client_falls_back(fake_emitter, tmp_path: Path, monkeypatch):
    """配置了 api_key 但 T3 GlmClient 未集成：回退 FakeLLM 并警告（绝不触网）。"""
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    _block_modules(monkeypatch, "audit.llm.glm_client", "audit.agents.review")
    config = AuditConfig(
        source_path=str(tmp_path), work_root=str(tmp_path / "w"), api_key="fake-key"
    )
    report = await run_audit(config, fake_emitter)
    assert report is not None
    warnings = [e for e in fake_emitter.events if e.get("warning")]
    assert any("纯规则模式" in e["message"] for e in warnings)
    assert any("未集成" in e["message"] for e in warnings)


async def test_stage_error_does_not_crash_pipeline(fake_emitter, tmp_path: Path, monkeypatch):
    """某阶段运行期异常（非 ImportError）→ 记入 stage_errors，流水线继续到出报告。"""
    # 屏蔽真实 indexer，保证 index 阶段确定性跳过、只留下 understand 的运行期异常
    _block_modules(monkeypatch, "audit.indexer")
    boom = _fake_module("audit.understand", **{})
    arch = _fake_module("audit.understand.architecture", build_architecture=lambda ctx: 1 / 0)
    monkeypatch.setitem(sys.modules, "audit.understand", boom)
    monkeypatch.setitem(sys.modules, "audit.understand.architecture", arch)

    captured: dict = {}

    def fake_ingest(source, work_root):
        ws = WorkspaceContext(
            audit_id="cap0001",
            src_root=Path(source),
            work_root=Path(work_root),
            db_path=Path(work_root) / "i.db",
        )
        captured["ws"] = ws
        return ws

    def fake_detect(ctx, review_fn=None):
        captured["ctx"] = ctx
        return []

    monkeypatch.setitem(sys.modules, "audit.ingest", _fake_module("audit.ingest", ingest=fake_ingest))
    monkeypatch.setitem(
        sys.modules, "audit.detect.engine", _fake_module("audit.detect.engine", run_detection=fake_detect)
    )

    config = AuditConfig(source_path=str(tmp_path), work_root=str(tmp_path / "w"))
    report = await run_audit(config, fake_emitter)
    assert report is not None
    # 异常被记录而非扩散
    errs = captured["ctx"].extra["stage_errors"]
    assert len(errs) == 1 and errs[0]["stage"] == "understand" and "ZeroDivisionError" in errs[0]["error"]
    # 后续 detect / report 照常执行
    stages = _stages(fake_emitter.events)
    assert stages.index("understand") < stages.index("detect") < stages.index("report")


# ---------------------------------------------------------------- B) 假模块联调


@pytest.fixture
def wired_fake_modules(tmp_path: Path, monkeypatch):
    """注入 T2/T4 假模块：ingest/indexer/understand.architecture/detect.engine。"""
    # 契约 v1.2：屏蔽 W2 真实 fix/testgen 模块，保持"未集成占位"语义可断言
    _block_modules(monkeypatch, "audit.fix", "audit.testgen")
    captured: dict = {}
    fake_issues = [
        Issue(
            id="ISS-0001",
            category=Category.BUG,
            severity=Severity.CRITICAL,
            title="空指针",
            file="a.py",
            line_start=1,
            line_end=2,
        ),
        Issue(
            id="ISS-0002",
            category=Category.STYLE,
            severity=Severity.LOW,
            title="命名",
            file="b.py",
            line_start=5,
            line_end=5,
        ),
    ]

    class FakeIndex:
        def __init__(self, workspace):
            self.workspace = workspace

        def build(self):
            captured["built"] = True
            return None

        def stats(self):
            return {"files": 2, "symbols": 4}

    def fake_ingest(source, work_root):
        ws = WorkspaceContext(
            audit_id="fake0001",
            src_root=Path(source),
            work_root=Path(work_root) / "fake0001",
            db_path=Path(work_root) / "fake0001" / "index.db",
        )
        captured["workspace"] = ws
        return ws

    def fake_create_index(workspace, db_path=None):
        captured["index_ws"] = workspace
        return FakeIndex(workspace)

    def fake_build_architecture(ctx):
        from audit.models import ArchitectureCard

        captured["arch_ctx"] = ctx
        return ArchitectureCard(text="假架构", tech_stack=["Python"], hotspots=["a.py"])

    def fake_run_detection(ctx, review_fn=None):
        captured["detect_ctx"] = ctx
        captured["review_fn"] = review_fn
        return list(fake_issues)

    # 注意：audit.understand 父包也不存在，需同时注入父模块与子模块
    monkeypatch.setitem(sys.modules, "audit.ingest", _fake_module("audit.ingest", ingest=fake_ingest))
    monkeypatch.setitem(
        sys.modules, "audit.indexer", _fake_module("audit.indexer", create_index=fake_create_index)
    )
    monkeypatch.setitem(sys.modules, "audit.understand", _fake_module("audit.understand"))
    monkeypatch.setitem(
        sys.modules,
        "audit.understand.architecture",
        _fake_module("audit.understand.architecture", build_architecture=fake_build_architecture),
    )
    monkeypatch.setitem(
        sys.modules,
        "audit.detect.engine",
        _fake_module("audit.detect.engine", run_detection=fake_run_detection),
    )
    return captured, fake_issues


async def test_wired_pipeline_order_events_and_report(
    fake_emitter, tmp_path: Path, wired_fake_modules
):
    captured, fake_issues = wired_fake_modules
    src = tmp_path / "my-proj"
    src.mkdir()
    (src / "a.py").write_text("x = 1\n", encoding="utf-8")
    config = AuditConfig(
        source_path=str(src),
        work_root=str(tmp_path / "work"),
        out_dir=str(tmp_path / "out"),
        do_fix=True,
        do_tests=True,
    )
    report = await run_audit(config, fake_emitter)
    events = fake_emitter.events

    # 1) 编排顺序：七阶段按序出现
    assert _stages(events) == ["init", "ingest", "index", "understand", "detect", "fix", "testgen", "report", "done"]

    # 1b) 报告项目名取原始路径名（而非工作副本目录名 src）
    assert report.project_name == "my-proj"

    # 2) ingest 产物替换了临时工作区
    assert captured["workspace"].audit_id == "fake0001"

    # 3) index：build 被调用，store 注入 workspace 与 ctx
    assert captured["built"] is True
    assert captured["index_ws"] is captured["workspace"]
    assert captured["detect_ctx"] is not None
    assert captured["detect_ctx"].workspace is captured["workspace"]
    assert captured["detect_ctx"].index is captured["detect_ctx"].workspace.index
    assert any("files" in e.get("message", "") for e in events if e["stage"] == "index")

    # 4) understand：架构卡注入 ctx
    assert captured["arch_ctx"] is captured["detect_ctx"]
    assert captured["detect_ctx"].architecture.text == "假架构"

    # 5) detect：无 api_key → review_fn 为 None（纯规则模式）
    assert captured["review_fn"] is None

    # 6) fix/testgen 占位事件
    assert any(e["stage"] == "fix" and "skipped: wave2" in e["message"] for e in events)
    assert any(e["stage"] == "testgen" and "skipped: wave2" in e["message"] for e in events)

    # 7) 报告聚合了假检测产物
    assert report.audit_id == "fake0001"
    assert len(report.issues) == 2
    assert report.summary["critical"] == 1 and report.summary["low"] == 1
    assert report.architecture is not None and report.architecture.text == "假架构"
    assert report.health_score >= 0

    # 8) LLM 未配置警告 + 报告落盘
    assert any("LLM 未配置" in e["message"] for e in events)
    assert (tmp_path / "out" / "report.json").exists()
    assert (tmp_path / "out" / "report.md").exists()
    assert (tmp_path / "out" / "report.html").exists()


async def test_run_audit_simple_returns_report(tmp_path: Path, wired_fake_modules):
    captured, fake_issues = wired_fake_modules
    config = AuditConfig(source_path=str(tmp_path), work_root=str(tmp_path / "w"))
    report = await run_audit_simple(config)
    assert report is not None
    assert len(report.issues) == 2
    assert report.stats.duration_sec > 0


async def test_no_llm_review_passes_none_review_fn(tmp_path: Path, wired_fake_modules, monkeypatch):
    """enable_llm_review=False 时即使有 api_key 也不装配 review_fn。"""
    captured, _ = wired_fake_modules
    monkeypatch.setattr(AuditConfig, "llm_available", property(lambda self: True))
    config = AuditConfig(
        source_path=str(tmp_path),
        work_root=str(tmp_path / "w"),
        api_key="k",
        enable_llm_review=False,
    )
    await run_audit_simple(config)
    assert captured["review_fn"] is None
