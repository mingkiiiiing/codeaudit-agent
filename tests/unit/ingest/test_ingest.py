"""Stage1 接入单元测试：目录/zip 双入口一致性、编码容错、gitignore 过滤、规模保护。"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from audit.errors import IngestError
from audit.ingest import ingest
from audit.ingest.decode import decode_source_bytes
from audit.utils import count_lines, sha256_file


def _write_project(root: Path, files: dict[str, str | bytes]) -> Path:
    """按 {相对路径: 内容} 在 root 下生成项目文件。"""
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
    return root


def _zip_dir(src: Path, zip_path: Path, top_dir: str | None = None) -> Path:
    """现场把目录打包为 zip；top_dir 非空时加一层顶层目录。"""
    with zipfile.ZipFile(zip_path, "w") as zf:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                arc = p.relative_to(src).as_posix()
                if top_dir:
                    arc = f"{top_dir}/{arc}"
                zf.write(p, arc)
    return zip_path


def _manifest_tuples(ctx) -> list[tuple]:
    return sorted(
        (m.path, m.language, m.loc, m.sha256, m.parsed_ok, m.skipped_reason) for m in ctx.manifests
    )


# ---------------------------------------------------------------- 基本目录入口


def test_ingest_dir_basic(demo_proj_path: Path, tmp_path: Path):
    ctx = ingest(demo_proj_path, tmp_path, "a1")
    assert ctx.audit_id == "a1"
    assert ctx.src_root == tmp_path / "a1" / "src"
    assert ctx.work_root == tmp_path / "a1"
    assert ctx.db_path == tmp_path / "a1" / "index.db"
    assert (ctx.src_root / "app" / "services" / "users.py").is_file()
    assert ctx.language == "python"
    paths = {m.path for m in ctx.manifests}
    assert "app/services/users.py" in paths
    assert "main.py" in paths
    for m in ctx.manifests:
        assert m.sha256 == sha256_file(ctx.abs_path(m.path))
        if m.language == "python":
            assert m.loc == count_lines(ctx.read_file_text(m.path))
    # manifests 可 JSON 序列化（模型契约）
    assert all(isinstance(m.to_dict(), dict) for m in ctx.manifests)


def test_ingest_default_audit_id(demo_proj_path: Path, tmp_path: Path):
    ctx = ingest(demo_proj_path, tmp_path)
    assert ctx.audit_id  # 缺省时自动生成
    assert len(ctx.audit_id) == 12


def test_ingest_rerun_same_audit_id_idempotent(demo_proj_path: Path, tmp_path: Path):
    ctx1 = ingest(demo_proj_path, tmp_path, "same")
    ctx2 = ingest(demo_proj_path, tmp_path, "same")
    assert _manifest_tuples(ctx1) == _manifest_tuples(ctx2)


# ---------------------------------------------------------------- zip 入口与一致性


def test_ingest_zip_parity_with_dir(demo_proj_path: Path, tmp_path: Path):
    dir_ctx = ingest(demo_proj_path, tmp_path, "from_dir")
    zip_path = _zip_dir(demo_proj_path, tmp_path / "demo.zip")
    zip_ctx = ingest(zip_path, tmp_path, "from_zip")
    assert _manifest_tuples(dir_ctx) == _manifest_tuples(zip_ctx)
    assert dir_ctx.language == zip_ctx.language


def test_ingest_zip_with_top_folder_hoisted(demo_proj_path: Path, tmp_path: Path):
    """带单层顶层目录的常见 zip 形态：自动提升为项目根。"""
    dir_ctx = ingest(demo_proj_path, tmp_path, "from_dir")
    zip_path = _zip_dir(demo_proj_path, tmp_path / "wrapped.zip", top_dir="demo_proj")
    zip_ctx = ingest(zip_path, tmp_path, "from_zip")
    assert _manifest_tuples(dir_ctx) == _manifest_tuples(zip_ctx)
    assert (zip_ctx.src_root / "main.py").is_file()


# ---------------------------------------------------------------- 编码容错


def test_decode_source_bytes_gbk():
    gbk_bytes = "中文注释".encode("gbk")
    assert decode_source_bytes(gbk_bytes) == "中文注释"
    assert decode_source_bytes("ascii".encode("utf-8")) == "ascii"
    assert decode_source_bytes(b"\xff\xfe\x00bad")  # 永不崩溃


def test_ingest_gbk_file_no_crash(tmp_path: Path):
    gbk_content = '"""GBK 编码模块"""\n\nGREETING = "你好世界"\n'
    _write_project(tmp_path / "proj", {"gbk_mod.py": gbk_content.encode("gbk")})
    ctx = ingest(tmp_path / "proj", tmp_path, "gbk1")
    assert ctx.language == "python"
    assert [m.path for m in ctx.manifests] == ["gbk_mod.py"]
    assert ctx.manifests[0].parsed_ok is True
    # 工作副本可按契约读取（utf-8 失败 → gbk）
    assert ctx.read_file_text("gbk_mod.py") == gbk_content


# ---------------------------------------------------------------- .gitignore 过滤


def test_ingest_gitignore_filter(tmp_path: Path):
    _write_project(
        tmp_path / "proj",
        {
            ".gitignore": "logs/\n*.tmp\nsecret.txt\n",
            "main.py": "print('hi')\n",
            "logs/app.log": "log\n",
            "deep/nested/debug.tmp": "tmp\n",
            "secret.txt": "s3cret\n",
            "keep/me.py": "x = 1\n",
        },
    )
    ctx = ingest(tmp_path / "proj", tmp_path, "gi1")
    paths = {m.path for m in ctx.manifests}
    assert "main.py" in paths
    assert "keep/me.py" in paths
    assert "logs/app.log" not in paths
    assert "deep/nested/debug.tmp" not in paths
    assert "secret.txt" not in paths
    # 工作副本已物理剪枝
    assert not (ctx.src_root / "logs").exists()
    assert not (ctx.src_root / "secret.txt").exists()


def test_ingest_builtin_ignore_dirs(tmp_path: Path):
    _write_project(
        tmp_path / "proj",
        {
            "main.py": "x = 1\n",
            "node_modules/pkg/index.js": "export default 0;\n",
            "__pycache__/m.pyc": b"\x00\x01",
            "app.min.js": "var a=1;",
        },
    )
    ctx = ingest(tmp_path / "proj", tmp_path, "bi1")
    paths = {m.path for m in ctx.manifests}
    assert paths == {"main.py"}


# ---------------------------------------------------------------- 规模保护与异常入口


def test_ingest_max_files_guard(demo_proj_path: Path, tmp_path: Path):
    with pytest.raises(IngestError, match="文件数"):
        ingest(demo_proj_path, tmp_path, "guard1", max_files=3)


def test_ingest_max_loc_guard(demo_proj_path: Path, tmp_path: Path):
    with pytest.raises(IngestError, match="行数"):
        ingest(demo_proj_path, tmp_path, "guard2", max_loc=10)


def test_ingest_too_long_file_marked_skipped(tmp_path: Path):
    long_file = "\n".join(f"x{i} = {i}" for i in range(120)) + "\n"
    _write_project(tmp_path / "proj", {"big.py": long_file, "ok.py": "y = 1\n"})
    ctx = ingest(tmp_path / "proj", tmp_path, "tl1", max_file_lines=100)
    by_path = {m.path: m for m in ctx.manifests}
    assert by_path["big.py"].parsed_ok is False
    assert "too_long" in by_path["big.py"].skipped_reason
    assert by_path["ok.py"].parsed_ok is True


def test_ingest_missing_source(tmp_path: Path):
    with pytest.raises(IngestError, match="不存在"):
        ingest(tmp_path / "no_such_dir", tmp_path, "m1")


def test_ingest_bad_zip(tmp_path: Path):
    bad = tmp_path / "bad.zip"
    bad.write_text("this is not a zip", encoding="utf-8")
    with pytest.raises(IngestError, match="zip"):
        ingest(bad, tmp_path, "m2")


def test_ingest_empty_project(tmp_path: Path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(IngestError, match="没有可用源文件"):
        ingest(tmp_path / "empty", tmp_path, "m3")


# ---------------------------------------------------------------- R1-11 / R1-14 / R1-16 回归


def test_ingest_dir_skips_default_ignore_dirs_at_copy(tmp_path: Path):
    """R1-11：目录入口在 copytree 阶段即跳过内置忽略目录（.git 不进工作副本）。"""
    proj = tmp_path / "proj"
    _write_project(
        proj,
        {
            "main.py": "x = 1\n",
            "node_modules/pkg/index.js": "export default 0;\n",
        },
    )
    (proj / ".git").mkdir()
    (proj / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    ctx = ingest(proj, tmp_path, "r1_11")
    paths = {m.path for m in ctx.manifests}
    assert paths == {"main.py"}
    assert not (ctx.src_root / ".git").exists()
    assert not (ctx.src_root / "node_modules").exists()


def test_ingest_zip_skips_ignore_dir_members(tmp_path: Path):
    """R1-11：zip 入口与目录入口同口径——忽略目录成员（含 .git 骨架）不进副本。"""
    proj = tmp_path / "proj"
    _write_project(proj, {"main.py": "x = 1\n"})
    zip_path = tmp_path / "with_git.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("proj/main.py", "x = 1\n")
        zf.writestr("proj/.git/HEAD", "ref: refs/heads/main\n")
        zf.writestr("proj/.git/config", "[core]\n")
        zf.writestr("proj/node_modules/pkg/index.js", "export default 0;\n")
    ctx = ingest(zip_path, tmp_path, "r1_11z")
    paths = {m.path for m in ctx.manifests}
    assert paths == {"main.py"}
    assert not (ctx.src_root / ".git").exists()


def test_ingest_zip_bomb_guard(tmp_path: Path):
    """R1-14：zip 解压累计大小前置校验——按中央目录声明大小超限即拒（不解压）。"""
    zip_path = tmp_path / "payload.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("proj/main.py", "x = 1\n")
        zf.writestr("proj/data.bin", b"\x00" * 5000)
    with pytest.raises(IngestError, match="上限"):
        ingest(zip_path, tmp_path, "bomb", max_zip_bytes=1000)
    assert not (tmp_path / "bomb").exists()  # R1-16：失败清理 task_root


def test_ingest_failure_cleans_task_root(tmp_path: Path):
    """R1-16：接入失败（无可用源文件）时删除半成品 task_root。"""
    (tmp_path / "empty").mkdir()
    with pytest.raises(IngestError):
        ingest(tmp_path / "empty", tmp_path, "gone")
    assert not (tmp_path / "gone").exists()


def test_ingest_max_files_guard_cleans_task_root(demo_proj_path: Path, tmp_path: Path):
    """R1-16：规模超限失败同样清理 task_root（原来会残留整个副本）。"""
    with pytest.raises(IngestError, match="文件数"):
        ingest(demo_proj_path, tmp_path, "guardx", max_files=3)
    assert not (tmp_path / "guardx").exists()
