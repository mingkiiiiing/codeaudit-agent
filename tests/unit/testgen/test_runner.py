"""W2-A2 单测：write_generated / run_generated / remove_generated（落盘、运行委托、剔除）。

覆盖：
- 命名规则（路径转下划线）、文件头标注、追加块标记；
- remove_generated 按块剔除：保留其他目标、仅剩头注释则删文件、目录为空一并删除；
- run_generated 委托沙箱（framework/cwd/target 参数正确传递）。
"""

from __future__ import annotations

from pathlib import Path

from audit.sandbox import SandboxResult

import _testgen_helpers as H
from audit.testgen.runner import generated_rel_path, remove_generated, run_generated, write_generated

MATHX = "app/utils/mathx.py"


def test_generated_rel_path_naming():
    assert generated_rel_path(MATHX) == "tests/generated/test_gen_app_utils_mathx.py"
    assert generated_rel_path("app.py") == "tests/generated/test_gen_app.py"
    assert generated_rel_path("app\\utils\\mathx.py") == "tests/generated/test_gen_app_utils_mathx.py"


def test_write_generated_creates_file_with_header(tmp_path):
    ws = H.make_workspace(tmp_path, {MATHX: "x = 1\n"})
    path = write_generated(ws, MATHX, "compare", H.GOOD_TEST_CODE)

    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "由 CodeAudit 自动生成" in text and "可整目录删除" in text
    assert "# ---- CodeAudit block: app/utils/mathx.py:compare ----" in text
    assert "def test_compare_equal_values():" in text


def test_write_generated_appends_with_block_mark(tmp_path):
    """同模块第二个目标：追加新块而非覆盖，前一块保留。"""
    ws = H.make_workspace(tmp_path, {MATHX: "x = 1\n"})
    write_generated(ws, MATHX, "compare", H.GOOD_TEST_CODE)
    write_generated(ws, MATHX, "build_page", H.FAILING_TEST_CODE)

    text = ws.abs_path(generated_rel_path(MATHX)).read_text(encoding="utf-8")
    assert "app/utils/mathx.py:compare ----" in text
    assert "app/utils/mathx.py:build_page ----" in text
    assert text.index("compare ----") < text.index("build_page ----")


def test_remove_generated_strips_block_and_keeps_other(tmp_path):
    ws = H.make_workspace(tmp_path, {MATHX: "x = 1\n"})
    gen_path = write_generated(ws, MATHX, "compare", H.GOOD_TEST_CODE)
    write_generated(ws, MATHX, "build_page", H.FAILING_TEST_CODE)

    remove_generated(ws, gen_path, MATHX, "compare")

    text = gen_path.read_text(encoding="utf-8")
    assert "test_compare_equal_values" not in text
    assert "build_page" in text
    assert "由 CodeAudit 自动生成" in text


def test_remove_generated_deletes_file_when_only_header_left(tmp_path):
    ws = H.make_workspace(tmp_path, {MATHX: "x = 1\n"})
    gen_path = write_generated(ws, MATHX, "compare", H.GOOD_TEST_CODE)

    remove_generated(ws, gen_path, MATHX, "compare")

    assert not gen_path.exists()
    assert not (ws.src_root / "tests" / "generated").exists()  # 空目录一并移除


def test_remove_generated_whole_file_without_target(tmp_path):
    ws = H.make_workspace(tmp_path, {MATHX: "x = 1\n"})
    gen_path = write_generated(ws, MATHX, "compare", H.GOOD_TEST_CODE)

    remove_generated(ws, gen_path)

    assert not gen_path.exists()


def test_remove_generated_missing_file_is_noop(tmp_path):
    ws = H.make_workspace(tmp_path, {MATHX: "x = 1\n"})
    remove_generated(ws, ws.abs_path("tests/generated/test_gen_app_utils_mathx.py"), MATHX, "compare")


async def test_run_generated_delegates_to_sandbox(tmp_path):
    """run_generated 委托 sandbox.run_tests("pytest", src_root, 相对路径)。"""
    ws = H.make_workspace(tmp_path, {MATHX: "x = 1\n"})
    gen_path = write_generated(ws, MATHX, "compare", H.GOOD_TEST_CODE)

    calls: list[tuple] = []

    class StubSandbox:
        async def run_tests(self, framework, cwd, target=""):
            calls.append((framework, Path(cwd), target))
            return SandboxResult(exit_code=0, stdout_tail="1 passed")

    result = await run_generated(StubSandbox(), ws, gen_path)  # type: ignore[arg-type]

    assert result.exit_code == 0
    assert calls == [("pytest", ws.src_root, "tests/generated/test_gen_app_utils_mathx.py")]
