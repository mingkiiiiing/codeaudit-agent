"""Stage7 报告渲染：Jinja2 模板渲染 Markdown / HTML，并负责三种格式落盘。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pygments import highlight as pygments_highlight
from pygments.formatters.html import HtmlFormatter
from pygments.lexers import DiffLexer, TextLexer, get_lexer_for_filename
from pygments.util import ClassNotFound

from audit.models import AuditReport, Issue, Patch, Severity
from audit.utils import guess_language

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_SEVERITY_ORDER = (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW)
_SEVERITY_LABELS = {"critical": "致命", "high": "严重", "medium": "中等", "low": "轻微"}

_MD_ENV = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
    autoescape=False,
)
_MD_ENV.filters["cell"] = lambda v: str(v).replace("|", "\\|").replace("\n", " ")

# 行首 Markdown 结构符（R1-27）：LLM 文案若以 #/-/*/数字. 等开头，会被渲染成
# 标题/列表破坏报告结构；渲染前统一转义为字面量。
_MD_LEADING_MARK_RE = re.compile(r"^(\s{0,3})(#{1,6} |>|\- |\* |\+ |\d+[.)] )", re.MULTILINE)
_ORDERED_LIST_RE = re.compile(r"^(\d+)([.)])")


def _md_escape(value: Any) -> str:
    """转义每行行首的 Markdown 结构符（R1-27）。

    有序列表（"1. "）只转义标点（CommonMark 反斜杠转义仅对标点生效），
    其余结构符在行首加反斜杠。
    """

    def _sub(m: re.Match[str]) -> str:
        lead, mark = m.group(1), m.group(2)
        if _ORDERED_LIST_RE.match(mark):
            return lead + _ORDERED_LIST_RE.sub(r"\1\\\2", mark)
        return f"{lead}\\{mark}"

    return _MD_LEADING_MARK_RE.sub(_sub, str(value))


_MD_ENV.filters["md"] = _md_escape

_HTML_ENV = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
    autoescape=select_autoescape(default=True),  # 模板内高亮产物用 |safe 信任
)


# ---------------------------------------------------------------- 视图模型


def _fix_status_label(status: str) -> str:
    return {
        "none": "未修复",
        "patch_generated": "已生成补丁",
        "verified": "已验证",
        "needs-review": "待人工复核",
        "syntax-ok": "语法校验通过",
    }.get(status, status)


def _source_label(source: str) -> str:
    return {"rule": "规则", "llm": "LLM", "rule+llm": "规则+LLM"}.get(source, source)


def _health_level(score: float) -> str:
    if score >= 85:
        return "良好"
    if score >= 70:
        return "一般"
    if score >= 50:
        return "较差"
    return "病态"


def _issue_row(issue: Issue) -> dict[str, str]:
    """问题总表一行的纯文本字段（md/html 共用）。"""
    return {
        "id": issue.id or "-",
        "severity": issue.severity.value if isinstance(issue.severity, Severity) else str(issue.severity),
        "severity_label": _SEVERITY_LABELS.get(
            issue.severity.value if isinstance(issue.severity, Severity) else str(issue.severity), ""
        ),
        "category": issue.category.value if hasattr(issue.category, "value") else str(issue.category),
        "location": f"{issue.file}:{issue.line_start}" if issue.file else "-",
        "title": issue.title,
        "confidence": f"{issue.confidence:.0%}",
        "source": _source_label(issue.source.value if hasattr(issue.source, "value") else str(issue.source)),
        "fix_status": _fix_status_label(
            issue.fix_status.value if hasattr(issue.fix_status, "value") else str(issue.fix_status)
        ),
    }


def _highlight_code(code: str, filename: str) -> str:
    """按文件扩展名选择词法器做 pygments 高亮（noclasses 内联样式）。"""
    try:
        lexer = get_lexer_for_filename(filename or "x.txt", code)
    except ClassNotFound:
        lexer = TextLexer()
    return pygments_highlight(code or "", lexer, HtmlFormatter(noclasses=True))


def _issue_detail(issue: Issue, for_html: bool) -> dict[str, Any]:
    """critical/high 详情卡字段。html 时 code 字段为 pygments 高亮产物。"""
    row = _issue_row(issue)
    detail: dict[str, Any] = dict(row)
    detail["description"] = issue.description
    detail["evidence"] = list(issue.evidence)
    detail["suggestion"] = issue.suggestion
    detail["line_start"] = issue.line_start
    detail["line_end"] = issue.line_end
    detail["lang"] = guess_language(issue.file) or ""
    if for_html:
        detail["code_html"] = _highlight_code(issue.code_snippet, issue.file) if issue.code_snippet else ""
    else:
        detail["code"] = issue.code_snippet
    return detail


def _patch_view(patch: Patch, for_html: bool) -> dict[str, Any]:
    view: dict[str, Any] = {
        "id": patch.id or "-",
        "issue_id": patch.issue_id or "-",
        "rationale": patch.rationale,
        "apply_status": patch.apply_status,
        "tests_run": patch.tests_run,
        "tests_passed": patch.tests_passed,
        "has_diff": bool(patch.diff),
    }
    if for_html:
        view["diff_html"] = pygments_highlight(patch.diff, DiffLexer(), HtmlFormatter(noclasses=True))
    else:
        view["diff"] = patch.diff
    return view


def _base_view(report: AuditReport, for_html: bool) -> dict[str, Any]:
    """md/html 模板共用的视图模型：全部字段预先拍平为纯文本。"""
    summary = report.summary
    total_issues = sum(summary.values())
    groups: list[dict[str, Any]] = []
    for sev in _SEVERITY_ORDER:
        sev_key = sev.value
        issues = [i for i in report.issues if (i.severity.value if isinstance(i.severity, Severity) else str(i.severity)) == sev_key]
        issues.sort(key=lambda i: (i.file, i.line_start))
        groups.append(
            {
                "severity": sev_key,
                "label": _SEVERITY_LABELS[sev_key],
                "count": len(issues),
                "issues": [_issue_row(i) for i in issues],
            }
        )
    detail_issues = [
        _issue_detail(i, for_html)
        for i in report.issues
        if i.severity in (Severity.CRITICAL, Severity.HIGH)
    ]
    tests_passed = sum(1 for t in report.test_cases if t.status == "passed")
    return {
        "report": report,
        "project_name": report.project_name,
        "audit_id": report.audit_id,
        "created_at": report.created_at,
        "languages": report.languages,
        "loc": report.loc,
        "files_total": report.stats.files_total,
        "tech_stack": report.architecture.tech_stack if report.architecture else [],
        "health_score": report.health_score,
        "health_level": _health_level(report.health_score),
        "summary": summary,
        "total_issues": total_issues,
        "groups": groups,
        "detail_issues": detail_issues,
        "architecture_text": report.architecture.text if report.architecture else "",
        "architecture_modules": report.architecture.modules if report.architecture else {},
        "hotspots": report.architecture.hotspots if report.architecture else [],
        "patches": [_patch_view(p, for_html) for p in report.patches],
        "test_cases": [
            {
                "id": t.id or "-",
                "target": t.target,
                "file": t.file,
                "status": t.status,
                "kind": t.kind,
                "assert_count": t.assert_count,
            }
            for t in report.test_cases
        ],
        "tests_total": len(report.test_cases),
        "tests_passed": tests_passed,
        "stats": report.stats,
    }


# ---------------------------------------------------------------- 渲染入口


def render_markdown(report: AuditReport) -> str:
    """渲染人读 Markdown 报告（结构见 docs/01 FR-6.2）。"""
    template = _MD_ENV.get_template("report.md.j2")
    return template.render(**_base_view(report, for_html=False))


def render_html(report: AuditReport) -> str:
    """渲染 HTML 报告：内联 CSS，代码片段 pygments 高亮，diff 以 <pre> 呈现。"""
    template = _HTML_ENV.get_template("report.html.j2")
    return template.render(**_base_view(report, for_html=True))


def write_report(report: AuditReport, out_dir: Path) -> dict[str, Path]:
    """落盘 report.json / report.md / report.html，返回各格式路径字典。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": out_dir / "report.json",
        "md": out_dir / "report.md",
        "html": out_dir / "report.html",
    }
    paths["json"].write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    paths["md"].write_text(render_markdown(report), encoding="utf-8")
    paths["html"].write_text(render_html(report), encoding="utf-8")
    return paths
