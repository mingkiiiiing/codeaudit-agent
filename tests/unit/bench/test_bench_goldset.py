"""bench.goldset 单元测试：markdown 金标解析、JSONL 读写、坏行容错。"""

from __future__ import annotations

import json
from pathlib import Path

from bench.goldset import GoldenIssue, from_markdown_table, load_goldset, save_goldset

ROOT = Path(__file__).resolve().parents[3]
GOLDEN_MD = ROOT / "tests" / "samples" / "demo_proj" / "GOLDEN_ISSUES.md"


def test_from_markdown_table_real_file_12_items():
    """真实 GOLDEN_ISSUES.md → 12 条金标，字段与枚举字符串正确。"""
    items = from_markdown_table(GOLDEN_MD)
    assert len(items) == 12
    g1 = items[0]
    assert g1.project == "demo_proj"
    assert g1.file == "app/services/orders.py"
    assert (g1.line_start, g1.line_end) == (10, 10)
    assert g1.category == "bug"
    assert g1.severity == "high"
    assert "get_user" in g1.description
    g2 = items[1]
    assert g2.category == "security"
    assert g2.severity == "critical"
    # 四个类别都出现，且都是合法枚举字符串
    assert {i.category for i in items} == {"bug", "security", "performance", "style"}
    assert all(i.origin == "manual" for i in items)


def test_from_markdown_table_range_and_custom_project(tmp_path: Path):
    md = tmp_path / "G.md"
    md.write_text(
        "| # | 文件 | 行 | 类别 | 严重度 | 描述 |\n"
        "|---|---|---|---|---|---|\n"
        "| G1 | a/b.py | 10-12 | bug | high | 区间金标 |\n"
        "| G2 | a/c.py | 7 | style | low | 单行金标 |\n",
        encoding="utf-8",
    )
    items = from_markdown_table(md, project="my_proj")
    assert len(items) == 2
    assert (items[0].line_start, items[0].line_end) == (10, 12)
    assert items[1].line_start == 7 and items[1].line_end == 7
    assert all(i.project == "my_proj" for i in items)


def test_from_markdown_table_bad_rows_skipped(tmp_path: Path):
    md = tmp_path / "G.md"
    md.write_text(
        "| # | 文件 | 行 | 类别 | 严重度 | 描述 |\n"
        "|---|---|---|---|---|---|\n"
        "| G1 | a/b.py | abc | bug | high | 行号坏 |\n"
        "| G2 | a/c.py | 7 | nope | low | 类别坏 |\n"
        "| G3 | a/d.py | 8 | bug | high | 好行 |\n",
        encoding="utf-8",
    )
    warnings_out: list[str] = []
    items = from_markdown_table(md, warnings_out=warnings_out)
    assert [i.description for i in items] == ["好行"]
    assert len(warnings_out) == 2


def test_jsonl_roundtrip(tmp_path: Path):
    items = from_markdown_table(GOLDEN_MD)
    path = tmp_path / "goldset.jsonl"
    save_goldset(items, path)
    loaded = load_goldset(path)
    assert loaded == items


def test_load_bad_lines_skipped_with_warnings(tmp_path: Path):
    good = {
        "project": "p",
        "file": "a.py",
        "line_start": 3,
        "line_end": 4,
        "category": "bug",
        "severity": "high",
        "description": "ok",
        "origin": "manual",
    }
    path = tmp_path / "g.jsonl"
    lines = [
        json.dumps(good, ensure_ascii=False),
        "{not json",  # 坏 JSON
        json.dumps({"file": "", "category": "nope", "severity": "huge"}),  # 非法字段
        "",  # 空行静默跳过
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    warnings_out: list[str] = []
    loaded = load_goldset(path, warnings_out=warnings_out)
    assert len(loaded) == 1
    assert loaded[0].description == "ok"
    assert len(warnings_out) == 2


def test_from_dict_tolerant_and_to_dict_roundtrip():
    g = GoldenIssue.from_dict({"file": "x.py", "line_start": 1, "line_end": 2})
    assert g.file == "x.py"
    assert (g.line_start, g.line_end) == (1, 2)
    assert g.category == "bug"  # 缺省值
    full = GoldenIssue(
        project="p",
        file="a.py",
        line_start=1,
        line_end=2,
        category="security",
        severity="low",
        description="d",
        origin="injected",
    )
    assert GoldenIssue.from_dict(full.to_dict()) == full
