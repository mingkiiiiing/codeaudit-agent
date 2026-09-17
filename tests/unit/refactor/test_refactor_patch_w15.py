"""W15-E 重构执行闭环单测：refactor proposal → 合成 Issue → fix 管线 → Patch。

覆盖（docs/20 §4.6）：
- 场景 a：合法 patch → 合成 Issue 走完既有 fix 管线，终态 verified（文件已改、
  现有 pytest 全绿、Patch.issue_id 与合成 Issue 关联）；
- 场景 b：语法损坏 diff → 语法双校验（tree-sitter + ast）拦截 → 回滚 +
  needs-review，工作副本逐字节还原；
- 回归守护：离线模式（无 LLM）或未开 do_fix 时 refactor 阶段不合成任何 Issue，
  ctx.issues 与 refactor_stats 形状零变化；
- target 映射：file::symbol 剥离符号、非文件目标（project:imports）与工作副本
  外路径跳过并计数。

全部离线：FakeLLM 脚本化回放（第 1 次调用为 refactor 增强，第 2 次为 fix 补丁
生成）；git CLI 仅本机调用；沙箱跑 pytest 为白名单子进程。
"""

from __future__ import annotations

import json
from pathlib import Path

from audit.config import AuditConfig
from audit.fix.stage import run_fix_stage
from audit.llm.base import FakeLLMClient
from audit.models import Category, FixStatus, Issue, IssueSource, RefactorProposal, Severity
from audit.pipeline import PipelineContext
from audit.refactor.stage import (
    REFACTOR_ISSUE_TITLE_PREFIX,
    _synthesize_refactor_issues,
    run_refactor_stage,
)
from audit.workspace import WorkspaceContext

_PREFIX = REFACTOR_ISSUE_TITLE_PREFIX

# ---------------------------------------------------------------- 样例项目（LF 换行）

HANDLERS_PY = """import logging

logger = logging.getLogger(__name__)


def handle(value):
    try:
        return 10 / value
    except:
        return None
"""

TEST_HANDLERS_PY = """import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handlers import handle


def test_handle_normal():
    assert handle(2) == 5


def test_handle_zero():
    assert handle(0) is None
"""

# 合法修复 diff：handlers.py 第 9 行裸 except 改精确捕获并记录日志（4→5 行）
VALID_FIX_DIFF = (
    "diff --git a/handlers.py b/handlers.py\n"
    "--- a/handlers.py\n"
    "+++ b/handlers.py\n"
    "@@ -7,4 +7,5 @@\n"
    "     try:\n"
    "         return 10 / value\n"
    "-    except:\n"
    "+    except Exception:\n"
    '+        logger.exception("handle failed")\n'
    "         return None\n"
)

# 语法破坏型 diff：except 后接未闭合列表（tree-sitter 与 ast.parse 双双失败）
SYNTAX_BREAK_DIFF = (
    "diff --git a/handlers.py b/handlers.py\n"
    "--- a/handlers.py\n"
    "+++ b/handlers.py\n"
    "@@ -7,4 +7,4 @@\n"
    "     try:\n"
    "         return 10 / value\n"
    "-    except:\n"
    "+    except [:\n"
    "         return None\n"
)

_ENHANCE_PAYLOAD = {"rationale": "增强后的统一异常处理理由", "steps": ["第一步：收口 except", "第二步：补日志"], "benefit": "收益一句话"}


def _enhance_item() -> dict:
    """FakeLLM 脚本项 1：refactor 增强层的合法 JSON 响应。"""
    return {"content": json.dumps(_ENHANCE_PAYLOAD, ensure_ascii=False)}


def _fix_item(diff: str) -> dict:
    """FakeLLM 脚本项 2：Fix Agent 约定的 {"diff","rationale"} JSON 响应。"""
    return {"content": json.dumps({"diff": diff, "rationale": "统一异常处理：精确捕获并记录日志"}, ensure_ascii=False)}


# ---------------------------------------------------------------- 工厂


def _make_workspace(tmp_path: Path) -> WorkspaceContext:
    src = tmp_path / "src"
    for rel, content in (("handlers.py", HANDLERS_PY), ("tests/test_handlers.py", TEST_HANDLERS_PY)):
        target = src / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    return WorkspaceContext(
        audit_id="w15cardE", src_root=src, work_root=tmp_path, db_path=tmp_path / "index.db"
    )


def _make_ctx(ws: WorkspaceContext, llm: FakeLLMClient, **cfg) -> tuple[PipelineContext, list[dict]]:
    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)

    config = AuditConfig(
        source_path=str(ws.src_root), do_fix=True, enable_llm_review=True, api_key="fake-key", **cfg
    )
    return PipelineContext(config=config, workspace=ws, llm=llm, emitter=emit), events


def _rule_issues() -> list[Issue]:
    """3 处同文件 PY-BARE-EXCEPT 规则命中（LOW：不进 fix 既有 critical/high 口径）。"""
    return [
        Issue(
            id=f"ISS-{n:04d}",
            category=Category.BUG,
            severity=Severity.LOW,
            title="裸 except 吞掉所有异常",
            file="handlers.py",
            line_start=ln,
            line_end=ln,
            evidence=["rule:PY-BARE-EXCEPT", f"loc:handlers.py:{ln}-{ln}"],
            confidence=0.7,
            source=IssueSource.RULE,
        )
        for n, ln in enumerate((9, 20, 31), 1)
    ]


# ---------------------------------------------------------------- 场景 a：合法 patch → verified


async def test_refactor_issue_runs_pipeline_to_verified(tmp_path: Path) -> None:
    """合成 Issue 走完 fix 管线：LLM 合法 diff → 语法双校验 → 现有测试全绿 → verified。"""
    ws = _make_workspace(tmp_path)
    llm = FakeLLMClient([_enhance_item(), _fix_item(VALID_FIX_DIFF)])
    ctx, events = _make_ctx(ws, llm)
    ctx.issues = _rule_issues()

    await run_refactor_stage(ctx)

    # 合成 Issue：medium + "[Refactor] " 前缀 + source=LLM + file 映射 + suggestion=steps
    assert len(ctx.issues) == 4
    refactor_issues = [i for i in ctx.issues if i.title.startswith(_PREFIX)]
    assert len(refactor_issues) == 1
    issue = refactor_issues[0]
    assert issue.severity == Severity.MEDIUM
    assert issue.source == IssueSource.LLM
    assert issue.category == Category.STYLE
    assert issue.file == "handlers.py"
    assert issue.suggestion == "第一步：收口 except\n第二步：补日志"
    assert issue.confidence == 0.65  # 3 处命中的 dedup 方案置信度（0.5 + 0.05*3）
    assert any(ev.startswith("refactor:REF-0001") for ev in issue.evidence)
    assert ctx.extra["refactor_stats"]["refactor_fix_issues"] == 1
    assert ctx.extra["refactor_stats"]["refactor_fix_skipped"] == 0
    assert any("重构执行" in e["message"] for e in events if e.get("stage") == "refactor")

    await run_fix_stage(ctx)

    assert len(ctx.patches) == 1
    patch = ctx.patches[0]
    assert patch.apply_status == "verified"
    assert patch.issue_id == issue.id
    assert patch.diff == VALID_FIX_DIFF
    assert (patch.tests_run, patch.tests_passed) == (2, 2)
    assert issue.fix_status == FixStatus.VERIFIED
    assert issue.patch_id == patch.id
    content = ws.abs_path("handlers.py").read_text(encoding="utf-8")
    assert "except Exception:" in content
    stats = ctx.extra["fix_stats"]
    assert stats["attempted"] == 1
    assert stats["verified"] == 1
    assert stats["applied"] == 1
    # LOW 规则 Issue 不在候选口径内，未被触碰
    assert all(i.fix_status == FixStatus.NONE for i in ctx.issues if i is not issue)


# ---------------------------------------------------------------- 场景 b：语法损坏 → 回滚 + needs-review


async def test_broken_syntax_patch_rolls_back_to_needs_review(tmp_path: Path) -> None:
    """语法损坏 diff：应用后语法双校验失败 → 回滚（逐字节还原）+ needs-review。"""
    ws = _make_workspace(tmp_path)
    llm = FakeLLMClient([_enhance_item(), _fix_item(SYNTAX_BREAK_DIFF)])
    ctx, _events = _make_ctx(ws, llm)
    ctx.issues = _rule_issues()

    await run_refactor_stage(ctx)
    issue = next(i for i in ctx.issues if i.title.startswith(_PREFIX))
    await run_fix_stage(ctx)

    assert len(ctx.patches) == 1
    patch = ctx.patches[0]
    assert patch.apply_status == "needs-review"
    assert patch.issue_id == issue.id
    assert issue.fix_status == FixStatus.NEEDS_REVIEW
    assert issue.patch_id == patch.id
    # 回滚语义：工作副本与补丁前逐字节一致
    assert ws.abs_path("handlers.py").read_bytes() == HANDLERS_PY.encode("utf-8")
    stats = ctx.extra["fix_stats"]
    assert stats["attempted"] == 1
    assert stats["needs_review"] == 1
    assert stats["applied"] == 0
    assert stats["verified"] == 0


# ---------------------------------------------------------------- 回归守护：离线 / 未开 do_fix 不合成


async def test_offline_stage_synthesizes_no_issues(tmp_path: Path) -> None:
    """离线模式（enable_llm_review=False + 无 api_key）：不合成 Issue，零 LLM 调用。"""
    ws = _make_workspace(tmp_path)
    llm = FakeLLMClient()
    src = tmp_path / "src"
    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)

    config = AuditConfig(source_path=str(src), do_fix=True, enable_llm_review=False)
    ctx = PipelineContext(config=config, workspace=ws, llm=llm, emitter=emit)
    ctx.issues = _rule_issues()

    await run_refactor_stage(ctx)

    assert len(ctx.issues) == 3  # 只有规则 Issue，零合成
    assert all(not i.title.startswith(_PREFIX) for i in ctx.issues)
    assert llm.calls == []
    # stats 形状不变：不出现 W15-E 新键
    assert "refactor_fix_issues" not in ctx.extra["refactor_stats"]
    assert "refactor_fix_skipped" not in ctx.extra["refactor_stats"]


async def test_fix_disabled_no_synthesis(tmp_path: Path) -> None:
    """LLM 可用但 do_fix=False：增强照旧（既有行为），但不合成修复候选 Issue。"""
    ws = _make_workspace(tmp_path)
    llm = FakeLLMClient([_enhance_item()])
    src = tmp_path / "src"
    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)

    config = AuditConfig(source_path=str(src), do_fix=False, enable_llm_review=True, api_key="fake-key")
    ctx = PipelineContext(config=config, workspace=ws, llm=llm, emitter=emit)
    ctx.issues = _rule_issues()

    await run_refactor_stage(ctx)

    assert len(llm.calls) == 1  # 增强层照常发生
    assert ctx.refactor_proposals[0].source == "heuristic+llm"
    assert len(ctx.issues) == 3  # 零合成
    assert "refactor_fix_issues" not in ctx.extra["refactor_stats"]


# ---------------------------------------------------------------- target 映射


async def test_synthesize_maps_symbol_target_and_skips_unmapped(tmp_path: Path) -> None:
    """file::symbol 剥离符号取文件；非文件目标与工作副本外路径跳过并计数。"""
    ws = _make_workspace(tmp_path)
    ctx, _events = _make_ctx(ws, FakeLLMClient())
    proposals = [
        RefactorProposal(
            id="REF-0001",
            title="分解长函数 handle",
            target="handlers.py::handle",
            kind="decompose",
            rationale="r",
            steps=["s1"],
            source="heuristic",
            confidence=0.9,
        ),
        RefactorProposal(
            id="REF-0002",
            title="消除模块循环依赖（2 处）",
            target="project:imports",
            kind="other",
            rationale="r",
            steps=["s1"],
            source="heuristic",
            confidence=0.75,
        ),
        RefactorProposal(
            id="REF-0003",
            title="命中不存在文件",
            target="ghost/missing.py",
            kind="dedup",
            rationale="r",
            steps=["s1"],
            source="heuristic",
            confidence=0.7,
        ),
    ]

    injected, skipped = _synthesize_refactor_issues(ctx, proposals)

    assert (injected, skipped) == (1, 2)
    assert len(ctx.issues) == 1
    assert ctx.issues[0].file == "handlers.py"
    assert ctx.issues[0].id == "ISS-0001"  # 空 ctx.issues 水位从 1 起
    assert any(ev.startswith("refactor:REF-0001") for ev in ctx.issues[0].evidence)
