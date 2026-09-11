"""pipeline 接线测试（W4-A2）：diff 增量审计、基线导出与基线抑制，全部离线（真实临时 git 仓库）。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from audit.config import AuditConfig
from audit.orchestrator.pipeline import run_audit

# 两个各含已知规则缺陷的源文件（裸 except / 可变默认参数，均被内置规则命中）
_A_PY = """import os


def remove_quietly(path):
    try:
        os.remove(path)
    except:
        pass
    return True
"""

_B_PY = """def collect(items, bucket=[]):
    bucket.extend(items)
    return bucket
"""


def _collector() -> tuple[list[dict[str, Any]], Any]:
    """独立事件收集器（同一用例多次审计时互不污染）。"""
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    return events, emit


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _make_git_project(root: Path) -> None:
    """建两文件临时 git 仓库并完成首次提交。

    用 --separate-git-dir 把真实 git 目录放在项目树之外：Windows 上刚建好的
    .git 目录可能被安全软件/句柄短暂占用，ingest 的 copytree 偶发 PermissionError；
    项目内只留一个 .git 指针文件即可让 git diff 正常工作且复制安全。
    """
    root.mkdir(parents=True)
    git_dir = root.parent / (root.name + ".gitdir")
    git_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--separate-git-dir", str(git_dir), str(root)],
        check=True,
        capture_output=True,
    )
    (root / "a.py").write_text(_A_PY, encoding="utf-8")
    (root / "b.py").write_text(_B_PY, encoding="utf-8")
    _git(root, "config", "user.email", "audit@example.com")
    _git(root, "config", "user.name", "auditor")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")


def _config(tmp_path: Path, root: Path, **kw) -> AuditConfig:
    base = dict(
        source_path=str(root),
        work_root=str(tmp_path / "work"),
        out_dir=str(tmp_path / "out"),
        enable_llm_review=False,  # 纯规则模式：离线且命中来源可精确断言
    )
    base.update(kw)
    return AuditConfig(**base)


# ---------------------------------------------------------------- diff 增量


class TestDiffIncrement:
    async def test_diff_ref_audits_only_changed_file(self, tmp_path: Path):
        root = tmp_path / "proj"
        _make_git_project(root)

        # 首次全量审计：两个文件都出问题
        first_events, first_emit = _collector()
        first = await run_audit(_config(tmp_path, root), first_emit)
        assert {"a.py", "b.py"} <= {i.file for i in first.issues}

        # 提交后只改 a.py → diff_ref="HEAD" 只审计 1 个变更文件
        (root / "a.py").write_text(_A_PY + "\n# touched\n", encoding="utf-8")
        events, emit = _collector()
        report = await run_audit(_config(tmp_path, root, diff_ref="HEAD"), emit)

        assert any("增量模式：仅审计 1 个变更文件" in e["message"] for e in events)
        issue_files = {i.file for i in report.issues}
        assert issue_files == {"a.py"}  # 规则命中只来自变更文件
        assert any(
            "rule:PY-BARE-EXCEPT" in i.evidence for i in report.issues
        )  # a.py 的裸 except 缺陷仍被检出

    async def test_diff_ref_on_zip_input_warns_and_falls_back_full(self, tmp_path: Path):
        root = tmp_path / "proj"
        _make_git_project(root)
        zip_path = shutil.make_archive(str(tmp_path / "proj"), "zip", root_dir=root)

        events, emit = _collector()
        report = await run_audit(_config(tmp_path, Path(zip_path), work_root=str(tmp_path / "work2"), out_dir=str(tmp_path / "out2"), diff_ref="HEAD"), emit)

        warnings = [e for e in events if e.get("warning")]
        assert any("diff 增量仅支持本地 git 目录输入" in e["message"] for e in warnings)
        # 回退全量：两个文件的问题都在
        assert {"a.py", "b.py"} <= {i.file for i in report.issues}

    async def test_diff_ref_on_non_git_dir_warns_and_falls_back_full(self, tmp_path: Path):
        root = tmp_path / "plain"
        root.mkdir()
        (root / "a.py").write_text(_A_PY, encoding="utf-8")
        (root / "b.py").write_text(_B_PY, encoding="utf-8")

        events, emit = _collector()
        report = await run_audit(_config(tmp_path, root, diff_ref="HEAD"), emit)

        warnings = [e for e in events if e.get("warning")]
        assert any("diff 增量仅支持本地 git 目录输入" in e["message"] for e in warnings)
        assert {"a.py", "b.py"} <= {i.file for i in report.issues}


# ---------------------------------------------------------------- 基线导出与抑制


class TestBaselineWiring:
    async def test_report_baseline_out_writes_file_with_issue_count(self, tmp_path: Path):
        root = tmp_path / "proj"
        _make_git_project(root)
        baseline = tmp_path / "baseline.json"

        events, emit = _collector()
        report = await run_audit(_config(tmp_path, root, report_baseline_out=str(baseline)), emit)

        assert baseline.is_file()
        data = json.loads(baseline.read_text(encoding="utf-8"))
        assert data["schema_version"] == 1
        assert data["created_at"]
        assert len(data["fingerprints"]) == len(report.issues)  # 指纹数 == 问题数
        assert any(
            e["stage"] == "report" and "基线已写入" in e["message"] for e in events
        )
        # 既有 report 事件文案未被破坏
        assert any(e["stage"] == "report" and "报告已生成" in e["message"] for e in events)

    async def test_baseline_suppresses_known_issues(self, tmp_path: Path):
        root = tmp_path / "proj"
        _make_git_project(root)
        baseline = tmp_path / "baseline.json"

        first_events, first_emit = _collector()
        first = await run_audit(_config(tmp_path, root, report_baseline_out=str(baseline)), first_emit)
        assert first.issues

        # 第二次审计携带基线：已知问题全部被抑制
        events, emit = _collector()
        second = await run_audit(_config(tmp_path, root, baseline_path=str(baseline)), emit)

        assert second.stats.suppressed > 0
        assert second.stats.suppressed == len(first.issues)
        assert second.issues == []  # 内容未变 → 全部命中基线
        assert sum(second.summary.values()) < sum(first.summary.values())  # summary 计数下降
        assert any(e["stage"] == "detect" and "基线抑制：" in e["message"] for e in events)

    async def test_baseline_partial_suppress_keeps_new_issues(self, tmp_path: Path):
        root = tmp_path / "proj"
        _make_git_project(root)
        baseline = tmp_path / "baseline.json"

        first_events, first_emit = _collector()
        first = await run_audit(_config(tmp_path, root, report_baseline_out=str(baseline)), first_emit)

        # 新增一个新缺陷文件：旧问题被抑制、新问题保留
        (root / "c.py").write_text("def h(x, acc=[]):\n    acc.append(x)\n    return acc\n", encoding="utf-8")
        events, emit = _collector()
        second = await run_audit(_config(tmp_path, root, baseline_path=str(baseline)), emit)

        assert second.stats.suppressed == len(first.issues)
        assert {i.file for i in second.issues} == {"c.py"}
        assert any(e["stage"] == "detect" and "基线抑制：" in e["message"] for e in events)
