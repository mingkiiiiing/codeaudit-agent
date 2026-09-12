"""Stage5 验证器：补丁后语法重解析、现有测试框架探测与沙箱运行。

只调用 Wave 1 成果（audit.indexer 的 tree-sitter 封装、audit.sandbox 的白名单
测试执行器），不做任何重写。

W7-A3：find_existing_tests 扩展 JS/TS——无 package.json 但存在 *.test.js/*.test.ts
等 JS 测试文件且 node 可用时返回 "node-test"（node --test，零依赖）；node 不可用
返回 None（fix 阶段按"无现有测试"诚实降级为 syntax-ok）。语法重解析对 js/ts 由
audit.indexer.parsers 原生支持，fix 闭环对 JS/TS Issue 直接可用（stage 选 Issue
只按 severity，不按语言过滤）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from audit.indexer import SUPPORTED_LANGUAGES
from audit.indexer.parsers import parse_source
from audit.sandbox import SandboxExecutor, SandboxResult, test_runner_available
from audit.utils import guess_language
from audit.workspace import DEFAULT_IGNORE_DIRS, WorkspaceContext

__all__ = ["syntax_ok", "find_existing_tests", "run_existing_tests"]


def syntax_ok(workspace: WorkspaceContext, rel_path: str) -> bool:
    """用 tree-sitter 重新解析补丁后的文件做语法校验。

    语言不支持或文件不存在（如补丁删除了文件）时不阻塞，返回 True；
    解析失败（root_node.has_error）返回 False。
    """
    language = guess_language(str(rel_path).replace("\\", "/"))
    if language not in SUPPORTED_LANGUAGES:
        return True
    try:
        data = workspace.abs_path(rel_path).read_bytes()
    except (FileNotFoundError, OSError):
        return True
    tree, parsed_ok = parse_source(language, data)
    if tree is None:  # 解析器异常兜底：不阻塞（语言已支持仍解析失败属环境问题）
        return True
    return parsed_ok


def _walk_files(root: Path) -> Iterator[Path]:
    """遍历工作副本文件，跳过默认忽略目录（node_modules/.venv 等）。"""
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in DEFAULT_IGNORE_DIRS for part in rel.parts[:-1]):
            continue
        yield path


# JS/TS 测试文件识别（W7-A3）：*.test.* / *.spec.* 等与 node --test 默认发现模式对齐
_JS_SUFFIXES = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx")
_TEST_NAME_PARTS = (".test", ".spec", "-test", "_test")
_TEST_DIR_NAMES = ("tests", "test")


def _is_js_test_file(name: str) -> bool:
    """按文件名判断是否为 JS/TS 测试文件（*.test.js / *.spec.ts / test-*.mjs / test.js 等）。"""
    lowered = name.lower()
    if not lowered.endswith(_JS_SUFFIXES):
        return False
    stem = lowered.rsplit(".", 1)[0]
    if stem in ("test", "spec"):
        return True
    return any(part in stem for part in _TEST_NAME_PARTS) or stem.startswith("test-")


def _dir_has(root: Path, dirname: str, suffixes: tuple[str, ...]) -> bool:
    """tests/ 目录下是否存在给定后缀的文件（跳过忽略目录，深度不限）。"""
    base = root / dirname
    if not base.is_dir():
        return False
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in DEFAULT_IGNORE_DIRS for part in rel.parts[:-1]):
            continue
        if path.suffix.lower() in suffixes:
            return True
    return False


def _has_js_test_files(root: Path) -> bool:
    """项目中是否存在 JS/TS 测试文件：tests(test)/ 目录下的 js 文件，或任意 *.test.* 文件。"""
    for dirname in _TEST_DIR_NAMES:
        if _dir_has(root, dirname, _JS_SUFFIXES):
            return True
    for path in _walk_files(root):
        if _is_js_test_file(path.name):
            return True
    return False


def find_existing_tests(workspace: WorkspaceContext) -> str | None:
    """探测项目现有测试框架：pytest / jest / node-test；都没有返回 None。

    - tests(test)/ 目录（含 py 文件）或 test_*.py / *_test.py 文件 → "pytest"；
    - package.json 的 scripts 含 test 脚本 → "jest"（pytest 优先）；
    - W7-A3：无 package.json 但存在 JS 测试文件（tests/ 下 js 文件或
      *.test.js/*.test.ts 等）且 node 可用 → "node-test"；
      node 不可用返回 None（fix 阶段诚实降级 syntax-ok，不会误跑 pytest）。
    """
    root = workspace.src_root
    tests_dir_present = any((root / d).is_dir() for d in _TEST_DIR_NAMES)
    if tests_dir_present and any(
        _dir_has(root, d, (".py",)) for d in _TEST_DIR_NAMES
    ):
        return "pytest"
    for path in _walk_files(root):
        name = path.name
        if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")):
            return "pytest"
    package_json = root / "package.json"
    has_package_json = package_json.is_file()
    if has_package_json:
        try:
            scripts = json.loads(package_json.read_text(encoding="utf-8")).get("scripts") or {}
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            scripts = {}
        if "test" in scripts:
            return "jest"
    if not has_package_json and _has_js_test_files(root) and test_runner_available("node-test"):
        return "node-test"
    return None


async def run_existing_tests(
    sandbox: SandboxExecutor, workspace: WorkspaceContext, framework: str
) -> SandboxResult:
    """在沙箱中运行现有测试：直接委托 audit.sandbox（白名单 pytest/jest/node-test）。

    超时等失败语义由沙箱层保证（timed_out / exit_code），本函数不吞不抛。
    """
    return await sandbox.run_tests(framework, workspace.src_root)
