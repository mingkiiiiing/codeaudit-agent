"""报告 ↔ 金标匹配器（docs/04 §2.3）。

命中判定（``matches``）当且仅当同时满足：

1. **同文件**：路径按 posix 规范化（反斜杠→斜杠、去 ``./``）后相等；
2. **行区间相交（±3 容差）**：报告区间两端外扩 3 行后与金标区间有交集；
3. **类别兼容**：类别相等直接通过；跨类别仅 ``bug``↔``security`` 且双方
   severity 均 ∈ {critical, high} 时互认（medium/low 不与 critical/high 互认）。

``match_report`` 汇总：一条金标被多条报告命中只计一次（recall 口径），
命中明细对保留在 ``matched_pairs``（precision 口径按"去重的报告 id"统计）。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from audit.models import Category, Issue, Severity

from bench.goldset import GoldenIssue

LINE_TOLERANCE = 3  # 行区间两端容差（docs/04 §2.3：相差 ≤ 3 行仍算命中）

_HIGH_SEVERITIES: frozenset[str] = frozenset({Severity.CRITICAL.value, Severity.HIGH.value})
_CROSS_CATEGORY_PAIR: frozenset[str] = frozenset({Category.BUG.value, Category.SECURITY.value})


@dataclass
class MatchResult:
    """一次 ``match_report`` 的结果。

    - ``matched_pairs``：全部命中明细 (report_id, golden 描述)；
    - ``unmatched_reports``：未命中任何金标的报告 id 列表；
    - ``unmatched_goldens``：未被任何报告命中的金标描述列表；
    - ``goldens`` / ``matched_golden_indices``：供指标计算按严重度分层使用。
    """

    matched_pairs: list[tuple[str, str]] = field(default_factory=list)
    unmatched_reports: list[str] = field(default_factory=list)
    unmatched_goldens: list[str] = field(default_factory=list)
    goldens: list[GoldenIssue] = field(default_factory=list)
    matched_golden_indices: list[int] = field(default_factory=list)


def _enum_str(value: Any) -> str:
    """枚举或字符串统一转为字符串值。"""
    return value.value if isinstance(value, enum.Enum) else str(value)


def normalize_file(path: str) -> str:
    """posix 规范化：反斜杠→斜杠，去 ``./`` 前缀。"""
    return PurePosixPath(str(path).replace("\\", "/")).as_posix()


def _sorted_span(start: int, end: int) -> tuple[int, int]:
    return (start, end) if start <= end else (end, start)


def matches(report_issue: Issue, golden: GoldenIssue) -> bool:
    """判定一条报告 Issue 是否命中一条金标（docs/04 §2.3）。"""
    if normalize_file(report_issue.file) != normalize_file(golden.file):
        return False
    rs, re_ = _sorted_span(int(report_issue.line_start), int(report_issue.line_end))
    gs, ge = _sorted_span(int(golden.line_start), int(golden.line_end))
    if re_ + LINE_TOLERANCE < gs or rs - LINE_TOLERANCE > ge:
        return False
    report_cat = _enum_str(report_issue.category)
    golden_cat = _enum_str(golden.category)
    if report_cat == golden_cat:
        return True
    # 跨类别：仅 bug↔security 且双方 severity 均 ∈ {critical, high}
    if {report_cat, golden_cat} == _CROSS_CATEGORY_PAIR:
        return _enum_str(report_issue.severity) in _HIGH_SEVERITIES and _enum_str(golden.severity) in _HIGH_SEVERITIES
    return False


def report_id(issue: Issue, index: int) -> str:
    """报告 id：优先 Issue.id，缺失时用位置兜底（调用方两侧需同序）。"""
    return str(issue.id) if issue.id else f"report#{index}"


def match_report(issues: list[Issue], goldens: list[GoldenIssue]) -> MatchResult:
    """报告列表与金标列表整体匹配。

    一条金标被多条报告命中只计一次：``matched_golden_indices`` 去重；
    ``matched_pairs`` 保留全部命中明细供 precision 按报告去重统计。
    """
    result = MatchResult(goldens=list(goldens))
    matched: set[int] = set()
    for i, issue in enumerate(issues):
        rid = report_id(issue, i)
        hits = [j for j, golden in enumerate(goldens) if matches(issue, golden)]
        if not hits:
            result.unmatched_reports.append(rid)
            continue
        for j in hits:
            matched.add(j)
            result.matched_pairs.append((rid, str(goldens[j].description)))
    result.matched_golden_indices = sorted(matched)
    result.unmatched_goldens = [
        str(golden.description) for j, golden in enumerate(goldens) if j not in matched
    ]
    return result
