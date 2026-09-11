"""评估 Bench（T6）：金标集、匹配器、指标计算、运行器与消融配置。

对外入口（另可 ``python -m bench.run`` / ``python -m bench.real_run`` 直接执行）：

    from bench import GoldenIssue, load_goldset, save_goldset, from_markdown_table
    from bench import matches, match_report, MatchResult
    from bench import detection_metrics, timing_metrics, cost_metrics, fix_metrics, metrics_table
    from bench import run_bench, run_ablation, sample_projects, write_run_record
    from bench import ABLATION_CONFIGS, plan_ablation, is_placeholder
    from bench import calc_efficiency, write_timing_template, efficiency_from_rows
"""

from __future__ import annotations

from typing import Any

from bench.ablation import ABLATION_CONFIGS, is_placeholder, plan_ablation
from bench.efficiency import calc_efficiency, efficiency_from_rows, write_timing_template
from bench.goldset import GoldenIssue, from_markdown_table, load_goldset, save_goldset
from bench.matcher import LINE_TOLERANCE, MatchResult, match_report, matches
from bench.metrics import (
    cost_metrics,
    detection_metrics,
    fix_metrics,
    metrics_table,
    timing_metrics,
)

_LAZY_RUN_EXPORTS = frozenset(
    {
        "run_ablation",
        "run_bench",
        "render_ablation_table",
        "render_result_markdown",
        "sample_projects",
        "write_run_record",
    }
)

__all__ = [
    "ABLATION_CONFIGS",
    "LINE_TOLERANCE",
    "MatchResult",
    "GoldenIssue",
    "calc_efficiency",
    "cost_metrics",
    "detection_metrics",
    "efficiency_from_rows",
    "fix_metrics",
    "from_markdown_table",
    "is_placeholder",
    "load_goldset",
    "match_report",
    "matches",
    "metrics_table",
    "plan_ablation",
    "save_goldset",
    "timing_metrics",
    "write_timing_template",
]


def __getattr__(name: str) -> Any:  # 延迟导出：避免 python -m bench.run 的 sys.modules 冲突警告
    if name in _LAZY_RUN_EXPORTS:
        import importlib

        return getattr(importlib.import_module("bench.run"), name)
    raise AttributeError(f"module 'bench' has no attribute {name!r}")
