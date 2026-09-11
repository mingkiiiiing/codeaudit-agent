"""真跑评估脚本（docs/04 §5 / docs/07 §6）——需要真实 GLM_API_KEY，**不进测试**。

用法::

    # 预检：只打印项目清单 / 金标规模 / 预估 LLM 调用与成本（无需 key）
    python -m bench.real_run --projects path/to/proj1 path/to/proj2 --dry-run

    # 真跑：完整评估（可叠加消融），写 run 记录
    export GLM_API_KEY=sk-xxx
    python -m bench.real_run --projects path/to/proj1 ... \
        --goldset bench/datasets/goldset.jsonl --ablation \
        --out bench/results/run_20260911.md

行为约定：
- 未配置 ``GLM_API_KEY`` 且非 ``--dry-run`` 时打印中文提示并以退出码 2 退出；
- ``--dry-run`` 零网络零审计：按 docs/04 §5 run 记录的 tokens/KLOC 经验值
  （58k prompt + 4k completion）与占位牌价估算调用次数与成本，并注明假设；
- 真跑复用 :func:`bench.run.run_bench` / :func:`bench.run.run_ablation`
  （AuditConfig.from_env 自动读取 GLM_API_KEY / GLM_MODEL / GLM_BASE_URL），
  结果经 :func:`bench.run.write_run_record` 落盘为 docs/04 §5 格式 run 记录。

退出码：0 成功；1 项目/金标路径问题；2 未配置 GLM_API_KEY（或 orchestrator 缺失）。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from audit.config import DEFAULT_MODEL

from bench.run import (
    build_goldset,
    run_ablation,
    run_bench,
    sample_projects,
    write_run_record,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# docs/04 §5 run 记录经验值：每 KLOC 的 token 消耗（prompt + completion 分列）
EST_PROMPT_TOKENS_PER_KLOC = 58_000
EST_COMPLETION_TOKENS_PER_KLOC = 4_000
# 占位牌价（元 / 1M tokens）：真跑前请按 GLM 最新牌价修改，输出会注明假设
EST_PRICE_PROMPT_PER_1M_CNY = 2.0
EST_PRICE_COMPLETION_PER_1M_CNY = 8.0

SOURCE_EXTS: frozenset[str] = frozenset({".py", ".js", ".ts"})

# 每源文件至少 1 次 LLM 审查调用（review_mode=simple 的下界估算）
REVIEW_CALLS_PER_SOURCE_FILE = 1


def estimate_project(path: Path) -> dict[str, int]:
    """估算单个项目的规模：源文件数与物理行数（跳过隐藏目录/依赖目录）。"""
    files = 0
    loc = 0
    skip_dirs = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".codeaudit"}
    for p in sorted(Path(path).rglob("*")):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(path).parts
        if any(part in skip_dirs or part.startswith(".") for part in rel_parts[:-1]):
            continue
        if p.suffix.lower() not in SOURCE_EXTS:
            continue
        files += 1
        try:
            loc += len(p.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
    return {"files": files, "loc": loc}


def estimate_usage(
    scales: list[dict[str, int]],
) -> dict[str, float | int]:
    """按 docs/04 §5 经验值估算 LLM 调用次数与 token/成本（不含 Verify 复核）。"""
    files = sum(s["files"] for s in scales)
    loc = sum(s["loc"] for s in scales)
    kloc = loc / 1000.0
    prompt_tokens = kloc * EST_PROMPT_TOKENS_PER_KLOC
    completion_tokens = kloc * EST_COMPLETION_TOKENS_PER_KLOC
    cost = (prompt_tokens / 1e6) * EST_PRICE_PROMPT_PER_1M_CNY + (
        completion_tokens / 1e6
    ) * EST_PRICE_COMPLETION_PER_1M_CNY
    return {
        "source_files": files,
        "loc": loc,
        "kloc": kloc,
        "review_calls": files * REVIEW_CALLS_PER_SOURCE_FILE,
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "cost_cny_est": cost,
    }


def _git_commit(root: Path) -> str:
    """项目根的 git 短 commit（本地 git，list 参数无 shell）；失败返回空串。"""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return proc.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _prompt_version() -> str:
    """读取 prompts 的 PROMPT_VERSION；模块缺失时回退 "v2"。"""
    try:
        from audit.agents.prompts import PROMPT_VERSION

        return str(PROMPT_VERSION)
    except Exception:  # pragma: no cover - prompts 缺失时静默回退
        return "v2"


def _default_out() -> Path:
    return PROJECT_ROOT / "bench" / "results" / f"run_{time.strftime('%Y%m%d')}.md"


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口；返回进程退出码（--help 由 argparse 抛 SystemExit(0)）。"""
    parser = argparse.ArgumentParser(
        prog="python -m bench.real_run",
        description="真跑评估：对金标项目运行完整审计（需 GLM_API_KEY），产出 docs/04 §5 run 记录。",
    )
    parser.add_argument(
        "--projects", type=Path, nargs="+", default=[], help="待审计项目目录（一个或多个）"
    )
    parser.add_argument(
        "--goldset", type=Path, default=None, help="金标集路径（.jsonl 或 .md 表格）；缺省用 demo_proj 金标表"
    )
    parser.add_argument("--ablation", action="store_true", help="追加消融实验（可表达配置真实执行）")
    parser.add_argument("--out", type=Path, default=None, help="run 记录输出路径（缺省 bench/results/run_YYYYMMDD.md）")
    parser.add_argument("--level", default="critical+high", help="指标严重度层级，默认 critical+high")
    parser.add_argument(
        "--max-projects", type=int, default=None, metavar="N", help="对项目列表确定性等距抽样 N 个（成本控制）"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="预检：只打印项目清单/金标规模/预估调用与成本，不做任何审计"
    )
    args = parser.parse_args(argv)

    if not args.projects:
        parser.error("--projects 至少提供一个项目目录")
    if args.max_projects is not None and args.max_projects <= 0:
        parser.error("--max-projects 需为正整数")
    for p in args.projects:
        if not p.exists():
            print(f"[real_run] 错误：项目路径不存在：{p}", file=sys.stderr)
            return 1

    api_key = os.environ.get("GLM_API_KEY", "").strip()

    # -------- 预检（--dry-run）：零网络、零审计、无 key 也可执行
    if args.dry_run:
        if not api_key:
            print("[real_run] 提示：未检测到 GLM_API_KEY，当前仅做成本估算，不执行审计。")
        projects = sample_projects(list(args.projects), args.max_projects or len(args.projects))
        scales = [estimate_project(p) for p in projects]
        warnings_out: list[str] = []
        goldens = build_goldset(args.goldset, warnings_out)
        high = sum(1 for g in goldens if g.severity in {"critical", "high"})

        print(f"[real_run] 项目清单（{len(projects)} 个）：")
        for p, s in zip(projects, scales):
            print(f"  - {p} ｜ 源文件 {s['files']} 个 ｜ {s['loc']} 行")
        print(f"[real_run] 金标规模：共 {len(goldens)} 条，其中 critical+high {high} 条")
        if not goldens:
            print("[real_run] 警告：未能加载任何金标条目（--goldset 缺省路径也不存在）。", file=sys.stderr)

        est = estimate_usage(scales)
        print(
            f"[real_run] 预估 LLM 审查调用 ≈ {est['review_calls']} 次"
            "（按每源文件 1 次 review 估算，未含 Verify 复核与重试）"
        )
        print(
            f"[real_run] 预估 tokens ≈ {est['prompt_tokens']} prompt + "
            f"{est['completion_tokens']} completion（按 docs/04 经验值 "
            f"{EST_PROMPT_TOKENS_PER_KLOC // 1000}k + {EST_COMPLETION_TOKENS_PER_KLOC // 1000}k tokens/KLOC）"
        )
        print(
            f"[real_run] 预估成本 ≈ {est['cost_cny_est']:.2f} 元（占位牌价 prompt "
            f"{EST_PRICE_PROMPT_PER_1M_CNY} 元/1M、completion {EST_PRICE_COMPLETION_PER_1M_CNY} 元/1M，"
            "请按 GLM 最新牌价核对）"
        )
        print("[real_run] 以上为估算；确认后去掉 --dry-run 真跑。")
        return 0

    # -------- 真跑：必须有 key
    if not api_key:
        print("[real_run] 错误：未检测到环境变量 GLM_API_KEY，真跑评估无法进行。", file=sys.stderr)
        print("[real_run] 请先配置：export GLM_API_KEY=sk-xxx（Git Bash）或 setx GLM_API_KEY sk-xxx（PowerShell 持久化）。", file=sys.stderr)
        print("[real_run] 若只想预估成本，请加 --dry-run（无需 key）。", file=sys.stderr)
        return 2

    goldens = build_goldset(args.goldset)
    if not goldens:
        print("[real_run] 错误：未能加载任何金标条目（--goldset 缺省路径也不存在）。", file=sys.stderr)
        return 1

    projects = list(args.projects)
    if args.max_projects is not None:
        projects = sample_projects(projects, args.max_projects)
        print(f"[real_run] --max-projects={args.max_projects}：抽样 {len(projects)}/{len(args.projects)} 个项目")

    try:
        result = run_bench(projects, goldens, level=args.level)
        if args.ablation:
            print("[real_run] 主配置完成，开始消融实验（full/−verify/rules_only 真实执行）...")
            result["ablation"] = run_ablation(projects, goldens, level=args.level)
    except RuntimeError as exc:
        print(f"[real_run] {exc}", file=sys.stderr)
        return 2

    meta: dict[str, Any] = {
        "model": os.environ.get("GLM_MODEL", "").strip() or DEFAULT_MODEL,
        "prompt_version": _prompt_version(),
        "commit": _git_commit(PROJECT_ROOT),
        "config": "full(+ablation)" if args.ablation else "full",
        "notes": [
            f"项目数 {len(result.get('projects', []))}；金标 {len(goldens)} 条；层级 {args.level}",
            "耗时口径：Ingest 起点至 Report 落盘的 wall time（含全部 LLM 调用），不含用户上传传输时间。",
        ],
    }
    out_path = write_run_record(result, args.out or _default_out(), meta)

    detection = result["detection"]

    def _f(value: Any) -> str:
        return "N/A" if value is None else f"{value:.3f}"

    print(
        f"[real_run] Precision({detection['level']})={_f(detection['precision'])} ｜ "
        f"Recall={_f(detection['recall'])} ｜ F1={_f(detection['f1'])}"
    )
    print(f"[real_run] run 记录已写入：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
