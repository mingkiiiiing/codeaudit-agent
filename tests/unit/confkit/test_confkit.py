"""confkit（基线与 diff 增量助手）单测：全部离线，git 用真实临时仓库。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from audit.confkit import (
    BASELINE_SCHEMA_VERSION,
    apply_baseline,
    apply_diff_filter,
    changed_files,
    load_baseline,
    write_baseline,
)
from audit.models import Category, Issue, Severity
from audit.utils import issue_fingerprint


def make_issue(**kw) -> Issue:
    """构造带稳定字段的 Issue（指纹由 file/行区间/category/title 决定）。"""
    base = dict(
        id="ISS-0001",
        category=Category.SECURITY,
        severity=Severity.CRITICAL,
        title="SQL 语句字符串拼接",
        file="app/services/orders.py",
        line_start=11,
        line_end=11,
        confidence=0.7,
    )
    base.update(kw)
    return Issue(**base)


def run_git(root: Path, *args: str) -> None:
    """在临时仓库执行 git 命令（list 参数，check=True）。"""
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def make_git_repo(root: Path) -> None:
    """建一个两文件临时 git 仓库并完成首次提交（含嵌套路径，验证 posix 归一）。"""
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "b.py").write_text("y = 2\n", encoding="utf-8")
    run_git(root, "init")
    run_git(root, "config", "user.email", "audit@example.com")
    run_git(root, "config", "user.name", "auditor")
    run_git(root, "add", ".")
    run_git(root, "commit", "-m", "init")


# ---------------------------------------------------------------- 基线写读回环


class TestWriteLoadBaseline:
    def test_roundtrip(self, tmp_path: Path):
        issues = [
            make_issue(),
            make_issue(id="ISS-0002", file="b.py", line_start=3, category=Category.BUG, title="裸 except"),
        ]
        out = tmp_path / "nested" / "baseline.json"
        assert write_baseline(issues, out) == out
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["schema_version"] == BASELINE_SCHEMA_VERSION == 1
        assert data["created_at"]
        assert data["fingerprints"] == [issue_fingerprint(i) for i in issues]  # 顺序与 issues 一致
        assert load_baseline(out) == {data["fingerprints"][0], data["fingerprints"][1]}

    def test_write_with_note(self, tmp_path: Path):
        out = tmp_path / "baseline.json"
        write_baseline([make_issue()], out, note="首次全量审计")
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["note"] == "首次全量审计"

    def test_load_missing_returns_empty(self, tmp_path: Path):
        assert load_baseline(tmp_path / "nope.json") == set()

    def test_load_bad_json_returns_empty(self, tmp_path: Path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        assert load_baseline(bad) == set()

    def test_load_wrong_shape_returns_empty(self, tmp_path: Path):
        wrong = tmp_path / "wrong.json"
        wrong.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        assert load_baseline(wrong) == set()
        no_field = tmp_path / "nofield.json"
        no_field.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        assert load_baseline(no_field) == set()


# ---------------------------------------------------------------- apply_baseline


class TestApplyBaseline:
    def test_hit_and_miss_counting(self):
        a, b, c = make_issue(), make_issue(file="b.py"), make_issue(file="c.py")
        kept, suppressed = apply_baseline([a, b, c], {issue_fingerprint(a)})
        assert suppressed == 1
        assert kept == [b, c]

    def test_empty_baseline_suppresses_nothing(self):
        issues = [make_issue(), make_issue(file="b.py")]
        kept, suppressed = apply_baseline(issues, set())
        assert suppressed == 0
        assert kept == issues

    def test_all_hit(self):
        issues = [make_issue(), make_issue(file="b.py")]
        kept, suppressed = apply_baseline(issues, {issue_fingerprint(i) for i in issues})
        assert suppressed == 2
        assert kept == []


# ---------------------------------------------------------------- changed_files


class TestChangedFiles:
    def test_git_repo_returns_changed_relative_posix(self, tmp_path: Path):
        root = tmp_path / "repo"
        make_git_repo(root)
        assert changed_files(root, "HEAD") == set()  # 刚提交：无变更
        (root / "pkg" / "a.py").write_text("x = 2\n", encoding="utf-8")
        assert changed_files(root, "HEAD") == {"pkg/a.py"}
        # 第二次提交后再改 b.py：相对 HEAD~1 的集合包含两个变更文件
        run_git(root, "add", ".")
        run_git(root, "commit", "-m", "update")
        (root / "b.py").write_text("y = 20\n", encoding="utf-8")
        assert changed_files(root, "HEAD") == {"b.py"}
        assert changed_files(root, "HEAD~1") == {"pkg/a.py", "b.py"}

    def test_untracked_new_file_not_in_diff(self, tmp_path: Path):
        root = tmp_path / "repo"
        make_git_repo(root)
        (root / "new.py").write_text("z = 3\n", encoding="utf-8")
        assert changed_files(root, "HEAD") == set()  # 未跟踪文件不入 diff

    def test_non_git_dir_returns_none(self, tmp_path: Path):
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "a.py").write_text("x = 1\n", encoding="utf-8")
        assert changed_files(plain, "HEAD") is None

    def test_bad_ref_returns_none(self, tmp_path: Path):
        root = tmp_path / "repo"
        make_git_repo(root)
        assert changed_files(root, "no-such-ref") is None

    def test_empty_ref_returns_none(self, tmp_path: Path):
        root = tmp_path / "repo"
        make_git_repo(root)
        assert changed_files(root, "") is None


# ---------------------------------------------------------------- apply_diff_filter


class TestApplyDiffFilter:
    @pytest.fixture
    def workspace(self, tmp_path: Path):
        from audit.ingest import ingest

        proj = tmp_path / "proj"
        (proj / "sub").mkdir(parents=True)
        (proj / "a.py").write_text("a = 1\n", encoding="utf-8")
        (proj / "b.py").write_text("b = 1\n", encoding="utf-8")
        (proj / "sub" / "c.py").write_text("c = 1\n", encoding="utf-8")
        return ingest(proj, tmp_path / "work")

    def test_prunes_to_changed_files_and_syncs_manifests(self, workspace, tmp_path: Path):
        kept = apply_diff_filter(workspace, {"a.py"})
        assert kept == 1
        assert [p.name for p in workspace.source_files()] == ["a.py"]
        assert [m.path for m in workspace.manifests] == ["a.py"]
        assert (workspace.src_root / "b.py").exists() is False
        assert (workspace.src_root / "sub").exists() is False  # 空目录被清理

    def test_keeps_nested_changed_files(self, workspace):
        kept = apply_diff_filter(workspace, {"a.py", "sub/c.py"})
        assert kept == 2
        assert {m.path for m in workspace.manifests} == {"a.py", "sub/c.py"}
        assert (workspace.src_root / "sub" / "c.py").exists()

    def test_no_intersection_keeps_everything(self, workspace):
        kept = apply_diff_filter(workspace, {"zzz.py"})
        assert kept == 0
        assert len(workspace.manifests) == 3  # 未做任何删除
        assert len(list(workspace.source_files())) == 3

    def test_empty_changed_set_is_noop(self, workspace):
        assert apply_diff_filter(workspace, set()) == 0
        assert len(workspace.manifests) == 3

    def test_windows_style_paths_matched(self, workspace):
        kept = apply_diff_filter(workspace, {"sub\\c.py"})  # 容忍反斜杠写法
        assert kept == 1
        assert {m.path for m in workspace.manifests} == {"sub/c.py"}
