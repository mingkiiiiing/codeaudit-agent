"""W7-A3 单测：verifier 的 JS/TS 测试框架探测（node-test）与沙箱运行。

- 无 package.json 但存在 *.test.js/*.test.ts（或 tests/ 下 js 文件）且 node 可用
  → "node-test"；node 不可用 → None（诚实降级）；
- tests/ 目录只含 js 文件时不得误判 pytest（回归保护）；
- package.json 语义维持：有 test 脚本 → jest；无 test 脚本 → 不启用 node-test；
- 语法重解析对 js/ts 直接可用。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import audit.fix.verifier as verifier_mod
import _fix_helpers as H
import _js_helpers as J
from audit.fix.verifier import find_existing_tests, run_existing_tests, syntax_ok
from audit.sandbox import SandboxExecutor

_requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="本机无 node 可执行文件"
)


# ---------------------------------------------------------------- 探测：node-test


@_requires_node
def test_js_project_without_package_json_returns_node_test(tmp_path: Path):
    ws = J.make_js_workspace(
        tmp_path, {"store.js": J.STORE_JS, "tests/store.test.js": J.TESTS_STORE_TEST_JS}
    )
    assert find_existing_tests(ws) == "node-test"


@_requires_node
def test_loose_test_file_returns_node_test(tmp_path: Path):
    ws = J.make_js_workspace(tmp_path, {"src/app.test.js": "test('x', () => {});\n"})
    assert find_existing_tests(ws) == "node-test"


@_requires_node
def test_typescript_test_file_returns_node_test(tmp_path: Path):
    ws = J.make_js_workspace(tmp_path, {"src/util.test.ts": "export {};\n"})
    assert find_existing_tests(ws) == "node-test"


@_requires_node
def test_tests_dir_with_only_js_is_not_pytest(tmp_path: Path):
    """tests/ 目录只含 js 文件：不得误判 pytest，应落 node-test。"""
    ws = J.make_js_workspace(tmp_path, {"tests/util.test.js": "test('x', () => {});\n"})
    assert find_existing_tests(ws) == "node-test"


def test_node_unavailable_returns_none(tmp_path: Path, monkeypatch):
    """node 不可用：探测返回 None（fix 阶段诚实降级 syntax-ok），不误报。"""
    monkeypatch.setattr(verifier_mod, "test_runner_available", lambda fw: False)
    ws = J.make_js_workspace(tmp_path, {"app.test.js": "test('x', () => {});\n"})
    assert find_existing_tests(ws) is None


# ---------------------------------------------------------------- 探测：package.json 语义


def test_package_json_without_test_script_not_node_test(tmp_path: Path):
    """有 package.json（无 test 脚本）时不启用 node-test（维持探测契约）。"""
    ws = J.make_js_workspace(
        tmp_path,
        {"package.json": '{"name": "demo", "scripts": {"build": "tsc"}}', "app.test.js": "test('x', () => {});\n"},
    )
    assert find_existing_tests(ws) is None


@_requires_node
def test_package_json_with_jest_wins(tmp_path: Path):
    ws = J.make_js_workspace(
        tmp_path,
        {
            "package.json": '{"scripts": {"test": "jest"}}',
            "app.test.js": "test('x', () => {});\n",
        },
    )
    assert find_existing_tests(ws) == "jest"


# ---------------------------------------------------------------- 探测：Python 行为不变


def test_python_tests_still_pytest(tmp_path: Path):
    ws = H.make_workspace(tmp_path, {"tests/test_app.py": "def test_ok():\n    assert True\n"})
    assert find_existing_tests(ws) == "pytest"


@_requires_node
def test_mixed_project_pytest_wins(tmp_path: Path):
    """py + js 测试并存：pytest 优先（现有语义不回退）。"""
    ws = J.make_js_workspace(
        tmp_path,
        {
            "tests/test_app.py": "def test_ok():\n    assert True\n",
            "tests/util.test.js": "test('x', () => {});\n",
        },
    )
    assert find_existing_tests(ws) == "pytest"


# ---------------------------------------------------------------- 语法重解析（js/ts）


def test_syntax_ok_for_javascript(tmp_path: Path):
    ws = J.make_js_workspace(tmp_path, {"store.js": J.STORE_JS})
    assert syntax_ok(ws, "store.js") is True


def test_syntax_broken_javascript_detected(tmp_path: Path):
    ws = J.make_js_workspace(tmp_path, {"broken.js": "function f( {\n"})
    assert syntax_ok(ws, "broken.js") is False


def test_syntax_broken_typescript_detected(tmp_path: Path):
    ws = J.make_js_workspace(tmp_path, {"broken.ts": "const x: number = ;\n"})
    assert syntax_ok(ws, "broken.ts") is False


# ---------------------------------------------------------------- 沙箱运行（node-test）


@_requires_node
async def test_run_existing_tests_node_test_real(tmp_path: Path):
    good_mjs = (
        "import { test } from 'node:test';\n"
        "import assert from 'node:assert';\n"
        "test('ok', () => { assert.strictEqual(1 + 1, 2); });\n"
    )
    ws = J.make_js_workspace(tmp_path, {"tests/good.test.mjs": good_mjs})
    result = await run_existing_tests(SandboxExecutor(), ws, "node-test")
    assert result.exit_code == 0
    assert not result.timed_out
    assert "pass 1" in result.stdout_tail + result.stderr_tail


async def test_run_existing_tests_node_test_missing_node(tmp_path: Path, monkeypatch):
    import audit.sandbox.executor as executor_mod

    monkeypatch.setattr(executor_mod.shutil, "which", lambda name: None)
    ws = J.make_js_workspace(tmp_path, {"app.test.js": "test('x', () => {});\n"})
    result = await run_existing_tests(SandboxExecutor(), ws, "node-test")
    assert result.exit_code == 127
    assert "node" in result.stderr_tail
