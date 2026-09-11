"""W2-A1 单测：run_fix_stage 修复闭环编排（全部离线，FakeLLM 脚本回放）。

覆盖路径：
- verified：合法 diff + 现有测试全绿 → VERIFIED，文件已改；
- 坏 diff：apply 失败 → needs-review，文件与备份逐字节一致；
- 语法破坏：apply 成功但语法解析失败 → 回滚 + needs-review；
- 测试失败：语法通过但现有测试挂 → 回滚 + needs-review；
- 无测试项目：apply + 语法通过 → syntax-ok；
- LLM 输出不可解析：failed，带错误回喂重试 1 次；
- fix_max_patches 上限与 (severity, -confidence) 排序。
"""

from __future__ import annotations

from pathlib import Path

from audit.llm.base import FakeLLMClient
from audit.models import Category, FixStatus, Issue, Severity
from audit.pipeline import PipelineContext

import _fix_helpers as H
from audit.fix.stage import run_fix_stage


def _issue(
    issue_id: str,
    file: str,
    severity: Severity,
    confidence: float,
    line: int = 1,
    title: str = "示例缺陷",
) -> Issue:
    return Issue(
        id=issue_id,
        category=Category.BUG,
        severity=severity,
        title=title,
        file=file,
        line_start=line,
        line_end=line,
        code_snippet="",
        description=title,
        evidence=["rule_hit: demo"],
        confidence=confidence,
    )


# ---------------------------------------------------------------- verified 路径


async def test_verified_path_applies_patch_and_runs_tests(tmp_path: Path, fake_emitter):
    """合法 diff 修复裸 except + 现有 pytest 全绿 → verified，文件内容已改。"""
    ws = H.make_workspace(
        tmp_path,
        {"app.py": H.APP_PY, "tests/test_app.py": H.TEST_APP_PY},
    )
    fake = FakeLLMClient([H.llm_json(H.BARE_EXCEPT_DIFF, "裸 except 改为精确捕获并记录日志")])
    issue = _issue("ISS-0001", "app.py", Severity.CRITICAL, 0.9, line=9, title="裸 except")
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.issues = [issue]

    await run_fix_stage(ctx)

    # Patch 产物
    assert len(ctx.patches) == 1
    patch = ctx.patches[0]
    assert patch.id == "PATCH-0001"
    assert patch.issue_id == "ISS-0001"
    assert patch.diff == H.BARE_EXCEPT_DIFF
    assert patch.apply_status == "verified"
    assert patch.tests_run == 2
    assert patch.tests_passed == 2

    # Issue 状态
    assert issue.fix_status == FixStatus.VERIFIED
    assert issue.patch_id == "PATCH-0001"

    # 文件内容已被补丁修改
    content = ws.abs_path("app.py").read_text(encoding="utf-8")
    assert "except Exception:" in content
    assert 'logger.exception("divide failed")' in content

    # 统计与事件
    assert ctx.extra["fix_stats"] == {
        "attempted": 1,
        "patch_generated": 1,
        "applied": 1,
        "verified": 1,
        "syntax_ok": 0,
        "needs_review": 0,
        "failed": 0,
    }
    stages = [e for e in fake_emitter.events if e.get("stage") == "fix"]
    assert any(e.get("issue_id") == "ISS-0001" for e in stages)
    assert any(e.get("verified") == 1 for e in stages)  # 汇总事件


# ---------------------------------------------------------------- 坏 diff / 回滚


async def test_bad_diff_marks_needs_review_and_keeps_bytes_identical(tmp_path: Path, fake_emitter):
    """结构合法但无法应用的 diff → needs-review，文件与原内容逐字节一致。"""
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY, "tests/test_app.py": H.TEST_APP_PY})
    fake = FakeLLMClient([H.llm_json(H.BAD_CONTEXT_DIFF, "上下文写错了")])
    issue = _issue("ISS-0001", "app.py", Severity.HIGH, 0.8)
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.issues = [issue]

    await run_fix_stage(ctx)

    assert ctx.patches[0].apply_status == "needs-review"
    assert ctx.patches[0].rationale == "上下文写错了"
    assert issue.fix_status == FixStatus.NEEDS_REVIEW
    assert issue.patch_id == "PATCH-0001"
    # 逐字节一致（未污染工作副本）
    assert ws.abs_path("app.py").read_bytes() == H.APP_PY.encode("utf-8")
    assert ctx.extra["fix_stats"]["needs_review"] == 1
    assert ctx.extra["fix_stats"]["applied"] == 0


async def test_syntax_broken_diff_rolls_back(tmp_path: Path, fake_emitter):
    """应用成功但 tree-sitter 解析失败 → 回滚 + needs-review。"""
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY, "tests/test_app.py": H.TEST_APP_PY})
    fake = FakeLLMClient([H.llm_json(H.SYNTAX_BREAK_DIFF)])
    issue = _issue("ISS-0001", "app.py", Severity.CRITICAL, 0.95)
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.issues = [issue]

    await run_fix_stage(ctx)

    assert ctx.patches[0].apply_status == "needs-review"
    assert issue.fix_status == FixStatus.NEEDS_REVIEW
    assert ws.abs_path("app.py").read_bytes() == H.APP_PY.encode("utf-8")
    stats = ctx.extra["fix_stats"]
    assert stats["needs_review"] == 1 and stats["applied"] == 0


async def test_failing_existing_tests_roll_back_patch(tmp_path: Path, fake_emitter):
    """语法通过但现有测试失败 → 回滚 + needs-review，并记录测试计数。"""
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY, "tests/test_app.py": H.TEST_APP_PY})
    fake = FakeLLMClient([H.llm_json(H.WRONG_EXCEPT_DIFF)])
    issue = _issue("ISS-0001", "app.py", Severity.CRITICAL, 0.9, line=9)
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.issues = [issue]

    await run_fix_stage(ctx)

    patch = ctx.patches[0]
    assert patch.apply_status == "needs-review"
    assert patch.tests_run == 2
    assert patch.tests_passed == 1  # except ValueError 漏掉 ZeroDivisionError
    assert issue.fix_status == FixStatus.NEEDS_REVIEW
    assert ws.abs_path("app.py").read_bytes() == H.APP_PY.encode("utf-8")
    assert ctx.extra["fix_stats"]["needs_review"] == 1


# ---------------------------------------------------------------- 无测试项目（sample_workspace）


async def test_project_without_tests_gets_syntax_ok(sample_workspace, fake_emitter):
    """demo_proj 无测试目录：合法 diff（SQL 拼接改参数化）→ syntax-ok。"""
    fake = FakeLLMClient([H.llm_json(H.SQL_DIFF, "改为参数化查询避免 SQL 注入")])
    issue = _issue(
        "ISS-0001",
        "app/services/orders.py",
        Severity.CRITICAL,
        0.93,
        line=11,
        title="SQL 拼接",
    )
    ctx = H.make_ctx(sample_workspace, fake, fake_emitter)
    ctx.issues = [issue]

    await run_fix_stage(ctx)

    patch = ctx.patches[0]
    assert patch.apply_status == "syntax-ok"
    assert patch.tests_run == 0
    assert patch.tests_passed == 0
    assert issue.fix_status == FixStatus.SYNTAX_OK
    assert issue.patch_id == "PATCH-0001"
    content = sample_workspace.abs_path("app/services/orders.py").read_text(encoding="utf-8")
    assert 'cur = conn.execute(query, (order_id,))' in content
    assert ctx.extra["fix_stats"]["syntax_ok"] == 1


# ---------------------------------------------------------------- LLM 失败


async def test_unparsable_llm_output_records_failed(tmp_path: Path, fake_emitter):
    """LLM 两次输出均不可解析 → failed，无 Patch 产物，重试恰好 1 次。"""
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    fake = FakeLLMClient()  # 空脚本：所有响应 content=""
    issue = _issue("ISS-0001", "app.py", Severity.CRITICAL, 0.9)
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.issues = [issue]

    await run_fix_stage(ctx)

    assert ctx.patches == []
    assert issue.fix_status == FixStatus.NONE
    assert len(fake.calls) == 2  # 首次 + 错误回喂重试 1 次
    assert ws.abs_path("app.py").read_bytes() == H.APP_PY.encode("utf-8")
    stats = ctx.extra["fix_stats"]
    assert stats["attempted"] == 1 and stats["failed"] == 1


async def test_medium_and_low_issues_are_not_selected(fake_emitter, tmp_path: Path):
    """只选 critical/high；medium/low 不进入修复流程。"""
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    fake = FakeLLMClient()
    issues = [
        _issue("ISS-0001", "app.py", Severity.MEDIUM, 0.99),
        _issue("ISS-0002", "app.py", Severity.LOW, 0.99),
    ]
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.issues = issues

    await run_fix_stage(ctx)

    assert ctx.patches == []
    assert ctx.extra["fix_stats"]["attempted"] == 0
    assert fake.calls == []
    assert all(i.fix_status == FixStatus.NONE for i in issues)


# ---------------------------------------------------------------- 上限与排序


async def test_fix_max_patches_cap_and_priority_order(tmp_path: Path, fake_emitter):
    """5 个 issue、上限 2：只处理 critical 置信度最高的两个，其余不触碰。"""
    files = {f"f{i}.py": "x = 1\n" for i in range(1, 6)}
    ws = H.make_workspace(tmp_path, files)
    issues = [
        _issue("ISS-0001", "f1.py", Severity.HIGH, 0.99),      # high 最高分，仍排在 critical 之后
        _issue("ISS-0002", "f2.py", Severity.CRITICAL, 0.50),
        _issue("ISS-0003", "f3.py", Severity.HIGH, 0.90),
        _issue("ISS-0004", "f4.py", Severity.CRITICAL, 0.90),  # 第 1 顺位
        _issue("ISS-0005", "f5.py", Severity.CRITICAL, 0.80),  # 第 2 顺位
    ]
    fake = FakeLLMClient(
        [
            H.llm_json(H.one_line_diff("f4.py", "x = 1", "x = 2")),
            H.llm_json(H.one_line_diff("f5.py", "x = 1", "x = 2")),
        ]
    )
    ctx = H.make_ctx(ws, fake, fake_emitter, fix_max_patches=2)
    ctx.issues = issues

    await run_fix_stage(ctx)

    stats = ctx.extra["fix_stats"]
    assert stats["attempted"] == 2  # 上限生效：5 个候选只尝试 2 个
    assert stats["patch_generated"] == 2
    assert stats["syntax_ok"] == 2
    assert len(ctx.patches) == 2
    # 排序：(critical, 0.90) → (critical, 0.80)
    assert [p.issue_id for p in ctx.patches] == ["ISS-0004", "ISS-0005"]
    assert len(fake.calls) == 2  # 未被选中的 issue 未发起任何 LLM 调用
    assert issues[0].fix_status == FixStatus.NONE  # high 未被处理
    assert ws.abs_path("f1.py").read_bytes() == b"x = 1\n"
    assert ws.abs_path("f4.py").read_text(encoding="utf-8") == "x = 2\n"
    assert ws.abs_path("f5.py").read_text(encoding="utf-8") == "x = 2\n"


async def test_zero_budget_processes_nothing(tmp_path: Path, fake_emitter):
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    fake = FakeLLMClient()
    ctx = H.make_ctx(ws, fake, fake_emitter, fix_max_patches=0)
    ctx.issues = [_issue("ISS-0001", "app.py", Severity.CRITICAL, 1.0)]

    await run_fix_stage(ctx)

    assert ctx.extra["fix_stats"]["attempted"] == 0
    assert fake.calls == []


async def test_second_issue_rereads_file_after_first_patch(tmp_path: Path, fake_emitter):
    """同文件两个 Issue：第二个 Patch 基于已改内容应用（顺序处理）。"""
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY, "tests/test_app.py": H.TEST_APP_PY})
    second_diff = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,3 @@\n"
        " import logging\n"
        "+LOG = logging.getLogger(__name__)\n"
        " \n"
    )
    fake = FakeLLMClient(
        [
            H.llm_json(H.BARE_EXCEPT_DIFF, "第一步：修裸 except"),
            H.llm_json(second_diff, "第二步：整理模块级 logger"),
        ]
    )
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.issues = [
        _issue("ISS-0001", "app.py", Severity.CRITICAL, 0.9, line=9),
        _issue("ISS-0002", "app.py", Severity.HIGH, 0.7, line=1),
    ]

    await run_fix_stage(ctx)

    assert [p.apply_status for p in ctx.patches] == ["verified", "verified"]
    assert [i.fix_status for i in ctx.issues] == [FixStatus.VERIFIED, FixStatus.VERIFIED]
    content = ws.abs_path("app.py").read_text(encoding="utf-8")
    assert "LOG = logging.getLogger(__name__)" in content  # 第二个补丁也已落盘
    assert ctx.extra["fix_stats"]["verified"] == 2


async def test_stage_emits_summary_event_even_without_issues(fake_emitter, tmp_path: Path):
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    ctx = H.make_ctx(ws, FakeLLMClient(), fake_emitter)
    ctx.issues = []

    await run_fix_stage(ctx)

    assert any(e.get("stage") == "fix" and "无可修复" in e.get("message", "") for e in fake_emitter.events)
    assert ctx.extra["fix_stats"]["attempted"] == 0
