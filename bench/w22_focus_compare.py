"""W22-E 一次性驱动脚本：full vs llm_focus 两组在线对数（GLM-5.3 Flash）。

目的（docs/19 §验收）：为 W22-B 风险聚焦翻默认提供判据——
- 耗时：llm_focus 的 s/KLOC 相对 full 的下降幅度（目标 P50 ≤ 300 s/KLOC）；
- 精度：Precision 降幅 ≤ 3pp（0.884 基线）。

用法（需 GLM_API_KEY，模型取 GLM_MODEL 缺省 glm-5.3-flash）：
  python -m bench.w22_focus_compare

产物：bench/results/run_<日期>_w22_focus.md（full + llm_focus 两组记录）。
本脚本为 Wave 22 评估附属工具，跑完对数后保留在 bench/ 供复跑。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bench.ablation import ABLATION_CONFIGS  # noqa: E402
from bench.real_run import _git_commit, _prompt_version  # noqa: E402
from bench.run import build_goldset, run_ablation, run_bench, write_run_record  # noqa: E402

PROJECTS = [
    PROJECT_ROOT / "bench/datasets/projects/blogengine",
    PROJECT_ROOT / "bench/datasets/projects/blogengine_inj",
    PROJECT_ROOT / "bench/datasets/projects/datatools",
    PROJECT_ROOT / "bench/datasets/projects/datatools_inj",
    PROJECT_ROOT / "tests/samples/demo_proj",
    PROJECT_ROOT / "bench/datasets/projects/demo_proj_inj",
    PROJECT_ROOT / "bench/datasets/projects/shopcore",
    PROJECT_ROOT / "bench/datasets/projects/shopcore_inj",
    PROJECT_ROOT / "bench/datasets/projects/webapi",
    PROJECT_ROOT / "bench/datasets/projects/webapi_inj",
]
GOLDSET = PROJECT_ROOT / "bench/datasets/goldset.jsonl"


def main() -> int:
    # .env 自动加载与"真实环境优先"口径同 CLI（AuditConfig.from_env）；
    # 拿到 key 后回写 os.environ，供 run_bench 内部的 os.environ 读法保持一致。
    from audit.config import AuditConfig

    cfg = AuditConfig.from_env()
    if not cfg.api_key:
        print("[w22] 错误：未检测到 GLM_API_KEY（.env 自动加载需在仓库根运行）。", file=sys.stderr)
        return 2
    os.environ.setdefault("GLM_API_KEY", cfg.api_key)
    for p in PROJECTS:
        if not p.exists():
            print(f"[w22] 错误：项目路径不存在：{p}", file=sys.stderr)
            return 1
    warnings: list[str] = []
    goldens = build_goldset(GOLDSET, warnings)
    if not goldens:
        print("[w22] 错误：金标加载失败。", file=sys.stderr)
        return 1

    started = time.time()
    print(f"[w22] full 组开始：{len(PROJECTS)} 项目 / 金标 {len(goldens)} 条 ...")
    result = run_bench(PROJECTS, goldens, level="critical+high")
    det = result["detection"]
    print(f"[w22] full：P={det['precision']:.3f} R={det['recall']:.3f} F1={det['f1']:.3f}")

    print("[w22] llm_focus 组开始（llm_review_top_files=25）...")
    ablation = run_ablation(PROJECTS, goldens, level="critical+high", names=["llm_focus"])
    result["ablation"] = ablation
    focus = (ablation or {}).get("llm_focus") or {}
    fdet = focus.get("detection") or {}
    if fdet:
        print(f"[w22] llm_focus：P={fdet.get('precision'):.3f} R={fdet.get('recall'):.3f} F1={fdet.get('f1'):.3f}")

    meta = {
        "model": os.environ.get("GLM_MODEL", "").strip() or "glm-5.3-flash",
        "prompt_version": _prompt_version(),
        "commit": _git_commit(PROJECT_ROOT),
        "config": "full + llm_focus（W22-B 翻默认对数）",
        "notes": [
            f"项目数 {len(PROJECTS)}；金标 {len(goldens)} 条；层级 critical+high",
            f"脚本 bench/w22_focus_compare.py；总耗时 {time.time() - started:.0f}s",
            f"llm_focus 组 overrides：{ABLATION_CONFIGS['llm_focus']}",
            "耗时口径：Ingest 起点至 Report 落盘的 wall time（含全部 LLM 调用）。",
        ],
    }
    out = PROJECT_ROOT / "bench" / "results" / f"run_{time.strftime('%Y%m%d')}_w22_focus.md"
    written = write_run_record(result, out, meta)
    print(f"[w22] 记录已写入：{written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
