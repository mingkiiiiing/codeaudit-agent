"""W7-A1 ingest 硬链接物化测试：同盘生效（st_nlink/st_ino）、跨盘与链接失败逐文件
回退拷贝、结构性失败整体回退 copytree、manifests 与拷贝路径完全一致、源文件安全。

Windows NTFS 原生支持硬链接（os.link / st_nlink），本文件即在本机真实语义上断言。
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

import audit.ingest.core as core
from audit.ingest import ingest


def _write_project(root: Path) -> Path:
    """标准样例项目：嵌套目录 + .git 忽略目录。"""
    (root / "pkg" / "sub").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "pkg" / "sub" / "b.py").write_text("y = 2\n", encoding="utf-8")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    return root


def _manifest_tuples(ctx) -> list[tuple]:
    return sorted(
        (m.path, m.language, m.loc, m.sha256, m.parsed_ok, m.skipped_reason) for m in ctx.manifests
    )


def ctx_read(ctx, rel: str) -> str:
    return ctx.abs_path(rel).read_text(encoding="utf-8")


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    return _write_project(tmp_path / "proj")


# ---------------------------------------------------------------- 同盘硬链接生效


def test_hardlink_active_on_same_volume(tmp_path: Path, proj: Path):
    ctx = ingest(proj, tmp_path / "work", "hl1", link_same_volume=True)
    ws_file = ctx.abs_path("pkg/a.py")
    src_file = proj / "pkg" / "a.py"
    assert os.stat(ws_file).st_nlink >= 2  # 硬链接生效（工作副本 + 源共享 inode）
    assert os.stat(ws_file).st_ino == os.stat(src_file).st_ino
    # 嵌套目录与根目录文件同样链接
    assert os.stat(ctx.abs_path("pkg/sub/b.py")).st_nlink >= 2
    assert os.stat(ctx.abs_path("README.md")).st_nlink >= 2


def test_hardlink_manifests_equal_copy_path(tmp_path: Path, proj: Path, monkeypatch):
    """硬链接路径与 copytree 路径的 manifests 必须完全一致（correctness 优先）。"""
    hl_ctx = ingest(proj, tmp_path / "work", "hl", link_same_volume=True)
    monkeypatch.setattr(core, "_try_hardlink_tree", lambda *_a, **_k: False)
    copy_ctx = ingest(proj, tmp_path / "work", "copy")
    assert _manifest_tuples(hl_ctx) == _manifest_tuples(copy_ctx)
    assert hl_ctx.language == copy_ctx.language
    for m in hl_ctx.manifests:
        assert ctx_read(hl_ctx, m.path) == ctx_read(copy_ctx, m.path)


def test_rerun_same_audit_id_with_hardlink_idempotent(tmp_path: Path, proj: Path):
    ctx1 = ingest(proj, tmp_path / "work", "same", link_same_volume=True)
    ctx2 = ingest(proj, tmp_path / "work", "same", link_same_volume=True)
    assert _manifest_tuples(ctx1) == _manifest_tuples(ctx2)
    assert os.stat(ctx2.abs_path("pkg/a.py")).st_nlink >= 2


# ---------------------------------------------------------------- 忽略语义与源安全


def test_ignored_dirs_not_linked(tmp_path: Path, proj: Path):
    ctx = ingest(proj, tmp_path / "work", "hl", link_same_volume=True)
    assert not (ctx.src_root / ".git").exists()  # R1-11：忽略目录不进副本
    assert (proj / ".git" / "config").is_file()  # 源工程原样保留


def test_workspace_prune_does_not_touch_source(tmp_path: Path):
    """gitignore 剪枝只摘工作副本侧的硬链接（unlink），源文件不受影响。"""
    proj = _write_project(tmp_path / "proj")
    (proj / ".gitignore").write_text("README.md\n", encoding="utf-8")
    ctx = ingest(proj, tmp_path / "work", "hl", link_same_volume=True)
    assert not (ctx.src_root / "README.md").exists()  # 副本侧被剪枝
    assert (proj / "README.md").is_file()  # 源侧仍在


def test_source_survives_workspace_file_delete(tmp_path: Path, proj: Path):
    ctx = ingest(proj, tmp_path / "work", "hl", link_same_volume=True)
    ctx.abs_path("pkg/a.py").unlink()
    assert (proj / "pkg" / "a.py").is_file()


# ---------------------------------------------------------------- 回退路径


def test_os_link_failure_falls_back_to_copy_per_file(tmp_path: Path, proj: Path, monkeypatch):
    """EPERM/EXDEV 等链接失败：逐文件回退 copy，物化结果与拷贝路径一致。"""

    def broken_link(src, dst, *args, **kwargs):
        raise OSError(errno.EPERM, "simulated: hardlink not permitted")

    monkeypatch.setattr(core.os, "link", broken_link)
    ctx = ingest(proj, tmp_path / "work", "fallback", link_same_volume=True)
    ws_file = ctx.abs_path("pkg/a.py")
    assert os.stat(ws_file).st_nlink == 1  # 真拷贝（非共享 inode）
    monkeypatch.setattr(core, "_try_hardlink_tree", lambda *_a, **_k: False)
    baseline = ingest(proj, tmp_path / "work", "baseline")
    assert _manifest_tuples(ctx) == _manifest_tuples(baseline)
    for m in ctx.manifests:
        assert ctx_read(ctx, m.path) == ctx_read(baseline, m.path)


def test_structural_failure_falls_back_to_copytree(tmp_path: Path, proj: Path, monkeypatch):
    """结构性失败（同卷判定为否）：整体回退 copytree，接入不失败且 manifests 一致。"""
    monkeypatch.setattr(core, "_same_volume", lambda *_a, **_k: False)
    ctx = ingest(proj, tmp_path / "work", "copytree", link_same_volume=True)
    assert os.stat(ctx.abs_path("pkg/a.py")).st_nlink == 1
    baseline = ingest(proj, tmp_path / "work", "baseline", link_same_volume=False)
    assert _manifest_tuples(ctx) == _manifest_tuples(baseline)


def test_link_same_volume_false_disables_hardlink(tmp_path: Path, proj: Path):
    ctx = ingest(proj, tmp_path / "work", "copy", link_same_volume=False)
    assert os.stat(ctx.abs_path("pkg/a.py")).st_nlink == 1
    hl_ctx = ingest(proj, tmp_path / "work", "hl", link_same_volume=True)
    assert _manifest_tuples(ctx) == _manifest_tuples(hl_ctx)
