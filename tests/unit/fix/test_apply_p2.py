"""apply P2 加固单测（第四轮审计 P2 清偿）：符号链接目标拒绝。

背景：applyer 此前对 symlink 目标语义未定义——规划期 read_bytes 穿透链接读
目标内容、写入期 os.replace 把链接本体替换为普通文件（用户静默丢链接）。本轮
补上规划期拒绝（修改型 / 删除型）与写入期核对（纵深防御，规划与写入间隙链接
被替换时整体回滚）。

Windows 无符号链接权限（未开开发者模式）时自动 skip。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

import _fix_helpers as H
from audit.fix.applyer import apply_patches
from audit.models import Patch

def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _patch(diff: str, sha_map: dict[str, str], *, pid: str = "PATCH-P2-1") -> Patch:
    return Patch(
        id=pid,
        issue_id="ISS-P2-1",
        diff=diff,
        rationale="symlink 加固测试补丁",
        apply_status="verified",
        target_sha256=sha_map,
    )


def _symlink_supported(tmp_path: Path) -> bool:
    try:
        probe_src = tmp_path / "_probe_src.txt"
        probe_src.write_text("x", encoding="utf-8")
        (tmp_path / "_probe_link.txt").symlink_to(probe_src)
        return True
    except (OSError, NotImplementedError):
        return False


def _seed_with_symlink_target(tmp_path: Path) -> Path:
    """workdir/app.py 为指向外部真实文件的符号链接（内容与 fix 样例一致）。"""
    real = tmp_path / "real_outside"
    real.mkdir()
    (real / "app.py").write_text(H.APP_PY, encoding="utf-8", newline="")
    workdir = tmp_path / "proj"
    workdir.mkdir()
    os.symlink(real / "app.py", workdir / "app.py")
    return workdir


def test_modify_symlink_target_rejected(tmp_path: Path):
    """修改型补丁：目标为符号链接 → 规划期拒绝，dry-run 与 yes 均不落盘。"""
    if not _symlink_supported(tmp_path):
        pytest.skip("当前环境不支持创建符号链接（Windows 未开开发者模式）")
    workdir = _seed_with_symlink_target(tmp_path)
    sha = _sha((workdir / "app.py").read_bytes())  # read_bytes 穿透链接，即目标内容指纹
    patch = _patch(H.BARE_EXCEPT_DIFF, {"app.py": sha})

    dry = apply_patches([patch], workdir, yes=False)
    assert not dry.ok and any("符号链接" in e.get("reason", "") for e in dry.rejected)

    yes = apply_patches([patch], workdir, yes=True)
    assert not yes.ok and not yes.written_files
    assert (workdir / "app.py").is_symlink(), "拒绝后链接本体必须原样保留"


def test_delete_symlink_target_rejected(tmp_path: Path):
    """删除型补丁（+++ /dev/null）：目标为符号链接 → 拒绝且不删链接。"""
    if not _symlink_supported(tmp_path):
        pytest.skip("当前环境不支持创建符号链接（Windows 未开开发者模式）")
    workdir = _seed_with_symlink_target(tmp_path)
    lines = H.APP_PY.split("\n")
    hunk_body = "".join(f"-{ln}\n" for ln in lines if ln != "")
    diff = f"--- a/app.py\n+++ /dev/null\n@@ -1,{len(lines) - 1} +0,0 @@\n{hunk_body}"
    sha = _sha((workdir / "app.py").read_bytes())
    patch = _patch(diff, {"app.py": sha})

    yes = apply_patches([patch], workdir, yes=True)
    assert not yes.ok and any("符号链接" in e.get("reason", "") for e in yes.rejected)
    assert (workdir / "app.py").is_symlink(), "拒绝后链接本体必须原样保留"
