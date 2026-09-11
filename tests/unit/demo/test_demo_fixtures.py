"""demo 夹具一致性测试（W3-A3）：保证 demo/run_demo.py 的脚本化答案与 demo/mini_app 对齐。

不跑完整流水线（那是 `python demo/run_demo.py` / `make demo` 的职责），只做三件便宜的事：
1. 脚本化 Fix diff 能 `git apply --check` 到 mini_app/store.py（字节级对齐）；
2. 脚本化 TestGen 代码通过 validate_code 硬闸门且 assert 数达标；
3. 纯规则扫描 mini_app 恰好命中 1 条 critical/high（PY-SQL-INJECTION @ store.py:6），
   否则演示的"单一靶点"叙事会失真。
全部离线：无 LLM、无沙箱子进程（仅本机 git CLI）。
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

from audit.config import AuditConfig
from audit.detect.engine import run_rules
from audit.llm.base import FakeLLMClient
from audit.pipeline import PipelineContext
from audit.testgen.generator import count_asserts, validate_code
from audit.workspace import WorkspaceContext

ROOT = Path(__file__).resolve().parents[3]
DEMO_DIR = ROOT / "demo"
MINI_APP = DEMO_DIR / "mini_app"
RUN_DEMO = DEMO_DIR / "run_demo.py"


def _load_run_demo():
    """以文件路径加载 demo/run_demo.py（模块级无副作用，仅常量与函数定义）。"""
    spec = importlib.util.spec_from_file_location("demo_run_demo", RUN_DEMO)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def run_demo():
    return _load_run_demo()


def test_mini_app_layout_is_complete() -> None:
    """靶项目的关键文件齐全：缺陷靶点、现有测试、README 说明。"""
    for rel in ("store.py", "textutil.py", "main.py", "tests/test_store.py", "README.md"):
        assert (MINI_APP / rel).is_file(), f"demo/mini_app 缺少 {rel}"
    assert b"\r\n" not in (MINI_APP / "store.py").read_bytes(), "store.py 必须为 LF 换行（git apply 逐字节对齐）"


def test_fix_diff_applies_to_mini_app_store(tmp_path: Path, run_demo) -> None:
    """脚本化 Fix diff 对 mini_app/store.py 副本可干跑通过 git apply --check。"""
    if shutil.which("git") is None:
        pytest.skip("git 不可用")
    target = tmp_path / "store.py"
    target.write_bytes((MINI_APP / "store.py").read_bytes())
    proc = subprocess.run(
        ["git", "apply", "--check", "--whitespace=nowarn"],
        cwd=str(tmp_path),
        input=run_demo.FIX_DIFF.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")


def test_fix_diff_removes_concatenation_and_adds_placeholder(run_demo) -> None:
    """diff 语义：删除拼接行、新增 ? 占位符 + 参数元组，且 hunk 行数不变（保持切片区间对齐）。"""
    removed = [ln for ln in run_demo.FIX_DIFF.splitlines() if ln.startswith("-") and not ln.startswith("---")]
    added = [ln for ln in run_demo.FIX_DIFF.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    assert any("+ username +" in ln for ln in removed)
    assert any("name = ?" in ln for ln in added)
    assert any("(username,)" in ln for ln in added)
    assert len(removed) == len(added)
    assert "@@ -4,4 +4,4 @@" in run_demo.FIX_DIFF


def test_testgen_code_passes_hard_gate(run_demo) -> None:
    """脚本化 TestGen 代码通过 validate_code，且 ≥5 条 assert、带 kind 标注、含注入载荷用例。"""
    code = run_demo.TESTGEN_CODE
    assert validate_code(code) == []
    assert count_asserts(code) >= 5
    for kind in ("normal", "boundary", "error"):
        assert f"# kind: {kind}" in code
    assert "OR '1'='1" in code, "应包含注入载荷回归用例，证明修复后按字面值匹配"
    assert "from store import find_user" in code


def test_rules_only_scan_finds_single_critical_target(tmp_path: Path) -> None:
    """纯规则扫描 mini_app：恰好 1 条 critical/high，且是 store.py:6 的 PY-SQL-INJECTION。"""
    src = tmp_path / "mini_app"
    shutil.copytree(MINI_APP, src, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    workspace = WorkspaceContext(audit_id="demo-fixtures", src_root=src, work_root=tmp_path, db_path=tmp_path / "index.db")
    config = AuditConfig(source_path=str(src), work_root=str(tmp_path), enable_llm_review=False)

    async def _noop(_event: dict) -> None:
        return None

    ctx = PipelineContext(config=config, workspace=workspace, llm=FakeLLMClient(), emitter=_noop)
    hits = run_rules(ctx)
    severe = [h for h in hits if h.severity.value in ("critical", "high")]
    assert len(severe) == 1, [(h.rule_id, h.file, h.line_start) for h in severe]
    hit = severe[0]
    assert hit.rule_id == "PY-SQL-INJECTION"
    assert hit.file.replace("\\", "/") == "store.py"
    assert hit.line_start == 6
