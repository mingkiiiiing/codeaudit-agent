"""生成测试的落盘、沙箱运行与剔除（W2-A2 Stage6 单测生成闭环）。

只写 src_root/tests/generated/（FR-5.4：不污染用户已有测试，可整目录删除）：
- 文件名 test_gen_<module>.py，module 由目标文件路径转下划线
  （app/utils/mathx.py → app_utils_mathx）；
- 同一模块多个目标共用一个文件：每个目标一个带标记的代码块，已存在则追加；
- remove_generated 支持按块剔除（失败目标的代码块被剥离，其余目标保留），
  剥离后仅剩头注释则连文件一起删除（目录为空时一并移除）。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from audit.sandbox import SandboxExecutor, SandboxResult
from audit.workspace import WorkspaceContext

__all__ = [
    "generated_rel_path",
    "write_generated",
    "run_generated",
    "remove_generated",
]

GENERATED_DIR = "tests/generated"

_HEADER_LINE = "# 由 CodeAudit 自动生成（TestGen），可整目录删除：tests/generated/。"
_BLOCK_PREFIX = "# ---- CodeAudit block: "


def _block_mark(file: str, symbol: str) -> str:
    return f"{_BLOCK_PREFIX}{file}:{symbol} ----"


def generated_rel_path(file: str) -> str:
    """由目标文件路径推导生成文件的 src 内相对路径（posix）。

    app/utils/mathx.py → tests/generated/test_gen_app_utils_mathx.py
    """
    normalized = Path(str(file).replace("\\", "/")).with_suffix("").as_posix()
    module = re.sub(r"[^0-9A-Za-z]+", "_", normalized).strip("_") or "module"
    return f"{GENERATED_DIR}/test_gen_{module}.py"


def write_generated(workspace: WorkspaceContext, file: str, symbol: str, code: str) -> Path:
    """把生成的测试代码写入 tests/generated/，返回生成文件的绝对路径。

    文件不存在时创建（带头注释）；已存在时追加并加分隔块标记
    （块标记供 remove_generated 按目标剔除）。
    """
    rel = generated_rel_path(file)
    path = workspace.abs_path(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    block = _block_mark(file, symbol) + "\n" + code.rstrip() + "\n"
    if path.exists():
        existing = path.read_text(encoding="utf-8", errors="replace")
        if _HEADER_LINE not in existing:
            existing = _HEADER_LINE + "\n" + existing
        content = existing.rstrip() + "\n\n" + block
    else:
        content = _HEADER_LINE + "\n" + block
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


async def run_generated(sandbox: SandboxExecutor, workspace: WorkspaceContext, gen_path: Path) -> SandboxResult:
    """在沙箱中运行生成的测试文件：委托 run_tests("pytest", cwd=src_root, target=相对路径)。

    超时/失败语义由沙箱层保证（timed_out / exit_code），本函数不吞不抛。
    """
    target = workspace.rel(gen_path)
    return await sandbox.run_tests("pytest", workspace.src_root, target)


def remove_generated(
    workspace: WorkspaceContext,
    gen_path: Path,
    file: str | None = None,
    symbol: str | None = None,
) -> None:
    """剔除生成的测试文件（stage 重试耗尽或失败时清理）。

    - 带 file+symbol：只剥离该目标的代码块；剥离后若文件仅剩头注释/空白，
      删除文件（generated 目录为空时一并移除）；
    - 不带 file+symbol（或文件中找不到对应块标记）：整个文件删除（该文件
      完全由 CodeAudit 生成，可安全删除）。
    """
    path = Path(gen_path)
    if not path.exists():
        return
    if not (file and symbol):
        path.unlink(missing_ok=True)
        _rmdir_if_empty(path.parent)
        return
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    mark = _block_mark(file, symbol)
    try:
        start = lines.index(mark)
    except ValueError:
        # 找不到块标记：无法安全剥离，整文件删除
        path.unlink(missing_ok=True)
        _rmdir_if_empty(path.parent)
        return
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith(_BLOCK_PREFIX):
            end = i
            break
    remaining = lines[:start] + lines[end:]
    meaningful = [ln for ln in remaining if ln.strip() and not ln.strip().startswith("#")]
    if meaningful:
        path.write_text("\n".join(remaining).rstrip() + "\n", encoding="utf-8", newline="\n")
    else:
        path.unlink(missing_ok=True)
        _rmdir_if_empty(path.parent)


def _rmdir_if_empty(directory: Path) -> None:
    """清理 generated 目录：先移除其中的 __pycache__（pytest 运行产物），目录为空时删除。

    只清理 generated 目录自身的字节码缓存，不触碰其他内容；失败静默。
    """
    try:
        shutil.rmtree(directory / "__pycache__", ignore_errors=True)
        directory.rmdir()
    except OSError:
        pass
