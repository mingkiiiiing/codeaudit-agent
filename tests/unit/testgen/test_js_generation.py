"""W7-A3 单测：testgen 的 JS/TS 生成闭环（FakeLLM node:test 代码 → .test.mjs → node --test 真跑）。

覆盖路径：
- validate_code_js：合法代码 / 语法错拒绝（node --check）/ require 拒绝（ESM 守卫）/
  无 assert 拒绝 / 第三方框架拒绝；node 不可用时跳过语法校验（syntax_checked=False）；
- happy：FakeLLM 合法 node:test 用例 → 写盘 .test.mjs → node --test 通过 → passed；
- 重试：[坏用例, 好用例] → node --test 报错回喂后通过；
- require 版本被校验拒绝后回喂重试；
- 三次全坏 → dropped + 生成文件清理；
- node 不可用 → 该语言目标 emit 警告并跳过（无落盘无记录）；
- 混合语言分派：py→pytest 与 js→node-test 同场跑通；
- prompt 变体与导入说明符、kind 注释解析、断言启发式计数。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import audit.testgen.stage as stage_mod
from audit.llm.base import FakeLLMClient

import _js_fixtures as J
import _testgen_helpers as H
from audit.testgen.generator import build_testgen_messages, count_asserts
from audit.testgen.stage import run_testgen_stage

_requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="本机无 node 可执行文件"
)


def _js_workspace(tmp_path: Path, files: dict[str, str] | None = None):
    # testgen 面向修复后函数生成回归用例 → 用已参数化的 store.js（与端到端一致）
    return J.make_js_workspace(tmp_path, files or {J.STORE: J.STORE_JS_FIXED})


# 与 test_testgen_stage.py 同源的最小 Python 目标函数（混合语言分派用）
MATHX_SOURCE = """def compare(a, b):
    if a == None:
        return False
    return a == b
"""


def _assert_no_pollution(workspace) -> None:
    tests_root = workspace.src_root / "tests"
    if not tests_root.is_dir():
        return
    for path in tests_root.rglob("*"):
        if path.is_file():
            rel_parts = path.relative_to(tests_root).parts
            assert "generated" in rel_parts, f"污染用户测试目录：{path}"


# ---------------------------------------------------------------- 静态校验


@_requires_node
def test_validate_code_js_accepts_good():
    from audit.testgen.generator import validate_code_js

    errors, syntax_checked = validate_code_js(J.ESM_TESTGEN_CODE)
    assert errors == []
    assert syntax_checked is True


@_requires_node
def test_validate_code_js_rejects_syntax_error():
    from audit.testgen.generator import validate_code_js

    broken = "import { test } from 'node:test';\nfunction broken( {\n"
    errors, syntax_checked = validate_code_js(broken)
    assert syntax_checked is True
    assert any("语法校验失败" in e for e in errors)


def test_validate_code_js_rejects_require():
    from audit.testgen.generator import validate_code_js

    errors, _checked = validate_code_js(J.ESM_TESTGEN_REQUIRE_CODE)
    assert any("require" in e for e in errors)


def test_validate_code_js_rejects_no_assert():
    from audit.testgen.generator import validate_code_js

    code = "import { test } from 'node:test';\ntest('x', () => { const a = 1; });\n"
    errors, _checked = validate_code_js(code)
    assert any("缺少显式 assert" in e for e in errors)


def test_validate_code_js_rejects_third_party_framework():
    from audit.testgen.generator import validate_code_js

    code = "import { describe } from 'vitest';\nconst x = 1;\nassert.strictEqual(x, 1);\n"
    errors, _checked = validate_code_js(code)
    assert any("第三方测试框架" in e for e in errors)


def test_validate_code_js_skips_syntax_when_node_missing(monkeypatch):
    """node 不可用：跳过语法校验并标注（syntax_checked=False），静态校验照常。"""
    from audit.testgen import generator

    monkeypatch.setattr(generator.shutil, "which", lambda name: None)
    errors, syntax_checked = generator.validate_code_js(J.ESM_TESTGEN_CODE)
    assert errors == []
    assert syntax_checked is False


def test_count_asserts_js_heuristic():
    assert count_asserts(J.ESM_TESTGEN_CODE, "javascript") == 3
    assert count_asserts("const x = 1;", "javascript") == 0


def test_generated_rel_path_js_suffix():
    from audit.testgen.runner import generated_rel_path

    assert generated_rel_path(J.STORE, "javascript") == "tests/generated/test_gen_store.test.mjs"
    assert generated_rel_path("app/utils/mathx.py") == "tests/generated/test_gen_app_utils_mathx.py"


def test_js_import_specifier():
    from audit.testgen.generator import js_import_specifier

    assert js_import_specifier(J.STORE) == "../../store.js"
    assert js_import_specifier("app/utils/mathx.js") == "../../app/utils/mathx.js"


def test_js_prompt_variant(tmp_path):
    ws = _js_workspace(tmp_path)
    messages = build_testgen_messages(ws, None, J.STORE, "findUser", language="javascript")
    assert "node:test" in messages[0]["content"]
    assert "../../store.js" in messages[1]["content"]
    # 默认 python 路径不变
    py_messages = build_testgen_messages(ws, None, J.STORE, "findUser")
    assert "pytest" in py_messages[0]["content"]


# ---------------------------------------------------------------- 生成闭环（真跑 node --test）


@_requires_node
async def test_js_generation_happy_path(tmp_path, fake_emitter):
    ws = _js_workspace(tmp_path)
    fake = FakeLLMClient([{"content": J.fenced_js(J.ESM_TESTGEN_CODE)}])
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.extra["testgen_targets"] = [(J.STORE, "findUser")]

    await run_testgen_stage(ctx)

    assert len(ctx.test_cases) == 1
    tc = ctx.test_cases[0]
    assert tc.status == "passed"
    assert tc.target == f"{J.STORE}:findUser"
    assert tc.file == "tests/generated/test_gen_store.test.mjs"
    assert tc.kind == "normal"
    assert tc.assert_count == 3
    gen_file = ws.abs_path(tc.file)
    assert gen_file.is_file()
    text = gen_file.read_text(encoding="utf-8")
    assert "// 由 CodeAudit 自动生成" in text
    assert "import { findUser } from '../../store.js';" in text
    assert ctx.extra["testgen_stats"]["passed"] == 1
    _assert_no_pollution(ws)


@_requires_node
async def test_js_retry_after_runtime_failure(tmp_path, fake_emitter):
    """[断言错误版本, 好版本]：node --test 失败报错回喂 → 第二版通过。"""
    ws = _js_workspace(tmp_path)
    fake = FakeLLMClient(
        [
            {"content": J.fenced_js(J.ESM_TESTGEN_BAD_CODE)},
            {"content": J.fenced_js(J.ESM_TESTGEN_CODE)},
        ]
    )
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.extra["testgen_targets"] = [(J.STORE, "findUser")]

    await run_testgen_stage(ctx)

    assert [tc.status for tc in ctx.test_cases] == ["passed"]
    assert ctx.extra["testgen_stats"] == {
        "targets": 1, "generated": 2, "passed": 1, "dropped": 0, "retries": 1,
    }
    assert "node --test 未通过" in fake.calls[1]["messages"][1]["content"]
    # 旧失败块被剥离，最终文件只保留当前版本
    text = ws.abs_path(ctx.test_cases[0].file).read_text(encoding="utf-8")
    assert "'boss'" not in text
    _assert_no_pollution(ws)


@_requires_node
async def test_js_require_version_rejected_then_retry(tmp_path, fake_emitter):
    """require 版本被 ESM 守卫拒绝（不落盘）→ 回喂后 ESM 版本通过。"""
    ws = _js_workspace(tmp_path)
    fake = FakeLLMClient(
        [
            {"content": J.fenced_js(J.ESM_TESTGEN_REQUIRE_CODE)},
            {"content": J.fenced_js(J.ESM_TESTGEN_CODE)},
        ]
    )
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.extra["testgen_targets"] = [(J.STORE, "findUser")]

    await run_testgen_stage(ctx)

    assert [tc.status for tc in ctx.test_cases] == ["passed"]
    assert ctx.extra["testgen_stats"]["retries"] == 1
    assert "require" in fake.calls[1]["messages"][1]["content"]


@_requires_node
async def test_js_all_attempts_fail_drops_and_cleans(tmp_path, fake_emitter):
    ws = _js_workspace(tmp_path)
    fake = FakeLLMClient([{"content": J.fenced_js(J.ESM_TESTGEN_BAD_CODE)}] * 3)
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.extra["testgen_targets"] = [(J.STORE, "findUser")]

    await run_testgen_stage(ctx)

    assert len(ctx.test_cases) == 1
    tc = ctx.test_cases[0]
    assert tc.status == "dropped"
    assert tc.file == "tests/generated/test_gen_store.test.mjs"
    assert not ws.abs_path(tc.file).exists()
    assert ctx.extra["testgen_stats"]["dropped"] == 1
    _assert_no_pollution(ws)


async def test_js_target_skipped_when_node_unavailable(tmp_path, fake_emitter, monkeypatch):
    """node 不可用：JS 目标 emit 警告并跳过——零 LLM 调用、零落盘、零记录。"""
    monkeypatch.setattr(stage_mod, "test_runner_available", lambda fw: False)
    ws = _js_workspace(tmp_path)
    fake = FakeLLMClient([])
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.extra["testgen_targets"] = [(J.STORE, "findUser")]

    await run_testgen_stage(ctx)

    assert ctx.test_cases == []
    assert fake.calls == []
    assert not (ws.src_root / "tests" / "generated").exists()
    assert any("node 不可用" in e.get("message", "") for e in fake_emitter.events)


@_requires_node
async def test_mixed_python_and_js_targets_dispatch(tmp_path, fake_emitter):
    """py→pytest 与 js→node-test 同场：按语言分派，两个目标都真跑通过。"""
    ws = H.make_workspace(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/utils/__init__.py": "",
            "app/utils/mathx.py": MATHX_SOURCE,
            J.STORE: J.STORE_JS_FIXED,
        },
    )
    fake = FakeLLMClient(
        [
            {"content": H.fenced(H.GOOD_TEST_CODE)},  # py 目标（排序在前）
            {"content": J.fenced_js(J.ESM_TESTGEN_CODE)},  # js 目标
        ]
    )
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.extra["testgen_targets"] = [("app/utils/mathx.py", "compare"), (J.STORE, "findUser")]

    await run_testgen_stage(ctx)

    assert [tc.status for tc in ctx.test_cases] == ["passed", "passed"]
    assert ctx.test_cases[0].file.endswith(".py")
    assert ctx.test_cases[1].file.endswith(".test.mjs")
    assert ctx.extra["testgen_stats"]["passed"] == 2


async def test_js_untestable_skips(tmp_path, fake_emitter):
    ws = _js_workspace(tmp_path)
    fake = FakeLLMClient([{"content": "// UNTESTABLE: 强耦合全局状态"}])
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.extra["testgen_targets"] = [(J.STORE, "findUser")]

    await run_testgen_stage(ctx)

    assert ctx.test_cases == []
    assert not (ws.src_root / "tests" / "generated").exists()
