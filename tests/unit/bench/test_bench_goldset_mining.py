"""bench.goldset_mining 单元测试（docs/04 §2.1）：git 历史回溯挖矿 + 规则缺陷注入。

- mine_git_history：临时本地 git 仓库（subprocess list 参数调用 git CLI，
  禁 shell=True），init → 提交缺陷文件 → "fix: ..." 二次提交 → 挖出金标；
- inject_defects：demo_proj 注入到 tmp，注入金标与 DEFAULT_REGISTRY 规则命中
  一致率 ≥ 90%，且源项目文件逐字节未变；全程零网络零真实 LLM。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from audit.config import AuditConfig
from audit.detect.engine import hits_to_issues, run_rules
from audit.llm.base import FakeLLMClient
from audit.pipeline import PipelineContext
from audit.workspace import WorkspaceContext

from bench.goldset import load_goldset
from bench.goldset_mining import inject_defects, mine_git_history
from bench.matcher import matches

ROOT = Path(__file__).resolve().parents[3]
DEMO_PROJ = ROOT / "tests" / "samples" / "demo_proj"


def _run_git(repo: Path, *args: str) -> None:
    """在 repo 中执行 git 子命令（list 参数，无 shell），失败即测试失败。"""
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@pytest.fixture
def git_repo_with_fix(tmp_path: Path) -> Path:
    """临时 git 仓库：init + user 配置 → 提交缺陷文件 → "fix: ..." 二次提交。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init")
    _run_git(repo, "config", "user.email", "bench@example.com")
    _run_git(repo, "config", "user.name", "bench")
    (repo / "buggy.py").write_text(
        "def total(prices):\n    return sum(prices) / 0\n", encoding="utf-8"
    )
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-m", "init project")
    (repo / "buggy.py").write_text(
        "def total(prices):\n"
        "    if not prices:\n"
        "        return 0\n"
        "    return sum(prices) / len(prices)\n",
        encoding="utf-8",
    )
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-m", "fix: division by zero when prices is empty")
    return repo


# ---------------------------------------------------------------- git 历史回溯


def test_mine_git_history_finds_golden_with_correct_span(
    git_repo_with_fix: Path, tmp_path: Path
) -> None:
    """挖矿产出 ≥1 条 GoldenIssue，file/行区间与修复 diff 反推一致。"""
    out_jsonl = tmp_path / "goldset.jsonl"
    goldens = mine_git_history(git_repo_with_fix, out_jsonl, max_commits=10, project="repo")
    assert len(goldens) >= 1
    golden = goldens[0]
    assert golden.project == "repo"
    assert golden.file == "buggy.py"
    # 行区间记录在修复前（父提交）坐标：被移除的缺陷行是旧文件第 2 行
    assert (golden.line_start, golden.line_end) == (2, 2)
    assert golden.category == "bug"
    assert golden.severity == "high"
    assert golden.origin == "git-history"
    assert "fix" in golden.description
    # 挖矿结果已写 JSONL 且可无损读回
    assert load_goldset(out_jsonl) == goldens


def test_mine_git_history_non_git_returns_empty(tmp_path: Path) -> None:
    """非 git 目录安全降级：返回空列表、不写文件、不抛异常。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("x = 1\n", encoding="utf-8")
    out_jsonl = tmp_path / "g.jsonl"
    assert mine_git_history(plain, out_jsonl, max_commits=10) == []
    assert not out_jsonl.exists()


# ---------------------------------------------------------------- 缺陷注入


def _rule_issues_for(project_root: Path, tmp_path: Path) -> list[Any]:
    """对 project_root 跑 DEFAULT_REGISTRY 全量规则并转换为 Issue 列表（离线）。"""

    async def _emit(event: dict[str, Any]) -> None:
        return None

    workspace = WorkspaceContext(
        audit_id="bench001",
        src_root=project_root,
        work_root=tmp_path,
        db_path=tmp_path / "idx.db",
    )
    ctx = PipelineContext(
        config=AuditConfig(source_path=str(project_root)),
        workspace=workspace,
        llm=FakeLLMClient(),
        emitter=_emit,
    )
    return hits_to_issues(run_rules(ctx))


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def test_inject_defects_lines_match_rules_and_source_untouched(tmp_path: Path) -> None:
    """注入金标行号与规则命中一致率 ≥ 90%，且 demo_proj 源文件逐字节未变。"""
    before = _snapshot(DEMO_PROJ)
    out_project = tmp_path / "injected"
    goldens = inject_defects(DEMO_PROJ, out_project, n=6, seed=0, per_file=4)
    assert len(goldens) >= 20  # 6 文件 × 4 模板
    assert all(g.origin == "injected" for g in goldens)
    assert all(g.project == "injected" for g in goldens)
    # 注入只允许追加到副本：源项目任何文件（含 GOLDEN_ISSUES.md）逐字节未变
    assert _snapshot(DEMO_PROJ) == before
    # 副本确实被注入（与源不同），且金标行号落在副本文件行数范围内
    assert _snapshot(out_project) != before

    issues = _rule_issues_for(out_project, tmp_path)
    assert issues, "DEFAULT_REGISTRY 应至少命中部分注入缺陷"
    matched = sum(1 for g in goldens if any(matches(issue, g) for issue in issues))
    consistency = matched / len(goldens)
    assert consistency >= 0.9, f"金标-规则命中一致率 {consistency:.3f} < 0.9"


def test_inject_defects_rejects_same_dir(tmp_path: Path) -> None:
    """源/输出同目录直接拒绝（防止原地改写源项目）。"""
    with pytest.raises(ValueError):
        inject_defects(tmp_path, tmp_path)


def test_inject_defects_deterministic_with_same_seed(tmp_path: Path) -> None:
    """同 seed + 同输出目录名，两次注入产出完全一致的金标（可复现）。"""
    g1 = inject_defects(DEMO_PROJ, tmp_path / "a" / "injected", n=3, seed=7, per_file=2)
    g2 = inject_defects(DEMO_PROJ, tmp_path / "b" / "injected", n=3, seed=7, per_file=2)
    assert g1 == g2
