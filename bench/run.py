"""Bench 运行器：跑审计 → 匹配金标 → 计算指标 → 输出结构化 dict + markdown。

用法::

    python -m bench.run --goldset bench/datasets/goldset.jsonl \
        --projects path/to/proj1 path/to/proj2 --out bench/results/run.md \
        [--ablation] [--max-projects 3]

--goldset 支持 .jsonl（load_goldset）或 GOLDEN_ISSUES.md 式表格
（from_markdown_table）；缺省尝试解析 tests/samples/demo_proj/GOLDEN_ISSUES.md。
--ablation 时逐配置执行消融（可表达配置真实跑，占位配置表注"待接入"）并输出
汇总对比表；write_run_record 按 docs/04 §5 模板落盘 run 记录。
orchestrator（audit.orchestrator.pipeline.run_audit）未集成时
打印友好中文提示并以退出码 2 退出。

退出码：0 成功；1 金标/项目路径问题；2 orchestrator 未集成（argparse 用法错误亦为 2）。
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import inspect
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from audit.config import DEFAULT_MODEL, AuditConfig
from audit.models import AuditReport, Issue, count_by_severity
from audit.utils import now_iso

from bench.ablation import is_placeholder, plan_ablation
from bench.goldset import GoldenIssue, from_markdown_table, load_goldset
from bench.matcher import MatchResult, match_report, report_id
from bench.metrics import (
    cost_metrics,
    detection_metrics,
    fix_metrics,
    metrics_table,
    timing_metrics,
)

DEFAULT_LEVEL = "critical+high"
DEFAULT_GOLDSET_MD = (
    Path(__file__).resolve().parents[1] / "tests" / "samples" / "demo_proj" / "GOLDEN_ISSUES.md"
)


# ---------------------------------------------------------------- 审计调用


async def _noop_emitter(event: dict[str, Any]) -> None:
    """进度事件黑洞：bench 不展示进度。"""
    return None


def _filter_overrides(overrides: dict[str, Any]) -> dict[str, Any]:
    """过滤掉 AuditConfig 不认识的键（含消融表的 "_" 占位键）。

    ``source_path`` 由 run_bench 逐项目注入，overrides 中同名键一并丢弃，
    避免 ``from_env(source_path=..., source_path=...)`` 参数冲突。
    """
    valid = {f.name for f in dataclasses.fields(AuditConfig)}
    return {k: v for k, v in overrides.items() if k in valid and k != "source_path"}


def _invoke_run_audit(config: AuditConfig) -> AuditReport:
    """延迟导入并调用 run_audit（兼容同步/异步两种实现）。

    orchestrator 未集成（ImportError）时抛 RuntimeError("orchestrator 未集成...")。
    异步实现时优先 ``asyncio.run``；若调用方已处于运行中的事件循环内
    （如 run_mini_bench 这类 async 入口），退化为独立线程中的新 loop 执行，
    保证 run_bench 在同步/异步两种上下文都可阻塞式复用。
    """
    try:
        from audit.orchestrator.pipeline import run_audit  # 延迟导入（T5 并行开发中）
    except ImportError as exc:
        raise RuntimeError(
            "orchestrator 未集成：找不到 audit.orchestrator.pipeline.run_audit"
        ) from exc
    result = run_audit(config, _noop_emitter)
    if inspect.isawaitable(result):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            result = asyncio.run(result)
        else:  # 已在事件循环内：独立线程跑新 loop，避免 asyncio.run 嵌套报错
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(asyncio.run, result).result()
    return result


# ---------------------------------------------------------------- 主流程


def run_bench(
    project_paths: list[Path],
    goldens: list[GoldenIssue],
    config_overrides: dict[str, Any] | None = None,
    level: str = DEFAULT_LEVEL,
) -> dict[str, Any]:
    """对每个项目跑审计，聚合 detection/timing/cost/fix 指标。

    - 逐项目匹配金标（按 golden.project 过滤；无匹配则退回全量金标），
      报告 id 加项目前缀防跨项目冲突；
    - 返回结构化 dict（projects 明细 + 各指标 + 未命中金标），
      ``render_result_markdown(result)`` 可渲染为 markdown 报告。
    """
    overrides = _filter_overrides(dict(config_overrides or {}))
    project_results: list[dict[str, Any]] = []
    all_issues: list[Issue] = []
    all_patches: list[Any] = []
    durations: list[float] = []
    locs: list[int] = []
    usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "cache_hits": 0, "cache_misses": 0}
    merged = MatchResult()

    for path in project_paths:
        path = Path(path)
        config = AuditConfig.from_env(source_path=str(path), **overrides)
        start = time.perf_counter()
        report = _invoke_run_audit(config)
        duration = time.perf_counter() - start

        issues: list[Issue] = list(report.issues or [])
        loc = int(report.loc or report.stats.loc_total or 0)
        name = report.project_name or path.name
        durations.append(duration)
        locs.append(loc)
        all_patches.extend(report.patches or [])
        for key in usage:
            usage[key] += int(getattr(report.stats, key, 0) or 0)

        # 统一报告 id（加项目前缀），保证与 matched_pairs 的 id 对得上
        prefixed: list[Issue] = []
        for i, issue in enumerate(issues):
            prefixed.append(dataclasses.replace(issue, id=f"{name}:{report_id(issue, i)}"))
        all_issues.extend(prefixed)

        project_goldens = [g for g in goldens if g.project == name] or list(goldens)
        match = match_report(issues, project_goldens)
        base = len(merged.goldens)
        merged.goldens.extend(project_goldens)
        merged.matched_golden_indices.extend(base + j for j in match.matched_golden_indices)
        merged.matched_pairs.extend((f"{name}:{rid}", desc) for rid, desc in match.matched_pairs)
        merged.unmatched_reports.extend(f"{name}:{rid}" for rid in match.unmatched_reports)
        merged.unmatched_goldens.extend(match.unmatched_goldens)

        project_results.append(
            {
                "project": name,
                "path": str(path),
                "duration_sec": duration,
                "loc": loc,
                "issue_count": len(issues),
                "severity_summary": count_by_severity(issues),
                "issues": [issue.to_dict() for issue in issues],
            }
        )

    return {
        "level": level,
        "projects": project_results,
        "detection": detection_metrics(merged, len(goldens), all_issues, level),
        "timing": timing_metrics(durations, locs),
        "cost": cost_metrics(usage, sum(locs) / 1000.0 if locs else 0.0),
        "fix": fix_metrics(all_patches),
        "unmatched_goldens": merged.unmatched_goldens,
    }


# ---------------------------------------------------------------- 抽样


def sample_projects(project_paths: list[Path], n: int) -> list[Path]:
    """从项目列表确定性等距抽样 n 个（保持原顺序、去重）。

    ``n >= len`` 时原样返回；``n <= 0`` 返回空列表；``n == 1`` 取首个。
    等距口径：索引 ``floor(i * len / n)``，保证覆盖首尾分布且结果可复现。
    """
    paths = [Path(p) for p in project_paths]
    total = len(paths)
    if n <= 0 or total == 0:
        return []
    if n >= total:
        return paths
    return [paths[i * total // n] for i in range(n)]


# ---------------------------------------------------------------- 消融执行


def run_ablation(
    project_paths: list[Path],
    goldens: list[GoldenIssue],
    level: str = DEFAULT_LEVEL,
    names: Sequence[str] | None = None,
    base_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """逐配置执行消融实验（docs/04 §4）：可表达配置真实跑 run_bench，占位跳过。

    - ``names`` 限定要跑的配置名（None = 全部 7 组）；占位配置不执行审计，
      记 ``status="待接入"``；
    - ``base_overrides`` 是所有配置共享的底层覆盖（如 ``api_key=""`` 强制离线），
      会被各配置自身 overrides 覆盖；
    - 返回 ``{"level", "rows": [...]}``，row 含 config/status/note/result；
      result 为该配置的 run_bench 结果 dict。
    """
    all_configs = dict(plan_ablation())
    if names is not None:
        wanted = list(names)
        unknown = [n for n in wanted if n not in all_configs]
        if unknown:
            raise KeyError(f"未知消融配置：{unknown}（可选：{list(all_configs)}）")
        selected = [(n, dict(all_configs[n])) for n in wanted]
    else:
        selected = list(plan_ablation())

    base = dict(base_overrides or {})
    rows: list[dict[str, Any]] = []
    for name, overrides in selected:
        if is_placeholder(overrides):
            placeholder_note = next(
                (str(v) for k, v in overrides.items() if str(k).startswith("_")), ""
            )
            rows.append({"config": name, "status": "待接入", "note": placeholder_note, "result": None})
            continue
        merged = {**base, **overrides}
        result = run_bench(list(project_paths), goldens, config_overrides=merged, level=level)
        rows.append({"config": name, "status": "ok", "note": "", "result": result})
    return {"level": level, "rows": rows}


def render_ablation_table(ablation: dict[str, Any]) -> str:
    """把 run_ablation 的结果渲染为 markdown 汇总对比表（docs/04 §4）。

    列：配置 | Precision | Recall | F1 | 耗时 P50 | 耗时 P90 | tokens/KLOC | 备注。
    占位配置指标列填 "—"，备注列标"待接入"；表尾追加占位配置清单注记。
    """
    lines: list[str] = [
        "| 配置 | Precision | Recall | F1 | 耗时 P50 | 耗时 P90 | tokens/KLOC | 备注 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    pending: list[str] = []
    for row in ablation.get("rows", []):
        name = str(row.get("config", ""))
        if row.get("status") != "ok" or row.get("result") is None:
            lines.append(f"| {name} | — | — | — | — | — | — | 待接入 {row.get('note', '')} |")
            pending.append(name)
            continue
        r = row["result"]
        d = r.get("detection", {})
        t = r.get("timing", {})
        c = r.get("cost", {})

        def _f(value: Any) -> str:
            return "—" if value is None else f"{value:.3f}"

        p50 = t.get("sec_per_kloc_p50")
        p90 = t.get("sec_per_kloc_p90")
        prompt_k = c.get("prompt_tokens_per_kloc")
        comp_k = c.get("completion_tokens_per_kloc")
        if prompt_k is None and comp_k is None:
            tokens = "—"
        else:
            tokens = (
                f"{(prompt_k or 0) / 1000.0:.0f}k + {(comp_k or 0) / 1000.0:.1f}k"
            )
        lines.append(
            f"| {name} | {_f(d.get('precision'))} | {_f(d.get('recall'))} | {_f(d.get('f1'))} "
            f"| {_f(p50)} | {_f(p90)} | {tokens} | |"
        )
    if pending:
        lines.append("")
        lines.append(f"注：配置 {'、'.join(pending)} 待接入（对应 AuditConfig/运行期开关尚未暴露）。")
    return "\n".join(lines)


# ---------------------------------------------------------------- run 记录（docs/04 §5）


def _fmt3(value: float | int | None) -> str:
    return "N/A" if value is None else f"{float(value):.3f}"


def write_run_record(
    result: dict[str, Any],
    out_path: Path,
    meta: dict[str, Any] | None = None,
) -> Path:
    """按 docs/04 §5 模板把一次评估结果写成 run 记录 markdown，返回最终路径。

    - 头部：日期 / model / prompt_version（``meta['prompt_version']``，缺省
      "v2"）/ 配置（``meta['config']``，缺省 "full"），commit 可选；
    - 指标表：Precision / Recall / F1（``result['detection']``）、耗时 P50/P90
      （``result['timing']``）、tokens/KLOC（``result['cost']``）；
    - ``result['ablation']`` 存在时附消融对比表小节；
    - 备注节：``meta['notes']``（str 或 list）逐条列出；
    - ``out_path`` 为已存在目录时自动命名 ``run_YYYYMMDD_HHMMSS.md``，
      父目录自动创建。
    """
    meta = dict(meta or {})
    out_path = Path(out_path)
    if out_path.is_dir():
        out_path = out_path / f"run_{time.strftime('%Y%m%d_%H%M%S')}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    run_date = str(meta.get("date") or time.strftime("%Y-%m-%d"))
    model = str(meta.get("model") or DEFAULT_MODEL)
    prompt_version = str(meta.get("prompt_version") or "v2")
    config_name = str(meta.get("config") or "full")
    header = f"## Run {run_date} ｜ model={model} ｜ prompt_ver={prompt_version} ｜ 配置={config_name}"
    commit = str(meta.get("commit") or "")
    if commit:
        header += f" ｜ commit={commit}"

    detection = result.get("detection", {}) or {}
    level = detection.get("level", result.get("level", DEFAULT_LEVEL))
    counts = detection.get("counts", {}) or {}
    reports_in = counts.get("reports_in_level")
    goldens_in = counts.get("goldens_in_level")
    n_projects = len(result.get("projects", []) or [])

    lines: list[str] = [header, ""]
    lines += ["| 指标 | 值 | 样本量 |", "|---|---|---|"]
    lines.append(
        f"| Precision ({level}) | {_fmt3(detection.get('precision'))} "
        f"| 报告 {reports_in if reports_in is not None else 'N/A'} 条 / 金标 {goldens_in if goldens_in is not None else 'N/A'} 条 |"
    )
    lines.append(f"| Recall ({level}) | {_fmt3(detection.get('recall'))} | 金标 {goldens_in if goldens_in is not None else 'N/A'} 条 |")
    lines.append(f"| F1 ({level}) | {_fmt3(detection.get('f1'))} | — |")

    timing = result.get("timing", {}) or {}
    p50, p90 = timing.get("sec_per_kloc_p50"), timing.get("sec_per_kloc_p90")
    p50_s = "N/A" if p50 is None else f"{float(p50):.1f}"
    p90_s = "N/A" if p90 is None else f"{float(p90):.1f}"
    lines.append(f"| 耗时 P50 / P90 | {p50_s} / {p90_s} s/KLOC | {n_projects} 项目 |")

    cost = result.get("cost", {}) or {}
    prompt_k = cost.get("prompt_tokens_per_kloc")
    comp_k = cost.get("completion_tokens_per_kloc")
    if prompt_k is None and comp_k is None:
        tokens_cell = "N/A"
    else:
        tokens_cell = (
            f"{(prompt_k or 0) / 1000.0:.0f}k prompt + {(comp_k or 0) / 1000.0:.1f}k completion"
        )
    lines.append(f"| tokens/KLOC | {tokens_cell} | {n_projects} 项目 |")
    lines.append("")

    ablation = result.get("ablation")
    if ablation:
        lines += ["## 消融对比", "", render_ablation_table(ablation), ""]

    notes = meta.get("notes")
    note_items: list[str] = []
    if isinstance(notes, str) and notes.strip():
        note_items = [notes.strip()]
    elif isinstance(notes, (list, tuple)):
        note_items = [str(n) for n in notes if str(n).strip()]
    lines += ["## 备注", ""]
    if note_items:
        lines.extend(f"- {n}" for n in note_items)
    else:
        lines.append("- （无）")
    lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


# ---------------------------------------------------------------- 渲染


def render_result_markdown(result: dict[str, Any]) -> str:
    """把 run_bench 的结果 dict 渲染为 markdown 报告文本。"""
    lines: list[str] = ["# Bench 运行结果", ""]
    lines.append(f"- 生成时间：{result.get('generated_at', '')}")
    lines.append(f"- 严重度层级：{result.get('level', DEFAULT_LEVEL)}")
    lines.append(f"- 项目数：{len(result.get('projects', []))}")
    lines += ["", "## 检测质量", "", metrics_table(result.get("detection", {}))]
    timing = {k: v for k, v in result.get("timing", {}).items() if k != "items"}
    lines += ["", "## 耗时（s/KLOC）", "", metrics_table(timing)]
    lines += ["", "### 分项目明细", ""]
    lines += ["| 项目 | LOC | 耗时(s) | s/KLOC | Issue 数 |", "|---|---|---|---|---|"]
    for p in result.get("projects", []):
        loc = int(p["loc"])
        duration = float(p["duration_sec"])
        spk = f"{duration / (loc / 1000.0):.2f}" if loc > 0 else "N/A"
        lines.append(
            f"| {p['project']} | {loc} | {duration:.2f} | {spk} | {p['issue_count']} |"
        )
    lines += ["", "## 成本", "", metrics_table(result.get("cost", {}))]
    lines += ["", "## 修复", "", metrics_table(result.get("fix", {}))]
    ablation = result.get("ablation")
    if ablation:
        lines += ["", "## 消融对比", "", render_ablation_table(ablation)]
    unmatched = result.get("unmatched_goldens") or []
    if unmatched:
        lines += ["", "## 未命中金标", ""]
        lines.extend(f"- {desc}" for desc in unmatched)
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI


def build_goldset(path: Path | None, warnings_out: list[str] | None = None) -> list[GoldenIssue]:
    """按后缀加载金标：.md 走表格解析，其余走 JSONL；None 用 demo_proj 缺省表。"""
    if path is not None:
        if Path(path).suffix.lower() == ".md":
            return from_markdown_table(path, warnings_out=warnings_out)
        return load_goldset(path, warnings_out=warnings_out)
    if DEFAULT_GOLDSET_MD.exists():
        return from_markdown_table(DEFAULT_GOLDSET_MD, warnings_out=warnings_out)
    return []


def _fmt(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.3f}"


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口；返回进程退出码（--help 由 argparse 抛 SystemExit(0)）。"""
    parser = argparse.ArgumentParser(
        prog="python -m bench.run",
        description="评估 Bench：对给定项目运行审计并与金标集比对，输出检测/耗时/成本/修复指标。",
    )
    parser.add_argument(
        "--goldset",
        type=Path,
        default=None,
        help="金标集路径（.jsonl 或 GOLDEN_ISSUES.md 表格）；缺省用 tests/samples/demo_proj/GOLDEN_ISSUES.md",
    )
    parser.add_argument(
        "--projects",
        type=Path,
        nargs="+",
        default=[],
        help="待审计项目目录（一个或多个）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="markdown 结果输出路径（为已存在目录时自动命名 run_YYYYMMDD_HHMMSS.md）",
    )
    parser.add_argument("--level", default=DEFAULT_LEVEL, help="指标严重度层级，默认 critical+high")
    parser.add_argument(
        "--ablation",
        action="store_true",
        help="逐配置执行消融实验（full/−verify/rules_only 真实执行，占位配置表注'待接入'）并输出汇总对比表",
    )
    parser.add_argument(
        "--max-projects",
        type=int,
        default=None,
        metavar="N",
        help="对项目列表确定性等距抽样 N 个后执行（成本控制）",
    )
    args = parser.parse_args(argv)

    if not args.projects:
        parser.error("--projects 至少提供一个项目目录")
    if args.max_projects is not None and args.max_projects <= 0:
        parser.error("--max-projects 需为正整数")
    for p in args.projects:
        if not p.exists():
            print(f"[bench] 错误：项目路径不存在：{p}", file=sys.stderr)
            return 1

    warnings_out: list[str] = []
    goldens = build_goldset(args.goldset, warnings_out)
    for message in warnings_out:
        print(f"[bench][goldset 警告] {message}", file=sys.stderr)
    if not goldens:
        print("[bench] 错误：未能加载任何金标条目（--goldset 缺省路径也不存在）。", file=sys.stderr)
        return 1

    projects = list(args.projects)
    if args.max_projects is not None:
        projects = sample_projects(projects, args.max_projects)
        print(f"[bench] --max-projects={args.max_projects}：抽样 {len(projects)}/{len(args.projects)} 个项目")

    try:
        result = run_bench(projects, goldens, level=args.level)
        if args.ablation:
            result["ablation"] = run_ablation(projects, goldens, level=args.level)
    except RuntimeError as exc:
        print(f"[bench] {exc}", file=sys.stderr)
        print(
            "[bench] 提示：orchestrator 由 T5 负责（audit/orchestrator/pipeline.py），集成完成后再运行本命令。",
            file=sys.stderr,
        )
        return 2

    result["generated_at"] = now_iso()
    markdown = render_result_markdown(result)

    out_path = args.out
    if out_path is None:
        out_path = Path("bench") / "results" / f"run_{time.strftime('%Y%m%d_%H%M%S')}.md"
    if out_path.is_dir():
        out_path = out_path / f"run_{time.strftime('%Y%m%d_%H%M%S')}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")

    detection = result["detection"]
    counts = detection["counts"]
    print(
        f"[bench] 项目 {len(result['projects'])} 个 ｜ "
        f"Precision({detection['level']})={_fmt(detection['precision'])} ｜ "
        f"Recall={_fmt(detection['recall'])} ｜ F1={_fmt(detection['f1'])} ｜ "
        f"报告 {counts['reports_total']} 条 / 金标 {counts['goldens_total']} 条"
    )
    if result.get("ablation"):
        ablation = result["ablation"]
        ok = [r["config"] for r in ablation["rows"] if r["status"] == "ok"]
        pending = [r["config"] for r in ablation["rows"] if r["status"] != "ok"]
        print(f"[bench] 消融：已执行 {len(ok)} 组（{'、'.join(ok) or '无'}）；待接入 {len(pending)} 组")
    print(f"[bench] 结果已写入：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
