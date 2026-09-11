"""Mini Bench（docs/04 §4 校准集思路）：prompt/规则改动后的快速回归。

- :func:`build_mini_dataset`：以 demo_proj 为基线项目 + ``inject_defects``
  生成 1 个变体项目（demo 原有缺陷 + 注入缺陷），合计金标 ≥ 30 条，
  金标 JSONL 落盘 ``work_dir/goldset.jsonl``。
- :func:`run_mini_bench`：强制离线（enable_llm_review=False 且清空 api_key，
  走 FakeLLM 纯规则路径）对两个项目跑完整审计 → match_report →
  detection_metrics，返回 ``{"precision","recall","f1","pass","details"}``。

pass 判据：precision ≥ 0.6 且 recall ≥ 0.5（critical+high 层级）。
这是**纯规则离线基线**的宽松阈值，用于捕捉"改动后指标明显塌方"的回归；
LLM 通道接入后的正式阈值（docs/04 §3.1：P≥0.85 / R≥0.60）由 bench.run
真跑流程与 run 记录承载，两者口径不同，不可混用。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from audit.config import AuditConfig

from bench.goldset import GoldenIssue, from_markdown_table, save_goldset
from bench.goldset_mining import inject_defects
from bench.run import run_bench

__all__ = ["DEMO_PROJ", "MINI_PASS_PRECISION", "MINI_PASS_RECALL", "build_mini_dataset", "run_mini_bench"]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEMO_PROJ = PROJECT_ROOT / "tests" / "samples" / "demo_proj"

# 纯规则离线基线的回归阈值（可调）：低于阈值视为 prompt/规则改动引入回归
MINI_PASS_PRECISION = 0.6
MINI_PASS_RECALL = 0.5

VARIANT_DIRNAME = "mini_variant"
MINI_GOLDENS_TARGET = 30  # 合计金标下限（docs/07 §6：评测金标 ≥150 的近期目标拆分）


def build_mini_dataset(work_dir: Path) -> tuple[Path, list[GoldenIssue]]:
    """构建 mini 校准集：demo_proj（12 条金标）+ 注入变体项目，合计 ≥30 条。

    - 变体项目落在 ``work_dir/mini_variant``（已存在则重建）；
    - 注入参数 n=6 / seed=0 / per_file=4：demo_proj 有 6 个可注入 .py 文件，
      4 模板/文件 ⇒ 注入金标 24 条 + 原有 12 条 = 36 条 ≥ 30；
    - 金标 JSONL 写入 ``work_dir/goldset.jsonl``；
    - 组合金标不足 MINI_GOLDENS_TARGET 时抛 RuntimeError（模板失效等异常信号）。
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    variant_goldens = inject_defects(
        DEMO_PROJ,
        work_dir / VARIANT_DIRNAME,
        n=6,
        seed=0,
        per_file=4,
    )
    demo_goldens = from_markdown_table(DEMO_PROJ / "GOLDEN_ISSUES.md", project=DEMO_PROJ.name)
    # 变体项目继承 demo_proj 的全部已有缺陷：其金标也按变体项目名克隆一份，
    # 否则 run_bench 按项目名过滤金标时，变体继承缺陷会全部计为误报。
    # 描述加 [variant] 前缀：detection_metrics 按 description 去重，
    # 跨项目克隆的金标必须保持描述全局唯一，否则 recall 被低估。
    variant_inherited = [
        dataclasses.replace(g, project=VARIANT_DIRNAME, description=f"[variant] {g.description}")
        for g in demo_goldens
    ]
    goldens = [*demo_goldens, *variant_inherited, *variant_goldens]
    save_goldset(goldens, work_dir / "goldset.jsonl")
    if len(goldens) < MINI_GOLDENS_TARGET:
        raise RuntimeError(
            f"mini 数据集金标不足：{len(goldens)} < {MINI_GOLDENS_TARGET}"
            "（注入模板可能失效，请检查 bench.goldset_mining.DEFECT_TEMPLATES）"
        )
    return work_dir, goldens


async def run_mini_bench(config: AuditConfig | None = None) -> dict[str, Any]:
    """离线跑 mini 校准集两个项目，返回 P/R/F1 与 pass 判定。

    强制离线：无论传入 config 还是环境变量如何，均置
    ``enable_llm_review=False``、``api_key=""``、``do_fix/do_tests=False``，
    保证走 FakeLLM 纯规则路径、零网络零 LLM 调用。
    """
    base = config or AuditConfig()
    offline = dataclasses.replace(
        base,
        enable_llm_review=False,
        api_key="",
        do_fix=False,
        do_tests=False,
    )

    work_dir, goldens = build_mini_dataset(PROJECT_ROOT / "bench" / "datasets" / "mini")
    projects = [DEMO_PROJ, work_dir / VARIANT_DIRNAME]
    result = run_bench(projects, goldens, config_overrides=dataclasses.asdict(offline))

    detection = result["detection"]
    precision = detection["precision"]
    recall = detection["recall"]
    f1 = detection["f1"]
    passed = (
        precision is not None
        and recall is not None
        and precision >= MINI_PASS_PRECISION
        and recall >= MINI_PASS_RECALL
    )
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "pass": passed,
        "details": result,
    }
