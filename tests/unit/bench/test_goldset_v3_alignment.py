"""W3-A1 金标↔规则对齐测试：抽样金标必须能在对应项目上被 DEFAULT_REGISTRY 命中。

口径（任务书 C-2）：随机（固定 seed）抽 20 条金标，逐条在"对应项目 + 行容差 ±3"内
检查是否有注册规则命中，命中率 ≥80% —— 证明金标与代码真实对齐，而非纸上数字。

坐标系说明（docs/04 §2.1）：
- manual / injected / 克隆金标：行号即项目 HEAD（工作区）坐标，直接对 HEAD 文件跑规则；
- git-history 金标：bench.goldset_mining.mine_git_history 把行区间记录在**修复前
  （父提交）版本**上，因此对账时用 ``git show <fix>~1:<file>`` 取父版本文件内容跑规则。
"""

from __future__ import annotations

import random
import subprocess
from pathlib import Path

from audit.detect.base import RuleContext, RuleHit
from audit.detect.registry import DEFAULT_REGISTRY
from audit.detect.rules._python_common import scan_python

from bench.goldset import GoldenIssue, load_goldset
from bench.matcher import LINE_TOLERANCE

ROOT = Path(__file__).resolve().parents[3]
GOLDSET_PATH = ROOT / "bench" / "datasets" / "goldset.jsonl"
PROJECTS_DIR = ROOT / "bench" / "datasets" / "projects"
DEMO_PROJ = ROOT / "tests" / "samples" / "demo_proj"

SAMPLE_SIZE = 20
SAMPLE_SEED = 20260911
MIN_HIT_RATIO = 0.8  # 门槛：20 条中至少 16 条被规则命中


def _project_dir(name: str) -> Path:
    """项目名 -> 项目根目录（合成/变体在 datasets/projects，demo_proj 在样例目录）。"""
    candidate = PROJECTS_DIR / name
    return candidate if candidate.exists() else DEMO_PROJ


def _rule_hits(rel_path: str, text: str) -> list[RuleHit]:
    """对单文件文本直接执行 DEFAULT_REGISTRY 的全部 Python 规则（不走完整流水线）。"""
    lines = text.splitlines()
    ctx = RuleContext(rel_path=rel_path, language="python", source=text, lines=lines, tree=None, symbols=[])
    ctx.meta["pyscan"] = scan_python(lines)
    hits: list[RuleHit] = []
    for rule in DEFAULT_REGISTRY.rules_for("python"):
        hits.extend(rule.check(ctx))
    return hits


def _parent_version_text(repo: Path, subject: str, rel_path: str) -> str:
    """按 commit subject 定位修复提交，返回其父提交版本的文件内容（git 本地操作）。"""
    log = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.autocrlf=false", "log", "--format=%H%x1f%s"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    ).stdout
    sha = next((h for h, s in (line.split("\x1f", 1) for line in log.splitlines()) if s == subject), None)
    assert sha is not None, f"仓库 {repo.name} 中找不到修复提交：{subject!r}"
    show = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.autocrlf=false", "show", f"{sha}~1:{rel_path}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    return show.stdout


def _resolve_text(golden: GoldenIssue) -> str:
    """返回金标应在其上对账的文件内容（git-history 金标用父提交版本）。"""
    repo = _project_dir(golden.project)
    if golden.origin == "git-history":
        subject, rest = golden.description.removeprefix("[git-history] ").split(" :: ", 1)
        rel_path = rest.split(" L", 1)[0]
        return _parent_version_text(repo, subject, rel_path)
    return (repo / golden.file).read_text(encoding="utf-8")


def _is_hit(golden: GoldenIssue, hits: list[RuleHit]) -> bool:
    """命中判定：任一规则命中与金标行区间相交（±LINE_TOLERANCE 行，同 bench.matcher 口径）。"""
    for hit in hits:
        if hit.line_end + LINE_TOLERANCE >= golden.line_start and hit.line_start - LINE_TOLERANCE <= golden.line_end:
            return True
    return False


def test_sampled_goldens_align_with_rules() -> None:
    """抽样 20 条金标（seed 固定可复现）≥80% 能被 DEFAULT_REGISTRY 规则命中。"""
    goldens = load_goldset(GOLDSET_PATH)
    assert len(goldens) >= 150, "金标集规模不足，先运行 bench/datasets/gen_offline.py"

    # GitHub 分发不含嵌套 .git（git 无法提交嵌套仓库内容）；缺失时跳过 git-history
    # 金标对账，运行 python bench/datasets/gen_offline.py 可完整重建历史
    if not (_project_dir("shopcore") / ".git").exists():
        goldens = [g for g in goldens if g.origin != "git-history"]

    sample = random.Random(SAMPLE_SEED).sample(goldens, SAMPLE_SIZE)
    misses: list[str] = []
    for golden in sample:
        hits = _rule_hits(golden.file, _resolve_text(golden))
        if not _is_hit(golden, hits):
            misses.append(
                f"{golden.project}/{golden.file}:{golden.line_start}-{golden.line_end} "
                f"[{golden.origin}] {golden.description[:70]}"
            )
    hit_ratio = (SAMPLE_SIZE - len(misses)) / SAMPLE_SIZE
    assert hit_ratio >= MIN_HIT_RATIO, (
        f"金标-规则对齐率 {hit_ratio:.0%} < {MIN_HIT_RATIO:.0%}，未命中：\n" + "\n".join(misses)
    )
