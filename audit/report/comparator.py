"""报告对比纯模块（W24-C，docs/23 卡 C）：两次审计报告的 fixed / new / persisted 三栏对比。

- 纯模块零副作用：不依赖存储与流水线，只消费 audit.models.AuditReport；
- 匹配口径：issue 按 (file, rule, 行号 ±3) 匹配——A 有 B 无 = fixed（已修复），
  B 有 A 无 = new（新增），双有 = persisted（仍存在）；
- rule 身份取 issue.evidence 中 "rule:<rule_id>" 标记（detect 引擎写入，见
  audit/detect/engine.hits_to_issues）；无该标记的问题（如 LLM 直出）回退用
  title 作身份，保证不同来源问题不跨身份误配；
- CLI 子命令 ``codeaudit diff <id1> <id2>`` 的接线不做（cli.py 归 W24-B 卡，
  集成人联调时统一加，≤10 行）；本模块提供 __main__ 入口可独立运行：
    python -m audit.report.comparator <report_a.json> <report_b.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from audit.models import AuditReport, Issue

# 行号匹配容差：同文件同规则下，行号相差 ≤ 3 行视为同一问题
# （修复/重排会让行号小幅漂移，行内容级对齐属后续轮次）
LINE_TOLERANCE = 3


def _rule_of(issue: Issue) -> str:
    """提取 issue 的规则身份：evidence 的 "rule:" 标记优先，回退 title。"""
    for item in issue.evidence or []:
        text = str(item)
        if text.startswith("rule:"):
            return text[len("rule:"):]
    return issue.title


def _match_key(issue: Issue) -> tuple[str, str]:
    """匹配键：(file, rule)。file 规范为 posix 风格，兼容不同平台产出的报告。"""
    return (str(issue.file).replace("\\", "/"), _rule_of(issue))


def _line_of(issue: Issue) -> int:
    try:
        return int(issue.line_start)
    except (TypeError, ValueError):  # pragma: no cover —— 模型字段恒为 int，防御兜底
        return 0


def _sev_of(issue: Issue) -> str:
    sev = issue.severity
    return getattr(sev, "value", str(sev))


def _describe(issue: Issue) -> str:
    """单行摘要：文件:行 [规则][严重度] 标题（三栏清单与 __main__ 输出共用）。"""
    return f"{issue.file}:{_line_of(issue)} [{_rule_of(issue)}][{_sev_of(issue)}] {issue.title}"


def compare(report_a: AuditReport, report_b: AuditReport) -> dict[str, list[Issue]]:
    """对比两份报告，返回 {"fixed": [...], "new": [...], "persisted": [...]}。

    - fixed：A 有 B 无（按 A 侧 issue 原样返回）；
    - new：B 有 A 无（按 B 侧 issue 原样返回）；
    - persisted：双有（按 B 侧 issue 返回——B 是当前状态，展示口径以现况为准）；
    - 匹配：(file, rule) 相同且 |line_a - line_b| <= LINE_TOLERANCE；多个候选取
      行距最小者（同距取行号小者），保证结果对输入顺序稳定；
    - 三个清单均按 (file, 行号, 规则) 排序输出，便于人工比对与快照断言。
    """
    a_issues = list(report_a.issues or [])
    b_issues = list(report_b.issues or [])

    # B 侧按匹配键建索引：(file, rule) -> [(line_start, b下标)]，升序便于就近取
    b_index: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for idx, issue in enumerate(b_issues):
        b_index.setdefault(_match_key(issue), []).append((_line_of(issue), idx))
    for candidates in b_index.values():
        candidates.sort()

    matched_b: set[int] = set()
    fixed: list[Issue] = []
    persisted: list[Issue] = []
    for issue in a_issues:
        key = _match_key(issue)
        line_a = _line_of(issue)
        best: tuple[int, int] | None = None  # (b行号, b下标)，取行距最小者
        for line_b, idx in b_index.get(key, ()):
            if idx in matched_b or abs(line_a - line_b) > LINE_TOLERANCE:
                continue
            if best is None or abs(line_a - line_b) < abs(line_a - best[0]):
                best = (line_b, idx)
        if best is None:
            fixed.append(issue)
        else:
            matched_b.add(best[1])
            persisted.append(b_issues[best[1]])
    new = [issue for idx, issue in enumerate(b_issues) if idx not in matched_b]

    def _ordered(items: list[Issue]) -> list[Issue]:
        return sorted(items, key=lambda i: (_match_key(i)[0], _line_of(i), _match_key(i)[1]))

    return {"fixed": _ordered(fixed), "new": _ordered(new), "persisted": _ordered(persisted)}


def load(path: str | Path) -> AuditReport:
    """从 JSON 文件加载报告（set_report 落库同构的 to_dict 形态）。"""
    data: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    return AuditReport.from_dict(data)


def format_result(result: dict[str, list[Issue]]) -> str:
    """把 compare 结果渲染为中文三栏文本（__main__ 与测试快照共用）。"""
    sections = (
        ("已修复（A 有 B 无）", "fixed"),
        ("新增（B 有 A 无）", "new"),
        ("仍存在（两侧同现）", "persisted"),
    )
    lines: list[str] = []
    for title, key in sections:
        items = result[key]
        lines.append(f"{title}：{len(items)} 个")
        lines.extend(f"  - {_describe(issue)}" for issue in items)
    lines.append(
        f"汇总：fixed={len(result['fixed'])} new={len(result['new'])}"
        f" persisted={len(result['persisted'])}"
    )
    return "\n".join(lines)


def _main(argv: list[str] | None = None) -> int:
    """独立运行入口：python -m audit.report.comparator <report_a.json> <report_b.json>。"""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m audit.report.comparator",
        description="两次审计报告对比：fixed / new / persisted 三栏（W24-C）",
    )
    parser.add_argument("report_a", help="旧报告 JSON 路径（基线侧 A）")
    parser.add_argument("report_b", help="新报告 JSON 路径（当前侧 B）")
    args = parser.parse_args(argv)

    report_a = load(args.report_a)
    report_b = load(args.report_b)
    print(f"报告对比：A={report_a.audit_id or args.report_a}  B={report_b.audit_id or args.report_b}")
    print(format_result(compare(report_a, report_b)))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
