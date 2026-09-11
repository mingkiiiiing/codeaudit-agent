"""W3-A1 数据集门槛测试：bench/datasets/goldset.jsonl 的规模与质量（docs/04 §2）。

门槛（docs/04 §2.1 / docs/08 §4 G-A）：
- 金标总数 ≥ 150 条；
- critical+high ≥ 60 条（Precision 主指标口径）；
- bug/security/performance/style 四类齐全、critical/high/medium/low 四级齐全；
- 全部字段合法（行号 1 ≤ line_start ≤ line_end、category/severity/origin 合法、
  file 为 posix 相对路径且在对应项目中真实存在）；
- description 全局唯一、同项目同文件同类别行区间不相交（无重复金标）。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from bench.goldset import VALID_CATEGORIES, VALID_ORIGINS, VALID_SEVERITIES, GoldenIssue, load_goldset

ROOT = Path(__file__).resolve().parents[3]
GOLDSET_PATH = ROOT / "bench" / "datasets" / "goldset.jsonl"
PROJECTS_DIR = ROOT / "bench" / "datasets" / "projects"
DEMO_PROJ = ROOT / "tests" / "samples" / "demo_proj"

EXPECTED_CATEGORIES = {"bug", "security", "performance", "style"}
EXPECTED_SEVERITIES = {"critical", "high", "medium", "low"}


def _load() -> list[GoldenIssue]:
    return load_goldset(GOLDSET_PATH)


def _project_dir(name: str) -> Path:
    """项目名 -> 项目根目录：合成/变体项目在 datasets/projects 下，demo_proj 在样例目录。"""
    candidate = PROJECTS_DIR / name
    return candidate if candidate.exists() else DEMO_PROJ


def test_goldset_exists_and_meets_scale() -> None:
    """金标文件存在、可加载且总量 ≥150（docs/04 §2.1 规模目标）。"""
    assert GOLDSET_PATH.is_file(), f"金标文件不存在：{GOLDSET_PATH}（请先运行 bench/datasets/gen_offline.py）"
    items = _load()
    assert len(items) >= 150, f"金标 {len(items)} 条 < 150"


def test_critical_high_at_least_60() -> None:
    """critical+high ≥60 条（Precision 主指标只在 critial+high 上声明）。"""
    items = _load()
    ch = sum(1 for item in items if item.severity in {"critical", "high"})
    assert ch >= 60, f"critical+high {ch} 条 < 60"


def test_all_categories_and_severities_covered() -> None:
    """category 覆盖 bug/security/performance/style，severity 覆盖四级。"""
    items = _load()
    assert {item.category for item in items} == EXPECTED_CATEGORIES
    assert {item.severity for item in items} == EXPECTED_SEVERITIES


def test_all_fields_valid() -> None:
    """全部条目字段合法：枚举值合法、行号 1≤start≤end、file/project/description 非空。"""
    for item in _load():
        where = f"{item.project}/{item.file}:{item.line_start}"
        assert item.project, f"{where} project 为空"
        assert item.file, f"{where} file 为空"
        assert "\\" not in item.file and not item.file.startswith("/"), f"{where} file 应为 posix 相对路径：{item.file}"
        assert item.category in VALID_CATEGORIES, f"{where} category 非法：{item.category}"
        assert item.severity in VALID_SEVERITIES, f"{where} severity 非法：{item.severity}"
        assert item.origin in VALID_ORIGINS, f"{where} origin 非法：{item.origin}"
        assert item.description, f"{where} description 为空"
        assert 1 <= item.line_start <= item.line_end, f"{where} 行号非法：{item.line_start}-{item.line_end}"


def test_golden_files_exist_in_projects() -> None:
    """每条金标引用的文件都能在对应项目目录中找到（除 git-history 指向的父版本坐标）。"""
    missing: list[str] = []
    for item in _load():
        target = _project_dir(item.project) / item.file
        if not target.is_file():
            missing.append(f"{item.project}/{item.file}")
    assert not missing, f"金标引用的文件缺失：{missing[:10]}"


def test_no_duplicate_goldens() -> None:
    """description 全局唯一；同项目同文件同类别的行区间互不相交（无重复入库）。"""
    items = _load()
    descriptions = [item.description for item in items]
    duplicated = [d for d, n in Counter(descriptions).items() if n > 1]
    assert not duplicated, f"description 重复（detection_metrics 按 description 去重会低估 recall）：{duplicated[:5]}"

    for (project, file, category), group in Counter((i.project, i.file, i.category) for i in items).items():
        if group < 2:
            continue
        spans = sorted(
            (i.line_start, i.line_end) for i in items if (i.project, i.file, i.category) == (project, file, category)
        )
        for (_, prev_end), (next_start, _) in zip(spans, spans[1:], strict=False):
            assert next_start > prev_end, (
                f"{project}/{file} [{category}] 存在相交金标区间：(...{prev_end}) 与 ({next_start}...)"
            )


def test_composition_balanced_across_projects() -> None:
    """项目分布合理性：每个数据集项目都有金标，且注入变体的金标不少于其源项目。"""
    items = _load()
    by_project = Counter(item.project for item in items)
    for name in ("shopcore", "blogengine", "datatools", "webapi", "demo_proj"):
        assert by_project[name] > 0, f"项目 {name} 无金标"
        variant = f"{name}_inj"
        assert by_project[variant] > 0, f"注入变体 {variant} 无金标"
        assert by_project[variant] >= by_project[name], (
            f"变体 {variant} 金标 {by_project[variant]} 少于源项目 {name} 的 {by_project[name]}（继承克隆缺失？）"
        )
