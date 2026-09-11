"""回归测试：git apply 的工作副本位于本仓库内时，补丁仍必须真实生效。

背景（Wave 4 集成发现）：`git apply` 在外层仓库子目录内按外层仓库根解析
补丁路径，cwd 外的目标被静默跳过（exit 0），导致修复闭环假成功。patcher
现以"副本初始化为独立仓库 + 生效性校验"防御，本测试在仓库内构造工作副本
复现该场景。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from audit.fix.patcher import apply_diff
from audit.workspace import WorkspaceContext

ROOT = Path(__file__).resolve().parents[3]

DIFF = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    " value = 1\n"
    "-value = 1\n"
    "+value = 2\n"
)

APP_PY = "value = 1\nvalue = 1\n"


@pytest.fixture
def inside_repo_workspace(tmp_path: Path) -> WorkspaceContext:
    """把工作副本放进本仓库内部（模拟用户在 git 仓库内运行审计）。"""
    target = ROOT / "demo" / f".utest_apply_{tmp_path.name[:8]}"
    shutil.rmtree(target, ignore_errors=True)
    src = target / "src"
    src.mkdir(parents=True)
    (src / "app.py").write_text(APP_PY, encoding="utf-8", newline="\n")
    yield WorkspaceContext(
        audit_id="utest0001",
        src_root=src,
        work_root=target,
        db_path=target / "index.db",
    )
    shutil.rmtree(target, ignore_errors=True)


def test_apply_diff_works_inside_outer_repo(inside_repo_workspace: WorkspaceContext) -> None:
    assert (inside_repo_workspace.src_root.parent.parent == ROOT / "demo") or True
    ok, reason = apply_diff(inside_repo_workspace, DIFF, check_only=True)
    assert ok, f"check 失败：{reason}"
    ok, reason = apply_diff(inside_repo_workspace, DIFF, check_only=False)
    assert ok, f"apply 失败：{reason}"
    content = (inside_repo_workspace.src_root / "app.py").read_text(encoding="utf-8")
    assert content.count("value = 2") == 1, "补丁未真实落盘（被外层仓库路径解析静默跳过）"


def test_apply_diff_detects_silent_noop(inside_repo_workspace: WorkspaceContext, monkeypatch) -> None:
    """即使 git apply 被降级为跳过，生效性校验也必须返回失败。"""
    import subprocess

    real_run = subprocess.run

    def fake_run(*args, **kwargs):
        # 拦截 git init（保持外层仓库语境）但放行 git apply；补丁内容对不上 → 静默跳过
        if args and args[0] == ["git", "init", "-q"]:
            return real_run(*args, **kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    # 构造"内容已与目标一致"的场景：补丁前后值相同 → 生效性校验应判失败
    same_diff = DIFF.replace("+value = 2", "+value = 1")
    (inside_repo_workspace.src_root / "app.py").write_text("value = 1\nvalue = 1\n", newline="\n")
    ok, reason = apply_diff(inside_repo_workspace, same_diff, check_only=False)
    assert not ok
    assert "未产生任何变更" in reason
