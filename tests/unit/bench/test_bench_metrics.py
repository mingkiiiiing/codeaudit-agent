"""bench.metrics 单元测试：P/R/F1 手算对账（含除零→None）、timing/cost/fix 空安全。"""

from __future__ import annotations

import pytest
from audit.models import Category, Issue, Patch, Severity

from bench.goldset import GoldenIssue
from bench.matcher import match_report
from bench.metrics import (
    cost_metrics,
    detection_metrics,
    fix_metrics,
    metrics_table,
    parse_level,
    timing_metrics,
)


def issue(id: str, file: str, ls: int, category: Category, severity: Severity) -> Issue:
    return Issue(id=id, file=file, line_start=ls, line_end=ls, category=category, severity=severity, title="t")


@pytest.fixture
def pr_fixture() -> tuple[list[GoldenIssue], list[Issue]]:
    """3 报告 4 金标：critical+high 层内 P=R=F1=2/3，可手算对账。"""
    goldens = [
        GoldenIssue(project="p", file="f1.py", line_start=10, line_end=12, category="bug", severity="high", description="G1"),
        GoldenIssue(project="p", file="f2.py", line_start=10, line_end=10, category="bug", severity="critical", description="G2"),
        GoldenIssue(project="p", file="f3.py", line_start=5, line_end=5, category="style", severity="low", description="G3"),
        GoldenIssue(project="p", file="f4.py", line_start=10, line_end=10, category="bug", severity="high", description="G4"),
    ]
    reports = [
        issue("R1", "f1.py", 11, Category.BUG, Severity.HIGH),  # 命中 G1
        issue("R2", "f2.py", 12, Category.BUG, Severity.CRITICAL),  # 12 ∈ [7,13] 命中 G2
        issue("R3", "f3.py", 5, Category.BUG, Severity.HIGH),  # bug vs style 跨类别 → 不命中
    ]
    return goldens, reports


def test_parse_level():
    assert parse_level("critical+high") == frozenset({"critical", "high"})
    assert parse_level("all") is None
    assert parse_level("") is None
    with pytest.raises(ValueError):
        parse_level("critical+oops")


def test_detection_metrics_hand_computed(pr_fixture):
    goldens, reports = pr_fixture
    match = match_report(reports, goldens)
    m = detection_metrics(match, len(goldens), reports, level="critical+high")
    # 层内报告 R1/R2/R3 共 3，命中者 R1/R2 共 2 → P = 2/3
    assert m["counts"]["reports_in_level"] == 3
    assert m["counts"]["reports_matched"] == 2
    assert m["precision"] == pytest.approx(2 / 3)
    # 层内金标 G1/G2/G4 共 3，命中 G1/G2 共 2 → R = 2/3
    assert m["counts"]["goldens_in_level"] == 3
    assert m["counts"]["goldens_matched"] == 2
    assert m["recall"] == pytest.approx(2 / 3)
    assert m["f1"] == pytest.approx(2 / 3)
    assert m["counts"]["goldens_total"] == 4
    assert m["counts"]["reports_total"] == 3


def test_detection_metrics_division_by_zero_none(pr_fixture):
    goldens, reports = pr_fixture
    match = match_report(reports, goldens)
    # medium：层内无报告也无金标 → 三个指标全 None
    m = detection_metrics(match, len(goldens), reports, level="medium")
    assert m["precision"] is None
    assert m["recall"] is None
    assert m["f1"] is None
    # low：层内无报告（P=None），金标 G3 未被命中（R=0），F1=None
    m2 = detection_metrics(match, len(goldens), reports, level="low")
    assert m2["precision"] is None
    assert m2["recall"] == 0.0
    assert m2["f1"] is None
    assert m2["counts"]["goldens_in_level"] == 1


def test_detection_metrics_level_all(pr_fixture):
    goldens, reports = pr_fixture
    match = match_report(reports, goldens)
    m = detection_metrics(match, len(goldens), reports, level="all")
    assert m["counts"]["reports_in_level"] == 3
    assert m["counts"]["goldens_in_level"] == 4


def test_timing_metrics_basic_and_percentiles():
    t = timing_metrics([10.0, 20.0], [1000, 1000])
    assert t["n"] == 2
    assert t["items"][0]["sec_per_kloc"] == pytest.approx(10.0)
    assert t["sec_per_kloc_p50"] == pytest.approx(15.0)
    assert t["sec_per_kloc_p90"] == pytest.approx(19.0)  # 线性插值：10 + 0.9*10
    assert t["sec_per_kloc_mean"] == pytest.approx(15.0)
    # 单样本
    t1 = timing_metrics([5.0], [500])
    assert t1["sec_per_kloc_p50"] == pytest.approx(10.0)
    assert t1["sec_per_kloc_p90"] == pytest.approx(10.0)


def test_timing_metrics_empty_and_zero_loc_safe():
    t0 = timing_metrics([], [])
    assert t0["n"] == 0
    assert t0["sec_per_kloc_p50"] is None
    assert t0["sec_per_kloc_p90"] is None
    assert t0["sec_per_kloc_mean"] is None
    # loc=0 / locs 缺项 → 该项不计入分位
    tz = timing_metrics([3.0], [0])
    assert tz["sec_per_kloc_p50"] is None
    assert tz["items"][0]["sec_per_kloc"] is None
    tm = timing_metrics([3.0], [])  # locs 缺项
    assert tm["sec_per_kloc_p50"] is None


def test_cost_metrics():
    usage = {"prompt_tokens": 2000, "completion_tokens": 500, "cache_hits": 3, "cache_misses": 1}
    c = cost_metrics(usage, kloc=2.0)
    assert c["prompt_tokens_per_kloc"] == pytest.approx(1000.0)
    assert c["completion_tokens_per_kloc"] == pytest.approx(250.0)
    assert c["cache_hit_ratio"] == pytest.approx(0.75)
    # 零除安全：kloc=0 → per-KLOC 为 None；无调用 → 命中率 None
    c0 = cost_metrics({}, 0.0)
    assert c0["prompt_tokens_per_kloc"] is None
    assert c0["completion_tokens_per_kloc"] is None
    assert c0["cache_hit_ratio"] is None


def test_fix_metrics_empty_safe():
    f = fix_metrics([])
    assert f["total"] == 0
    assert f["syntax_ok_ratio"] is None
    assert f["verified_ratio"] is None


def test_fix_metrics_by_apply_status():
    patches = [
        Patch(id="P1", apply_status="verified"),
        Patch(id="P2", apply_status="syntax-ok"),
        Patch(id="P3", apply_status="pending"),
        Patch(id="P4", apply_status="failed"),
    ]
    f = fix_metrics(patches)
    assert f["total"] == 4
    assert f["syntax_ok"] == 2  # verified + syntax-ok
    assert f["syntax_ok_ratio"] == pytest.approx(0.5)
    assert f["entered_verify"] == 2  # verified + failed 进入过沙箱
    assert f["verified"] == 1
    assert f["verified_ratio"] == pytest.approx(0.5)
    # dict 输入兼容
    fd = fix_metrics([{"apply_status": "verified"}, {"apply_status": "failed"}])
    assert fd["verified_ratio"] == pytest.approx(0.5)


def test_metrics_table_flat_nested_and_none():
    table = metrics_table({"precision": 2 / 3, "recall": None, "counts": {"a": 1}})
    lines = table.splitlines()
    assert lines[0] == "| 指标 | 值 |"
    assert lines[1] == "|---|---|"
    assert any("precision" in ln and "0.6667" in ln for ln in lines)
    assert any("recall" in ln and "N/A" in ln for ln in lines)
    assert any("counts.a" in ln and "| 1 |" in ln for ln in lines)
