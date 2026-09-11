"""Stage5 验证器：补丁后语法重解析、现有测试框架探测与沙箱运行。

只调用 Wave 1 成果（audit.indexer 的 tree-sitter 封装、audit.sandbox 的白名单
测试执行器），不做任何重写。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from audit.indexer import SUPPORTED_LANGUAGES
from audit.indexer.parsers import parse_source
from audit.sandbox import SandboxExecutor, SandboxResult
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


def find_existing_tests(workspace: WorkspaceContext) -> str | None:
    """探测项目现有测试框架：pytest / jest；都没有返回 None。

    - 存在 tests/ 目录或 test_*.py / *_test.py 文件 → "pytest"；
    - package.json 的 scripts 含 test 脚本 → "jest"（pytest 优先）。
    """
    root = workspace.src_root
    if (root / "tests").is_dir() or (root / "test").is_dir():
        return "pytest"
    for path in _walk_files(root):
        name = path.name
        if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")):
            return "pytest"
    package_json = root / "package.json"
    if package_json.is_file():
        try:
            scripts = json.loads(package_json.read_text(encoding="utf-8")).get("scripts") or {}
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            scripts = {}
        if "test" in scripts:
            return "jest"
    return None


async def run_existing_tests(
    sandbox: SandboxExecutor, workspace: WorkspaceContext, framework: str
) -> SandboxResult:
    """在沙箱中运行现有测试：直接委托 audit.sandbox（白名单 pytest/jest）。

    超时等失败语义由沙箱层保证（timed_out / exit_code），本函数不吞不抛。
    """
    return await sandbox.run_tests(framework, workspace.src_root)
