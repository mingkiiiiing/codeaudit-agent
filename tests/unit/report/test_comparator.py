"""comparator（W24-C）单元测试：三栏划分、±3 行容差边界、规则身份隔离、load 往返。

全部离线运行，报告用 AuditReport 内存构造，不依赖存储与流水线。
"""

from __future__ import annotations

import json
from pathlib import Path

from audit.models import AuditReport, Category, Issue, Severity
from audit.report.comparator import (
    LINE_TOLERANCE,
    _rule_of,
    compare,
    format_result,
    load,
)


def _issue(
    issue_id: str,
    file: str,
    line: int,
    rule: str,
    *,
    severity: Severity = Severity.HIGH,
    title: str = "",
) -> Issue:
    """构造带 rule: evidence 标记的 issue（与 detect 引擎 hits_to_issues 同形态）。"""
    return Issue(
        id=issue_id,
        category=Category.BUG,
        severity=severity,
        title=title or f"{rule} 检出问题",
        file=file,
        line_start=line,
        line_end=line,
        evidence=[f"rule:{rule}", f"loc:{file}:{line}-{line}"],
    )


def _report(*issues: Issue, audit_id: str = "rpt") -> AuditReport:
    return AuditReport(audit_id=audit_id, issues=list(issues))


def _ids(issues: list[Issue]) -> list[str]:
    return [i.id for i in issues]


# ---------------------------------------------------------------- 三栏基本划分
def test_fixed_new_persisted_basic_partition():
    a = _report(
        _issue("a-fixed", "app/x.py", 10, "PY-SQL-INJECTION"),
        _issue("a-keep", "app/y.py", 20, "PY-NAMING"),
    )
    b = _report(
        _issue("b-new", "app/z.py", 5, "PY-SECRET"),
        _issue("b-keep", "app/y.py", 20, "PY-NAMING"),
    )
    result = compare(a, b)
    assert _ids(result["fixed"]) == ["a-fixed"]
    assert _ids(result["new"]) == ["b-new"]
    assert _ids(result["persisted"]) == ["b-keep"]


def test_empty_reports_produce_all_empty():
    result = compare(_report(), _report())
    assert result == {"fixed": [], "new": [], "persisted": []}


def test_all_fixed_when_b_empty():
    a = _report(_issue("a1", "x.py", 1, "R1"))
    result = compare(a, _report())
    assert _ids(result["fixed"]) == ["a1"]
    assert result["new"] == [] and result["persisted"] == []


def test_all_new_when_a_empty():
    b = _report(_issue("b1", "x.py", 1, "R1"))
    result = compare(_report(), b)
    assert _ids(result["new"]) == ["b1"]
    assert result["fixed"] == [] and result["persisted"] == []


# ---------------------------------------------------------------- ±3 行容差边界
def test_line_tolerance_inclusive_boundary():
    """行距恰为 ±3 匹配（persisted），±4 超容差（fixed + new）。"""
    a = _report(
        _issue("a-in", "x.py", 10, "R1"),
        _issue("a-out", "y.py", 10, "R1"),
    )
    b = _report(
        _issue("b-in", "x.py", 10 + LINE_TOLERANCE, "R1"),  # 恰好 +3：匹配
        _issue("b-out", "y.py", 10 + LINE_TOLERANCE + 1, "R1"),  # +4：超容差
    )
    result = compare(a, b)
    assert _ids(result["persisted"]) == ["b-in"]
    assert _ids(result["fixed"]) == ["a-out"]
    assert _ids(result["new"]) == ["b-out"]


def test_negative_direction_tolerance():
    """行号向上漂移（B 比 A 小 3 行）同样匹配；小 4 行不匹配。"""
    a = _report(_issue("a-in", "x.py", 10, "R1"), _issue("a-out", "y.py", 10, "R1"))
    b = _report(
        _issue("b-in", "x.py", 7, "R1"),
        _issue("b-out", "y.py", 6, "R1"),
    )
    result = compare(a, b)
    assert _ids(result["persisted"]) == ["b-in"]
    assert _ids(result["fixed"]) == ["a-out"]
    assert _ids(result["new"]) == ["b-out"]


def test_nearest_candidate_wins_when_multiple_within_tolerance():
    """容差窗内多个候选取行距最小者（2 优先于 3，且各自只配一次）。"""
    a = _report(_issue("a1", "x.py", 10, "R1"))
    b = _report(_issue("b-far", "x.py", 13, "R1"), _issue("b-near", "x.py", 12, "R1"))
    result = compare(a, b)
    assert _ids(result["persisted"]) == ["b-near"]
    assert _ids(result["new"]) == ["b-far"]


def test_one_b_issue_consumed_once_no_double_match():
    """一个 B issue 只能配一个 A：两个 A 同时落窗时只成一个 persisted。"""
    a = _report(_issue("a1", "x.py", 10, "R1"), _issue("a2", "x.py", 11, "R1"))
    b = _report(_issue("b1", "x.py", 11, "R1"))
    result = compare(a, b)
    assert _ids(result["persisted"]) == ["b1"]
    assert len(result["fixed"]) == 1


# ---------------------------------------------------------------- 规则身份隔离
def test_same_place_different_rule_not_matched():
    """不同 rule 同文件同行：不算匹配 → 一个 fixed、一个 new。"""
    a = _report(_issue("a1", "x.py", 10, "PY-SQL-INJECTION"))
    b = _report(_issue("b1", "x.py", 10, "PY-HARDCODED-SECRET"))
    result = compare(a, b)
    assert result["persisted"] == []
    assert _ids(result["fixed"]) == ["a1"]
    assert _ids(result["new"]) == ["b1"]


def test_same_place_different_file_not_matched():
    a = _report(_issue("a1", "x.py", 10, "R1"))
    b = _report(_issue("b1", "y.py", 10, "R1"))
    result = compare(a, b)
    assert result["persisted"] == []
    assert _ids(result["fixed"]) == ["a1"] and _ids(result["new"]) == ["b1"]


def test_llm_issue_without_rule_evidence_falls_back_to_title():
    """无 rule: evidence 的问题（LLM 直出）按 title 作身份：同名匹配、异名不配。"""
    llm_a = _issue("la", "x.py", 10, "R", title="空指针风险")
    llm_a.evidence = []  # 模拟无规则标记
    llm_b_same = _issue("lb", "x.py", 11, "R", title="空指针风险")
    llm_b_same.evidence = []
    llm_b_other = _issue("lo", "y.py", 10, "R", title="资源未释放")
    llm_b_other.evidence = []
    result = compare(_report(llm_a), _report(llm_b_same, llm_b_other))
    assert _ids(result["persisted"]) == ["lb"]
    assert _ids(result["new"]) == ["lo"]


def test_rule_of_prefers_evidence_marker():
    issue = _issue("i", "x.py", 1, "PY-SQL-INJECTION", title="别的标题")
    assert _rule_of(issue) == "PY-SQL-INJECTION"


# ---------------------------------------------------------------- 排序与渲染
def test_result_lists_sorted_for_stable_output():
    a = _report(
        _issue("z", "b.py", 1, "R1"),
        _issue("a", "a.py", 9, "R1"),
    )
    result = compare(a, _report())
    assert [(i.file, i.line_start) for i in result["fixed"]] == [("a.py", 9), ("b.py", 1)]


def test_format_result_contains_three_sections_and_summary():
    result = compare(
        _report(_issue("a1", "x.py", 10, "R1"), _issue("a2", "y.py", 5, "R3")),
        _report(_issue("b1", "y.py", 1, "R2"), _issue("b2", "x.py", 10, "R1")),
    )
    text = format_result(result)
    assert "已修复（A 有 B 无）：1 个" in text
    assert "新增（B 有 A 无）：1 个" in text
    assert "仍存在（两侧同现）：1 个" in text
    assert "汇总：fixed=1 new=1 persisted=1" in text
    assert "x.py:10 [R1][high]" in text  # 单行摘要形态


# ---------------------------------------------------------------- load 文件往返
def test_load_reads_to_dict_json_file(tmp_path: Path):
    report = _report(_issue("i1", "app/x.py", 10, "R1"), audit_id="rt-1")
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report.to_dict(), ensure_ascii=False), encoding="utf-8")
    loaded = load(path)
    assert loaded.audit_id == "rt-1"
    assert loaded.to_dict() == report.to_dict()
    # 往返后 compare 仍可正常匹配（evidence 里的 rule 标记保留）
    result = compare(report, loaded)
    assert _ids(result["persisted"]) == ["i1"]
