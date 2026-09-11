"""指标计算（docs/04 §3）：检测质量 / 耗时分位 / 成本 / 修复质量。

全部为纯函数；分母为 0 或输入为空时对应指标返回 ``None``（而非抛错），
``metrics_table`` 把 ``None`` 渲染为 ``N/A``。
"""

from __future__ import annotations

import enum
import json
import math
import statistics
from collections import Counter
from typing import Any

from audit.models import Issue, Severity

from bench.matcher import MatchResult, report_id

# Patch.apply_status 语义（audit.models.Patch 注释）：
#   pending | verified | needs-review | syntax-ok | failed
_SYNTAX_OK_STATUSES = frozenset({"syntax-ok", "verified"})  # 语法可用（verified 隐含语法通过）
_SANDBOX_ENTERED = frozenset({"verified", "needs-review", "failed"})  # 进入过沙箱验证


# ---------------------------------------------------------------- 检测质量


def parse_level(level: str) -> frozenset[str] | None:
    """把层级表达式解析为严重度集合。

    ``"critical+high"`` → {critical, high}；``"all"`` / 空串 → ``None``（不过滤）。
    含未知 token 时抛 ``ValueError``。
    """
    text = (level or "").strip().lower()
    if text in {"", "all", "*"}:
        return None
    tokens = [t.strip() for t in text.split("+") if t.strip()]
    if not tokens:
        return None
    valid = {s.value for s in Severity}
    unknown = [t for t in tokens if t not in valid]
    if unknown:
        raise ValueError(f"未知严重度 token：{unknown}（合法值：{sorted(valid)} 或 all）")
    return frozenset(tokens)


def _sev(issue: Issue) -> str:
    value = issue.severity
    return value.value if isinstance(value, enum.Enum) else str(value)


def detection_metrics(
    match: MatchResult,
    goldens_total: int,
    reports: list[Issue],
    level: str = "critical+high",
) -> dict[str, Any]:
    """检测质量指标（docs/04 §3.1），只在指定严重度层级上计算。

    - precision = 命中金标的报告数(层内) / 层内报告总数；
    - recall    = 被命中的金标数(层内) / 层内金标总数；
    - f1        = 2PR/(P+R)；
    - 任一分母为 0 时该指标为 ``None``。

    金标层内集合取自 ``match.goldens``（match_report 会填充）；若调用方手工
    构造 MatchResult 未填 goldens，则退回 ``goldens_total`` 且命中金标无法按
    层过滤（按全部命中对计）。``reports`` 应与传给 ``match_report`` 的列表同序，
    以便空 id 的兜底编号（report_id）一致。
    """
    sevs = parse_level(level)
    reports_in = [r for r in reports if sevs is None or _sev(r) in sevs]

    if match.goldens:
        goldens_in = [g for g in match.goldens if sevs is None or str(g.severity) in sevs]
        goldens_in_level = len(goldens_in)
        matched_goldens_in_level = {
            str(match.goldens[j].description)
            for j in match.matched_golden_indices
            if sevs is None or str(match.goldens[j].severity) in sevs
        }
    else:
        goldens_in_level = goldens_total
        matched_goldens_in_level = {desc for _, desc in match.matched_pairs}

    matched_report_ids = {rid for rid, _ in match.matched_pairs}
    matched_reports_in_level = sum(
        1 for i, r in enumerate(reports_in) if report_id(r, i) in matched_report_ids
    )

    precision = (matched_reports_in_level / len(reports_in)) if reports_in else None
    recall = (len(matched_goldens_in_level) / goldens_in_level) if goldens_in_level else None
    if precision is None or recall is None or precision + recall == 0:
        f1: float | None = None
    else:
        f1 = 2.0 * precision * recall / (precision + recall)

    return {
        "level": level,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "counts": {
            "reports_total": len(reports),
            "reports_in_level": len(reports_in),
            "reports_matched": matched_reports_in_level,
            "goldens_total": goldens_total,
            "goldens_in_level": goldens_in_level,
            "goldens_matched": len(matched_goldens_in_level),
        },
    }


# ---------------------------------------------------------------- 耗时


def _percentile(values: list[float], q: float) -> float | None:
    """线性插值分位数（numpy 默认口径）；空列表返回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * (q / 100.0)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    frac = pos - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def timing_metrics(durations_sec: list[float], locs: list[int]) -> dict[str, Any]:
    """耗时指标：逐项目 sec/KLOC 后取 P50/P90/mean（docs/04 §3.2）。

    ``locs`` 缺项或 ≤0 的项目不计入分位统计（明细中该值记 ``None``）。
    空输入安全：各分位为 ``None``。
    """
    items: list[dict[str, Any]] = []
    rates: list[float] = []
    for idx, duration in enumerate(durations_sec):
        loc = locs[idx] if idx < len(locs) else 0
        rate = (float(duration) / (loc / 1000.0)) if loc and loc > 0 else None
        items.append({"duration_sec": duration, "loc": loc, "sec_per_kloc": rate})
        if rate is not None:
            rates.append(rate)
    return {
        "n": len(items),
        "sec_per_kloc_p50": _percentile(rates, 50.0),
        "sec_per_kloc_p90": _percentile(rates, 90.0),
        "sec_per_kloc_mean": statistics.fmean(rates) if rates else None,
        "items": items,
    }


# ---------------------------------------------------------------- 成本


def cost_metrics(usage: dict[str, Any], kloc: float) -> dict[str, Any]:
    """成本指标（docs/04 §3.5）：tokens/KLOC（prompt/completion 分列）与缓存命中率。

    ``usage`` 接受 AuditStats.to_dict() 同构 dict（prompt_tokens / completion_tokens /
    cache_hits / cache_misses）。``kloc <= 0`` 时 per-KLOC 值为 ``None``；
    无任何调用记录时缓存命中率为 ``None``。
    """
    usage = usage or {}

    def _int(key: str) -> int:
        try:
            return int(usage.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    prompt = _int("prompt_tokens")
    completion = _int("completion_tokens")
    hits = _int("cache_hits")
    misses = _int("cache_misses")
    total_calls = hits + misses
    positive_kloc = bool(kloc) and kloc > 0
    return {
        "kloc": kloc,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "prompt_tokens_per_kloc": (prompt / kloc) if positive_kloc else None,
        "completion_tokens_per_kloc": (completion / kloc) if positive_kloc else None,
        "cache_hits": hits,
        "cache_misses": misses,
        "cache_hit_ratio": (hits / total_calls) if total_calls > 0 else None,
    }


# ---------------------------------------------------------------- 修复质量


def _patch_status(patch: Any) -> str:
    """兼容 Patch 对象与 dict 两种输入，取出 apply_status。"""
    if isinstance(patch, dict):
        return str(patch.get("apply_status") or "")
    return str(getattr(patch, "apply_status", "") or "")


def fix_metrics(patches: list[Any]) -> dict[str, Any]:
    """修复质量指标（docs/04 §3.4，按 apply_status 字段统计）。

    - 语法可用率 = apply_status ∈ {syntax-ok, verified} 数 / Patch 总数；
    - 验证通过率 = verified 数 / 进入沙箱验证数（{verified, needs-review, failed}）；
    - 空列表或无人进入验证时对应比率为 ``None``。
    """
    statuses = [_patch_status(p) for p in (patches or [])]
    total = len(statuses)
    counts = dict(Counter(statuses))
    syntax_ok = sum(1 for s in statuses if s in _SYNTAX_OK_STATUSES)
    entered = sum(1 for s in statuses if s in _SANDBOX_ENTERED)
    verified = counts.get("verified", 0)
    return {
        "total": total,
        "status_counts": counts,
        "syntax_ok": syntax_ok,
        "syntax_ok_ratio": (syntax_ok / total) if total else None,
        "entered_verify": entered,
        "verified": verified,
        "verified_ratio": (verified / entered) if entered else None,
    }


# ---------------------------------------------------------------- 渲染


def _flatten(prefix: str, value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key, sub in value.items():
            _flatten(f"{prefix}.{key}" if prefix else str(key), sub, out)
    else:
        out[prefix] = value


def _format_value(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        text = f"{value:.4f}".rstrip("0").rstrip(".")
        return text or "0"
    if isinstance(value, (list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def metrics_table(metrics: dict[str, Any]) -> str:
    """把（可嵌套的）指标 dict 渲染为 markdown 两列表格。"""
    flat: dict[str, Any] = {}
    _flatten("", metrics, flat)
    lines = ["| 指标 | 值 |", "|---|---|"]
    lines.extend(f"| {key} | {_format_value(value)} |" for key, value in flat.items())
    return "\n".join(lines)
