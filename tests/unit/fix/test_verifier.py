"""W2-A1 单测：verifier（syntax_ok / find_existing_tests / run_existing_tests）。"""

from __future__ import annotations

from pathlib import Path

from audit.sandbox import SandboxExecutor

import _fix_helpers as H
from audit.fix.verifier import find_existing_tests, run_existing_tests, syntax_ok


# ---------------------------------------------------------------- syntax_ok


def test_syntax_ok_true_for_valid_python(tmp_path: Path):
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    assert syntax_ok(ws, "app.py") is True


def test_syntax_ok_false_for_broken_python(tmp_path: Path):
    ws = H.make_workspace(tmp_path, {"broken.py": "def f(:\n    pass\n"})
    assert syntax_ok(ws, "broken.py") is False


def test_syntax_ok_true_for_unsupported_language(tmp_path: Path):
    """语言不支持时不阻塞（返回 True）。"""
    ws = H.make_workspace(tmp_path, {"main.rb": "puts 'hi'\n"})
    assert syntax_ok(ws, "main.rb") is True


def test_syntax_ok_true_for_missing_file(tmp_path: Path):
    """补丁删除文件后无需解析：返回 True。"""
    ws = H.make_workspace(tmp_path, {})
    assert syntax_ok(ws, "ghost.py") is True


def test_syntax_ok_detects_broken_python_after_change(tmp_path: Path):
    """模拟补丁把文件改坏：改前 True，改后 False。"""
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    assert syntax_ok(ws, "app.py") is True
    H.write_file(ws.abs_path("app.py"), H.APP_PY.replace("    except:", "    except ["))
    assert syntax_ok(ws, "app.py") is False


# ---------------------------------------------------------------- find_existing_tests


def test_find_existing_tests_detects_tests_dir(tmp_path: Path):
    ws = H.make_workspace(tmp_path, {"tests/test_app.py": "def test_ok():\n    assert True\n"})
    assert find_existing_tests(ws) == "pytest"


def test_find_existing_tests_detects_loose_test_file(tmp_path: Path):
    ws = H.make_workspace(tmp_path, {"test_app.py": "def test_ok():\n    assert True\n"})
    assert find_existing_tests(ws) == "pytest"


def test_find_existing_tests_detects_suffix_style_test_file(tmp_path: Path):
    ws = H.make_workspace(tmp_path, {"app_test.py": "def test_ok():\n    assert True\n"})
    assert find_existing_tests(ws) == "pytest"


def test_find_existing_tests_detects_jest(tmp_path: Path):
    ws = H.make_workspace(
        tmp_path,
        {"package.json": '{"name": "demo", "scripts": {"test": "jest"}}', "index.js": "export {};\n"},
    )
    assert find_existing_tests(ws) == "jest"


def test_find_existing_tests_prefers_pytest(tmp_path: Path):
    ws = H.make_workspace(
        tmp_path,
        {"tests/test_app.py": "def test_ok():\n    assert True\n", "package.json": '{"scripts": {"test": "jest"}}'},
    )
    assert find_existing_tests(ws) == "pytest"


def test_find_existing_tests_none_without_tests(tmp_path: Path):
    ws = H.make_workspace(tmp_path, {"package.json": '{"name": "demo", "scripts": {"build": "tsc"}}'})
    assert find_existing_tests(ws) is None


def test_find_existing_tests_ignores_node_modules(tmp_path: Path):
    """node_modules 里的 test_*.py 不算项目测试。"""
    ws = H.make_workspace(
        tmp_path,
        {"node_modules/pkg/test_fake.py": "def test_fake():\n    assert True\n"},
    )
    assert find_existing_tests(ws) is None


# ---------------------------------------------------------------- run_existing_tests


async def test_run_existing_tests_delegates_to_sandbox(tmp_path: Path):
    ws = H.make_workspace(tmp_path, {"test_ok.py": "def test_ok():\n    assert 1 == 1\n"})
    result = await run_existing_tests(SandboxExecutor(), ws, "pytest")
    assert result.exit_code == 0
    assert not result.timed_out
    assert "1 passed" in result.stdout_tail + result.stderr_tail


async def test_run_existing_tests_reports_failure(tmp_path: Path):
    ws = H.make_workspace(tmp_path, {"test_bad.py": "def test_bad():\n    assert 1 == 2\n"})
    result = await run_existing_tests(SandboxExecutor(), ws, "pytest")
    assert result.exit_code != 0
    assert "1 failed" in result.stdout_tail + result.stderr_tail
