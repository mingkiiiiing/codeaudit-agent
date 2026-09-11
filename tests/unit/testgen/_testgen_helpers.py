"""tests/unit/testgen 共享工具：临时项目构造、FakeIndex、样例代码与 PipelineContext 工厂。

全部离线：LLM 一律用 FakeLLMClient 脚本回放；沙箱跑真实 pytest 子进程（无网络）。
样例目标函数取 demo_proj/app/utils/mathx.py 的 compare（真跑可验证）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from audit.config import AuditConfig
from audit.models import FunctionSlice, Patch
from audit.pipeline import PipelineContext
from audit.workspace import WorkspaceContext

# ---------------------------------------------------------------- 样例 diff / 代码

# verified Patch 样例：指向 app/utils/mathx.py 的 compare（切片区间 10-13，hunk 10-14 相交）
MATHX_COMPARE_DIFF = (
    "diff --git a/app/utils/mathx.py b/app/utils/mathx.py\n"
    "--- a/app/utils/mathx.py\n"
    "+++ b/app/utils/mathx.py\n"
    "@@ -10,4 +10,5 @@\n"
    " def compare(a, b):\n"
    "-    if a == None:\n"
    "+    if a is None:\n"
    "         return False\n"
    "     return a == b\n"
)

# FakeLLM 返回的合法 pytest 代码（真跑全绿；含 kind 注释与 ≥2 assert）
GOOD_TEST_CODE = """from app.utils.mathx import compare


# kind: normal
def test_compare_equal_values():
    assert compare(3, 3) is True
    assert compare("a", "a") is True


# kind: boundary
def test_compare_none_inputs():
    assert compare(None, 3) is False
    assert compare(None, None) is False


# kind: boundary
def test_compare_empty_vs_zero():
    assert compare("", 0) is False
"""

# 通过静态校验、但 pytest 必跑挂的代码（断言值推演错误）
FAILING_TEST_CODE = """from app.utils.mathx import compare


# kind: normal
def test_compare_wrong_expectation():
    assert compare(3, 3) == 999
"""

# 缺显式 assert：被 validate_code 拒绝（不落盘、不触发沙箱）
NO_ASSERT_CODE = """from app.utils.mathx import compare


# kind: normal
def test_compare_no_assert():
    compare(1, 2)
"""

UNTESTABLE_TEXT = "# UNTESTABLE: 函数强耦合模块级全局状态，无法隔离测试"


def fenced(code: str) -> str:
    """把代码包成 ```python 围栏（模拟 LLM 原始输出）。"""
    return f"```python\n{code}```"


def verified_patch(diff: str = MATHX_COMPARE_DIFF, patch_id: str = "PATCH-0001") -> Patch:
    """构造 apply_status=verified 的 Patch（testgen 只读 diff，不重新应用）。"""
    return Patch(id=patch_id, issue_id="ISS-0001", diff=diff, apply_status="verified")


# ---------------------------------------------------------------- FakeIndex


class FakeIndex:
    """切片桩：只实现 slices_for_file / get_symbol，其余不需要。"""

    def __init__(self, slices_by_file: dict[str, list[FunctionSlice]]):
        self._by_file = slices_by_file

    def slices_for_file(self, rel_path: str) -> list[FunctionSlice]:
        return list(self._by_file.get(str(rel_path).replace("\\", "/"), []))

    def get_symbol(self, name: str, file_hint: str | None = None) -> None:
        return None


def slice_of(file: str, symbol: str, start: int, end: int) -> FunctionSlice:
    return FunctionSlice(
        file=file, symbol=symbol, line_start=start, line_end=end,
        code=f"def {symbol.split('.')[-1]}():\n    ...", signature=f"def {symbol.split('.')[-1]}():",
    )


# ---------------------------------------------------------------- 工厂函数


def write_file(path: Path, content: str) -> None:
    """以 UTF-8 + LF 写文件（保证与沙箱/pytest 的行为一致）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def make_workspace(tmp_path: Path, files: dict[str, str]) -> WorkspaceContext:
    """按 {相对路径: 内容} 造工作副本，返回指向它的 WorkspaceContext。"""
    src = tmp_path / "src"
    for rel, content in files.items():
        write_file(src / rel, content)
    return WorkspaceContext(
        audit_id="testgen", src_root=src, work_root=tmp_path, db_path=tmp_path / "index.db"
    )


def make_ctx(
    workspace: WorkspaceContext, llm: Any, emitter: Any, **config_overrides: Any
) -> PipelineContext:
    """构造 do_tests=True 的 PipelineContext（issues/patches/index 由测试按需填充）。"""
    config = AuditConfig(source_path=str(workspace.src_root), do_tests=True, **config_overrides)
    return PipelineContext(config=config, workspace=workspace, llm=llm, emitter=emitter)


def generated_file(workspace: WorkspaceContext, file: str) -> Path:
    """生成文件的绝对路径（复用 runner.generated_rel_path 保证命名一致）。"""
    from audit.testgen.runner import generated_rel_path

    return workspace.abs_path(generated_rel_path(file))
