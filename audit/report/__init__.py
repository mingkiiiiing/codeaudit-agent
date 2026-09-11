"""Stage7 报告生成包：聚合流水线产物并渲染 Markdown / HTML 报告。"""

from audit.report.builder import build_report
from audit.report.render import render_html, render_markdown, write_report

__all__ = ["build_report", "render_markdown", "render_html", "write_report"]
