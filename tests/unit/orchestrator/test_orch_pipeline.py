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
from audit.llm.base import FakeLLMClient
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
    """T2/T4 未集成：run_audit 不崩，产出空报告；index/detect/fix 被 ingest 门控跳过。"""
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
    # R4-4：ingest 未成功 → 报告统计走空 manifests，不回扫用户原始目录
    assert report.loc == 0
    assert report.languages == {}
    assert report.stats.duration_sec > 0
    assert report.created_at

    events = fake_emitter.events
    skips = _skip_messages(events)
    # ingest 因模块未集成被跳过（占位文案）；understand 现随 ingest_ok 门控（R4-4）
    assert len(skips) == 1
    assert "module not integrated" in skips[0]
    # R1-2/R4-4 门控：index / understand / detect 因 ingest 未成功被明确跳过
    gated = [e for e in events if "ingest 未成功" in str(e.get("message", ""))]
    assert {e["stage"] for e in gated} >= {"index", "understand", "detect"}
    # R4-10：ingest 失败（降级）时 done 事件带 degraded 标志
    done_events = [e for e in events if e.get("stage") == "done"]
    assert done_events and done_events[-1].get("degraded") is True
    # 报告阶段真实产出
    assert any(e["stage"] == "report" and "报告已生成" in e["message"] for e in events)
    assert (tmp_path / "out" / "report.json").exists()
    assert (tmp_path / "out" / "report.md").exists()
    assert (tmp_path / "out" / "report.html").exists()


async def test_ingest_failure_gates_index_detect_fix(fake_emitter, tmp_path: Path, monkeypatch):
    """R1-2：ingest 失败（非 ImportError）时 index/detect/fix 全部门控跳过且不扫描原始目录。"""
    _block_modules(monkeypatch, "audit.indexer")

    def broken_ingest(source, work_root, audit_id=None):
        raise RuntimeError("disk full")

    monkeypatch.setitem(
        sys.modules, "audit.ingest", _fake_module("audit.ingest", ingest=broken_ingest)
    )
    src = tmp_path / "proj"
    src.mkdir()
    (src / "secret_impl.py").write_text("KEY = 'x'\n", encoding="utf-8")
    config = AuditConfig(
        source_path=str(src),
        work_root=str(tmp_path / "work"),
        out_dir=str(tmp_path / "out"),
        do_fix=True,
    )
    report = await run_audit(config, fake_emitter)

    assert report.issues == []
    gated = [e for e in fake_emitter.events if "ingest 未成功" in str(e.get("message", ""))]
    assert {e["stage"] for e in gated} == {"index", "understand", "detect", "refactor", "fix"}
    # ingest 失败被记入 stage_errors 而非中断流水线
    assert any(e.get("stage") == "ingest" and e.get("error") for e in fake_emitter.events)
    # 原始目录未被触碰
    assert (src / "secret_impl.py").read_text(encoding="utf-8") == "KEY = 'x'\n"


async def test_ingest_failure_report_stats_use_empty_manifests(fake_emitter, tmp_path: Path, monkeypatch):
    """R4-4：ingest 失败时报告统计不回扫原始目录（files_total=0 / loc=0 / languages 空）。"""
    _block_modules(monkeypatch, "audit.indexer")

    def broken_ingest(source, work_root, audit_id=None):
        raise RuntimeError("unzip failed")

    monkeypatch.setitem(
        sys.modules, "audit.ingest", _fake_module("audit.ingest", ingest=broken_ingest)
    )
    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("x = 1\n" * 30, encoding="utf-8")
    config = AuditConfig(
        source_path=str(src), work_root=str(tmp_path / "work"), out_dir=str(tmp_path / "out")
    )
    report = await run_audit(config, fake_emitter)

    assert report.stats.files_total == 0
    assert report.loc == 0
    assert report.languages == {}
    # 降级标志贯通到 done 事件（R4-10）
    done = [e for e in fake_emitter.events if e.get("stage") == "done"]
    assert done and done[-1].get("degraded") is True


async def test_ingest_success_done_event_has_no_degraded_flag(demo_proj_path: Path, tmp_path: Path, fake_emitter):
    """R4-10 对照：ingest 成功时 done 事件不带 degraded 字段。"""
    config = AuditConfig(
        source_path=str(demo_proj_path), work_root=str(tmp_path / "w"), out_dir=str(tmp_path / "out")
    )
    await run_audit(config, fake_emitter)
    done = [e for e in fake_emitter.events if e.get("stage") == "done"]
    assert done and "degraded" not in done[-1]


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

    def fake_ingest(source, work_root, audit_id="fake0001"):
        ws = WorkspaceContext(
            audit_id=audit_id,
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

    def fake_ingest(source, work_root, audit_id="fake0001"):
        ws = WorkspaceContext(
            audit_id=audit_id,
            src_root=Path(source),
            work_root=Path(work_root) / audit_id,
            db_path=Path(work_root) / audit_id / "index.db",
        )
        captured["workspace"] = ws
        captured["ingest_audit_id"] = audit_id
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
    assert _stages(events) == ["init", "ingest", "index", "understand", "detect", "refactor", "fix", "testgen", "report", "done"]

    # 1b) 报告项目名取原始路径名（而非工作副本目录名 src）
    assert report.project_name == "my-proj"

    # 2) ingest 产物替换了临时工作区；编排层把自身 audit_id 贯通传入（R1-17）
    assert captured["workspace"].audit_id == captured["ingest_audit_id"]
    assert captured["ingest_audit_id"] == report.audit_id

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
    assert report.audit_id == captured["ingest_audit_id"]
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


# ---------------------------------------------------------------- R1-1/R1-3/R1-4 回归


class _RecordingLLM(FakeLLMClient):
    """带 aclose 记录的 FakeLLM（R1-3：审计结束必须释放客户端）。"""

    def __init__(self, script=None):
        super().__init__(script)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


async def test_simple_review_fn_end_to_end_produces_llm_issues(
    tmp_path: Path, monkeypatch, fake_emitter
):
    """R1-1 贯通测试：simple 模式全流程，review_fn 参数正确转接、LLM Issue 进入报告。

    修复前：file_path 被绑到 workspace 位，review_file 内部抛 TypeError 被
    引擎吞进 review_errors，LLM 审查静默失效。
    """
    import json

    import audit.detect.engine as engine_mod
    from audit.llm.base import FakeLLMClient
    from audit.orchestrator import pipeline as orch

    src = tmp_path / "proj"
    src.mkdir()
    (src / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    payload = {
        "issues": [
            {
                "category": "bug",
                "severity": "high",
                "title": "add 减法实现疑似缺陷",
                "file": "app.py",
                "line_start": 2,
                "line_end": 2,
                "description": "函数名与实现语义不符",
                "confidence": 0.9,
            }
        ]
    }
    llm = FakeLLMClient([{"content": json.dumps(payload, ensure_ascii=False)}])
    monkeypatch.setattr(orch, "_make_llm", lambda config: (llm, None))
    _block_modules(monkeypatch, "audit.understand.architecture")  # 隔离 understand 的 LLM 调用

    captured: dict = {}
    real_run_detection = engine_mod.run_detection

    async def spy_run_detection(ctx, **kwargs):
        captured["ctx"] = ctx
        return await real_run_detection(ctx, **kwargs)

    monkeypatch.setattr(engine_mod, "run_detection", spy_run_detection)

    config = AuditConfig(
        source_path=str(src),
        work_root=str(tmp_path / "work"),
        out_dir=str(tmp_path / "out"),
        api_key="test-key",  # 让 detect 装配 review_fn（不会触网：LLM 已替换为 FakeLLM）
        review_mode="simple",
        enable_verify=False,
    )
    report = await run_audit(config, fake_emitter)

    # review 真正被调用（LLM 收到审查请求）
    assert len(llm.calls) == 1
    # LLM Issue 进入最终报告（而非静默 review_errors）
    titles = [i.title for i in report.issues]
    assert any("add 减法实现疑似缺陷" in t for t in titles)
    assert any(i.source.value == "llm" for i in report.issues)
    assert "review_errors" not in captured["ctx"].extra
    assert not any(e.get("error") and e.get("stage") == "detect" for e in fake_emitter.events)


async def test_run_audit_closes_llm_in_finally(tmp_path: Path, monkeypatch, fake_emitter):
    """R1-3：正常结束路径 run_audit 在 finally 中调用 llm.aclose()。"""
    from audit.orchestrator import pipeline as orch

    src = tmp_path / "proj"
    src.mkdir()
    (src / "m.py").write_text("x = 1\n", encoding="utf-8")
    llm = _RecordingLLM()
    monkeypatch.setattr(orch, "_make_llm", lambda config: (llm, None))
    config = AuditConfig(source_path=str(src), work_root=str(tmp_path / "w"))
    await run_audit(config, fake_emitter)
    assert llm.closed is True


async def test_run_audit_closes_llm_when_done_emit_raises(tmp_path: Path, monkeypatch):
    """R1-3：收尾事件抛异常时 aclose 仍被执行（finally 兜底）。"""
    from audit.orchestrator import pipeline as orch

    src = tmp_path / "proj"
    src.mkdir()
    (src / "m.py").write_text("x = 1\n", encoding="utf-8")
    llm = _RecordingLLM()

    async def exploding_emitter(event: dict) -> None:
        if event.get("stage") == "done":  # done 事件在阶段兜底之外，异常向外传播
            raise RuntimeError("emitter down")

    monkeypatch.setattr(orch, "_make_llm", lambda config: (llm, None))
    config = AuditConfig(source_path=str(src), work_root=str(tmp_path / "w"))
    with pytest.raises(RuntimeError, match="emitter down"):
        await run_audit(config, exploding_emitter)
    assert llm.closed is True


async def test_run_audit_closes_sqlite_store(demo_proj_path: Path, tmp_path: Path, fake_emitter):
    """R1-4：审计结束后索引连接已关闭（Windows 下未关闭的 db 文件无法删除）。"""
    config = AuditConfig(
        source_path=str(demo_proj_path),
        work_root=str(tmp_path / "work"),
        out_dir=str(tmp_path / "out"),
    )
    await run_audit(config, fake_emitter)
    dbs = list((tmp_path / "work").glob("*/index.db"))
    assert dbs, "索引库未落盘"
    for db in dbs:
        db.unlink()  # 连接未关闭时 Windows 抛 PermissionError
    assert not any(db.exists() for db in dbs)


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
