"""T1 契约层自测：模型序列化回环。"""

from audit.models import (
    ArchitectureCard,
    AuditReport,
    Category,
    FixStatus,
    Issue,
    IssueSource,
    Severity,
    count_by_severity,
    health_score,
)


def make_issue(**kw) -> Issue:
    base = dict(
        id="ISS-0001",
        category=Category.BUG,
        severity=Severity.HIGH,
        title="未判空",
        file="a/b.py",
        line_start=10,
        line_end=12,
        code_snippet="x = get()\nprint(x.y)",
        description="get() 可能返回 None",
        evidence=["b.py:5 return None"],
        suggestion="增加判空",
        confidence=0.9,
        source=IssueSource.LLM,
        fix_status=FixStatus.VERIFIED,
        patch_id="PATCH-0001",
    )
    base.update(kw)
    return Issue(**base)


def test_issue_roundtrip():
    issue = make_issue()
    d = issue.to_dict()
    assert d["category"] == "bug"  # 枚举已转字符串
    assert d["severity"] == "high"
    restored = Issue.from_dict(d)
    assert restored == issue


def test_issue_partial_from_dict():
    issue = Issue.from_dict({"id": "ISS-0002", "file": "x.py", "line_start": 1, "line_end": 1})
    assert issue.severity == Severity.LOW  # 缺省字段走默认值
    assert issue.evidence == []


def test_count_and_health_score():
    issues = [
        make_issue(severity=Severity.CRITICAL),
        make_issue(severity=Severity.HIGH),
        make_issue(severity=Severity.LOW),
    ]
    counts = count_by_severity(issues)
    assert counts == {"critical": 1, "high": 1, "medium": 0, "low": 1}
    score = health_score(issues, loc=10000)  # 10 KLOC
    # weighted = 10 + 5 + 0.5 = 15.5 -> 100 - 15.5/10*50 = 22.5
    assert score == 22.5
    assert health_score([], loc=5000) == 100.0


def test_report_roundtrip():
    report = AuditReport(
        audit_id="abc",
        project_name="demo",
        languages={"python": 1.0},
        loc=100,
        health_score=88.0,
        summary={"critical": 1},
        issues=[make_issue()],
        architecture=ArchitectureCard(text="t", tech_stack=["python"], hotspots=["a.py"]),
    )
    restored = AuditReport.from_dict(report.to_dict())
    assert restored.issues[0].severity == Severity.HIGH
    assert restored.architecture is not None
    assert restored.architecture.tech_stack == ["python"]
