"""bench.matcher 单元测试：docs/04 §2.3 的行容差 / 文件 / 类别层级规则。"""

from __future__ import annotations

from audit.models import Category, Issue, Severity

from bench.goldset import GoldenIssue
from bench.matcher import matches, match_report


def make_issue(
    file: str = "app/a.py",
    ls: int = 10,
    le: int = 10,
    category: Category = Category.BUG,
    severity: Severity = Severity.HIGH,
    id: str = "R1",
) -> Issue:
    return Issue(id=id, category=category, severity=severity, file=file, line_start=ls, line_end=le, title="t")


def make_golden(
    file: str = "app/a.py",
    ls: int = 20,
    le: int = 22,
    category: str = "bug",
    severity: str = "high",
    description: str = "G",
) -> GoldenIssue:
    return GoldenIssue(
        project="p", file=file, line_start=ls, line_end=le,
        category=category, severity=severity, description=description,
    )


# ---------------------------------------------------------------- 行容差


def test_line_tolerance_within_plus3():
    # 报告单行 25 → [22, 28]，与金标 [20, 22] 相交 → 命中
    assert matches(make_issue(ls=25, le=25), make_golden(ls=20, le=22)) is True


def test_line_tolerance_beyond_plus3():
    # 报告单行 26 → [23, 29]，与 [20, 22] 无交集 → 拒绝
    assert matches(make_issue(ls=26, le=26), make_golden(ls=20, le=22)) is False


def test_line_range_intersect_exact():
    assert matches(make_issue(ls=20, le=22), make_golden(ls=20, le=22)) is True


# ---------------------------------------------------------------- 文件


def test_different_file_rejected():
    assert matches(make_issue(file="app/b.py"), make_golden()) is False


def test_backslash_path_normalized():
    assert matches(make_issue(file="app\\a.py", ls=21, le=21), make_golden()) is True


# ---------------------------------------------------------------- 类别层级


def test_category_equal_passes():
    assert matches(
        make_issue(ls=21, le=21, category=Category.SECURITY, severity=Severity.CRITICAL),
        make_golden(category="security", severity="critical"),
    ) is True


def test_cross_category_bug_security_high_mutual():
    # 报告 security/critical ↔ 金标 bug/critical（方向一）
    assert matches(
        make_issue(ls=21, le=21, category=Category.SECURITY, severity=Severity.CRITICAL),
        make_golden(category="bug", severity="critical"),
    ) is True
    # 报告 bug/high ↔ 金标 security/high（方向二）
    assert matches(
        make_issue(ls=21, le=21, category=Category.BUG, severity=Severity.HIGH),
        make_golden(category="security", severity="high"),
    ) is True


def test_cross_category_medium_rejected():
    # medium 不与 critical/high 互认
    assert matches(
        make_issue(ls=21, le=21, category=Category.SECURITY, severity=Severity.MEDIUM),
        make_golden(category="bug", severity="critical"),
    ) is False
    assert matches(
        make_issue(ls=21, le=21, category=Category.BUG, severity=Severity.HIGH),
        make_golden(category="security", severity="medium"),
    ) is False


def test_cross_category_other_pairs_rejected():
    assert matches(
        make_issue(ls=21, le=21, category=Category.PERFORMANCE, severity=Severity.CRITICAL),
        make_golden(category="bug", severity="critical"),
    ) is False
    assert matches(
        make_issue(ls=21, le=21, category=Category.STYLE, severity=Severity.LOW),
        make_golden(category="bug", severity="low"),
    ) is False


# ---------------------------------------------------------------- match_report


def test_match_report_one_golden_multiple_reports_counted_once():
    golden = make_golden(ls=10, le=12, description="G1")
    other = make_golden(file="app/z.py", ls=1, le=1, description="G2")
    reports = [
        make_issue(id="R1", ls=11, le=11),
        make_issue(id="R2", ls=12, le=13),
    ]
    result = match_report(reports, [golden, other])
    # 金标 G1 被两条报告命中 → 只计一次（索引去重）
    assert result.matched_golden_indices == [0]
    assert result.unmatched_goldens == ["G2"]
    # 命中明细保留两条，但指向同一金标
    assert len(result.matched_pairs) == 2
    assert {desc for _, desc in result.matched_pairs} == {"G1"}
    assert result.unmatched_reports == []


def test_match_report_unmatched_report_and_golden():
    reports = [make_issue(id="R1", file="app/none.py")]
    result = match_report(reports, [make_golden(description="G1")])
    assert result.unmatched_reports == ["R1"]
    assert result.matched_pairs == []
    assert result.unmatched_goldens == ["G1"]
