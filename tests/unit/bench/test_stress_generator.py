"""bench.stress.generator 单测：产物结构 / seed 可复现 / 幂等 / 缺陷分配。"""

from __future__ import annotations

import ast
import hashlib
import json

import pytest

from bench.stress.generator import DEFECT_KINDS, alloc_counts, generate_project, load_manifest


def _py_files(root) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(root.rglob("*.py"))
        if p.is_file()
    }


def test_alloc_counts_sums_to_files():
    for files in (9, 12, 30, 60, 500, 2000):
        counts = alloc_counts(files)
        assert sum(counts.values()) == files
        assert counts["inits"] == 4 and counts["main"] == 1
        assert all(counts[role] >= 1 for role in ("services", "utils", "models", "tests"))


def test_alloc_counts_rejects_tiny_files():
    with pytest.raises(ValueError):
        alloc_counts(8)  # 4 个 __init__ + main + 4 类角色 = 9 为下限
    with pytest.raises(ValueError):
        alloc_counts(5)


def test_generated_project_structure(tmp_path):
    root = generate_project(tmp_path / "proj", files=30)
    sources = _py_files(root)
    assert len(sources) == 30  # .py 文件精确计数
    for required in (
        "main.py",
        "app/__init__.py",
        "app/services/__init__.py",
        "app/utils/__init__.py",
        "app/models/__init__.py",
    ):
        assert required in sources, f"缺少 {required}"
    assert any(p.startswith("app/services/svc_") for p in sources)
    assert any(p.startswith("app/utils/util_") for p in sources)
    assert any(p.startswith("app/models/model_") for p in sources)
    assert any(p.startswith("tests/test_svc_") for p in sources)
    # ast 全通过 + 行数在期望带宽（平均 60~150 行/文件）
    total_loc = 0
    for rel, src in sources.items():
        ast.parse(src)  # 语法失败直接抛 SyntaxError
        total_loc += src.count("\n")
    assert 60 * 30 <= total_loc <= 150 * 30
    # 跨文件 import 真实存在（量级 ≥ files）
    imports = sum(
        1
        for src in sources.values()
        for line in src.splitlines()
        if line.startswith("from app.")
    )
    assert imports >= 30
    # manifest 在项目目录外且与磁盘一致
    manifest = load_manifest(root)
    assert manifest["files"] == sorted(sources)
    assert manifest["total_loc"] == total_loc


def test_seed_reproducibility_byte_identical(tmp_path):
    a = generate_project(tmp_path / "a", files=24, seed=42)
    b = generate_project(tmp_path / "b", files=24, seed=42)
    da = {rel: hashlib.sha256(src.encode()).hexdigest() for rel, src in _py_files(a).items()}
    db = {rel: hashlib.sha256(src.encode()).hexdigest() for rel, src in _py_files(b).items()}
    assert da == db  # 同 seed 字节级一致
    c = generate_project(tmp_path / "c", files=24, seed=7)
    dc = {rel: hashlib.sha256(src.encode()).hexdigest() for rel, src in _py_files(c).items()}
    assert da != dc  # 不同 seed 内容不同


def test_generate_project_idempotent(tmp_path):
    root = generate_project(tmp_path / "proj", files=16)
    first = (root / "main.py").stat().st_mtime_ns
    again = generate_project(tmp_path / "proj", files=16)
    second = (root / "main.py").stat().st_mtime_ns
    assert again == root
    assert first == second  # 幂等：meta 一致则跳过重建
    regenerate = generate_project(tmp_path / "proj", files=16, seed=99, force=True)
    assert (regenerate / "main.py").stat().st_mtime_ns != first
    assert load_manifest(root)["meta"]["seed"] == 99


def test_defect_assignment_matches_rules_kinds(tmp_path):
    root = generate_project(tmp_path / "proj", files=200, defect_ratio=0.02)
    manifest = load_manifest(root)
    defects = manifest["defects"]
    assert len(defects) == 4  # round(200 * 0.02)
    assert set(defects.values()) <= set(DEFECT_KINDS)
    for rel in defects:
        assert (root / rel).is_file()
        ast.parse((root / rel).read_text(encoding="utf-8"))
    # manifest JSON 可解析且 meta 记录缺陷数
    raw = json.loads((root.parent / "proj.manifest.json").read_text(encoding="utf-8"))
    assert raw["meta"]["defects"] == 4
    assert raw["meta"]["files"] == 200
