"""W7-A3 单测：run_fix_stage 对 JS Issue 的闭环（FakeLLM diff + git apply + node --test 真跑）。

覆盖路径：
- verified：SQL 拼接改参数化 diff + 现有 node:test 全绿（2/2）→ VERIFIED；
- 语法破坏的 JS diff：tree-sitter 重解析失败 → 回滚 + needs-review；
- 无测试的 JS 项目：apply + 语法通过 → syntax-ok（无测试验证的诚实标注）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from audit.llm.base import FakeLLMClient
from audit.models import Category, FixStatus, Issue, Severity

import _fix_helpers as H
import _js_helpers as J
from audit.fix.stage import run_fix_stage

_requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="本机无 node 可执行文件"
)


def _issue(file: str, line: int) -> Issue:
    return Issue(
        id="ISS-0001",
        category=Category.SECURITY,
        severity=Severity.CRITICAL,
        title="SQL 拼接",
        file=file,
        line_start=line,
        line_end=line,
        code_snippet="",
        description="SQL 语句字符串拼接引入变量",
        evidence=["rule_hit: JS-SQL-CONCAT"],
        confidence=0.9,
    )


@_requires_node
async def test_js_issue_verified_with_node_test(tmp_path: Path, fake_emitter):
    """JS Issue + 合法 diff + node:test 现有测试全绿 → verified（2/2）。"""
    ws = J.make_js_workspace(
        tmp_path, {"store.js": J.STORE_JS, "tests/store.test.js": J.TESTS_STORE_TEST_JS}
    )
    fake = FakeLLMClient([J.llm_json(J.JS_STORE_DIFF, "拼接改参数化，由驱动层转义")])
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.issues = [_issue("store.js", 3)]

    await run_fix_stage(ctx)

    patch = ctx.patches[0]
    assert patch.apply_status == "verified"
    assert (patch.tests_run, patch.tests_passed) == (2, 2)
    assert "WHERE name = ?" in patch.diff
    assert ctx.issues[0].fix_status == FixStatus.VERIFIED
    assert ctx.issues[0].patch_id == patch.id
    content = ws.abs_path("store.js").read_text(encoding="utf-8")
    assert "db.all(query, username)" in content
    stats = ctx.extra["fix_stats"]
    assert stats["verified"] == 1 and stats["applied"] == 1


@_requires_node
async def test_js_syntax_broken_diff_rolls_back(tmp_path: Path, fake_emitter):
    """JS diff 应用成功但 tree-sitter 解析失败 → 回滚 + needs-review。"""
    broken_diff = J.JS_STORE_DIFF.replace(
        '+    const query = "SELECT * FROM users WHERE name = ?";',
        '+    const query = "SELECT * FROM users WHERE name = ?" +;',
    )
    ws = J.make_js_workspace(
        tmp_path, {"store.js": J.STORE_JS, "tests/store.test.js": J.TESTS_STORE_TEST_JS}
    )
    fake = FakeLLMClient([J.llm_json(broken_diff, "手滑改坏了")])
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.issues = [_issue("store.js", 3)]

    await run_fix_stage(ctx)

    assert ctx.patches[0].apply_status == "needs-review"
    assert ctx.issues[0].fix_status == FixStatus.NEEDS_REVIEW
    assert ws.abs_path("store.js").read_bytes() == J.STORE_JS.encode("utf-8")


@_requires_node
async def test_js_project_without_tests_gets_syntax_ok(tmp_path: Path, fake_emitter):
    """无现有测试的 JS 项目：apply + 语法通过 → syntax-ok（无测试验证的诚实标注）。"""
    ws = J.make_js_workspace(tmp_path, {"store.js": J.STORE_JS})
    fake = FakeLLMClient([J.llm_json(J.JS_STORE_DIFF)])
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.issues = [_issue("store.js", 3)]

    await run_fix_stage(ctx)

    assert ctx.patches[0].apply_status == "syntax-ok"
    assert ctx.issues[0].fix_status == FixStatus.SYNTAX_OK
    assert (ctx.patches[0].tests_run, ctx.patches[0].tests_passed) == (0, 0)
    content = ws.abs_path("store.js").read_text(encoding="utf-8")
    assert "db.all(query, username)" in content
