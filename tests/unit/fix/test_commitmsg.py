"""P0-8 commit message / PR 描述生成单测：纯字符串拼接，零 LLM token，全离线。

覆盖：单补丁模板 / 多补丁 (category, rule_id) 去重 / issue 反查失败降级 /
空补丁集 / needs-review 与 failed 状态如实呈现 / PR 描述验证统计 /
compat_notes 有无两态 / rationale 首行 72 字符截断 / 无规则证据回退
unknown-rule / CLI apply --commit-message / --pr-description 开关。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from audit.fix.commitmsg import build_commit_message, build_pr_description
from audit.models import AuditReport, Category, Issue, Patch
import cli

AID = "commitmsg1"

APP_PY = """import logging

logger = logging.getLogger(__name__)


def divide(a, b):
    try:
        return a / b
    except:
        return None
"""

BARE_EXCEPT_DIFF = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -6,5 +6,6 @@\n"
    " def divide(a, b):\n"
    "     try:\n"
    "         return a / b\n"
    "-    except:\n"
    "+    except Exception:\n"
    '+        logger.exception("divide failed")\n'
    "         return None\n"
)


def _issue(
    iid: str,
    *,
    category: Category = Category.BUG,
    title: str = "修复裸 except",
    rule: str = "PY-001",
) -> Issue:
    """规则命中样例 Issue：evidence 带 ``rule:<rule>`` 证据行（detect 引擎约定）。"""
    return Issue(id=iid, category=category, title=title, evidence=[f"rule:{rule}"])


def _patch(
    pid: str,
    issue_id: str,
    *,
    rationale: str = "测试补丁",
    status: str = "verified",
    tests_run: int = 0,
    tests_passed: int = 0,
    compat_notes: list[str] | None = None,
) -> Patch:
    return Patch(
        id=pid,
        issue_id=issue_id,
        diff=BARE_EXCEPT_DIFF,
        rationale=rationale,
        apply_status=status,
        tests_run=tests_run,
        tests_passed=tests_passed,
        compat_notes=compat_notes or [],
    )


# ---------------------------------------------------------------- commit message


def test_commit_message_single_patch_template():
    msg = build_commit_message([_patch("PATCH-0001", "ISS-1")], [_issue("ISS-1")])
    assert msg == "fix(bug): 修复裸 except (PY-001)"


def test_commit_message_multi_patch_dedupe_by_category_and_rule():
    # 两个补丁反查到同 (category, rule_id) 的不同 Issue → 去重为一行（先到先得）
    issues = [
        _issue("ISS-1", title="标题甲"),
        _issue("ISS-2", title="标题乙"),
        _issue("ISS-3", category=Category.SECURITY, rule="PY-007", title="标题丙"),
    ]
    patches = [
        _patch("P1", "ISS-1"),
        _patch("P2", "ISS-2"),
        _patch("P3", "ISS-3"),
    ]
    msg = build_commit_message(patches, issues)
    assert msg == "fix(bug): 标题甲 (PY-001)\nfix(security): 标题丙 (PY-007)"


def test_commit_message_unknown_issue_falls_back_to_rationale():
    # issue_id 反查不到 → 诚实降级：general / rationale 首行 / unknown-rule
    patch = _patch("PATCH-0001", "ISS-GONE", rationale="修复空指针风险\n第二行不取")
    msg = build_commit_message([patch], [])
    assert msg == "fix(general): 修复空指针风险 (unknown-rule)"


def test_commit_message_empty_patch_set():
    assert build_commit_message([], [_issue("ISS-1")]) == ""


def test_commit_message_truncates_rationale_first_line_to_72_chars():
    patch = _patch("P1", "ISS-GONE", rationale="x" * 80 + "\n第二行")
    msg = build_commit_message([patch], [])
    assert msg == f"fix(general): {'x' * 72} (unknown-rule)"


def test_commit_message_issue_without_rule_evidence_uses_unknown_rule():
    # Issue 存在但 evidence 无 rule: 证据行（纯 LLM 问题）→ category 保留，规则回退
    issue = Issue(id="ISS-LLM", category=Category.STYLE, title="命名不规范", evidence=[])
    msg = build_commit_message([_patch("P1", "ISS-LLM")], [issue])
    assert msg == "fix(style): 命名不规范 (unknown-rule)"


# ---------------------------------------------------------------- PR 描述


def test_pr_description_reports_needs_review_honestly():
    patch = _patch("P1", "ISS-1", status="needs-review")
    desc = build_pr_description([patch], [_issue("ISS-1")])
    assert "needs-review（需人工复核）" in desc  # 原始状态值如实呈现
    assert "| 0 |" in desc  # 逐补丁状态表


def test_pr_description_reports_failed_honestly():
    patch = _patch("P1", "ISS-1", status="failed", tests_run=2, tests_passed=1)
    desc = build_pr_description([patch], [_issue("ISS-1")])
    assert "failed（验证失败）" in desc
    assert "1/2" in desc  # 测试计数如实（1 过 / 2 跑）


def test_pr_description_contains_summary_counts_and_rule_list():
    issues = [
        _issue("ISS-1", title="标题甲"),
        _issue("ISS-2", title="标题乙"),
        _issue("ISS-3", category=Category.PERFORMANCE, rule="PY-009", title="标题丙"),
    ]
    patches = [_patch("P1", "ISS-1"), _patch("P2", "ISS-2"), _patch("P3", "ISS-3")]
    desc = build_pr_description(patches, issues)
    assert "## 变更摘要" in desc
    assert "共 3 个补丁，命中 2 条规则" in desc
    assert "- PY-001 × 2" in desc
    assert "## 涉及规则" in desc
    assert "- `PY-001`（bug）标题甲" in desc
    assert "- `PY-009`（performance）标题丙" in desc


def test_pr_description_verification_stats_and_untested_marker():
    patches = [
        _patch("P1", "ISS-1", status="verified", tests_run=3, tests_passed=3),
        _patch("P2", "ISS-2", status="syntax-ok"),
    ]
    issues = [_issue("ISS-1"), _issue("ISS-2", rule="PY-002")]
    desc = build_pr_description(patches, issues)
    assert "3/3" in desc
    assert "未运行" in desc  # tests_run=0 不虚报
    assert "syntax-ok（仅语法校验通过）" in desc


def test_pr_description_compat_notes_present_when_non_empty():
    patch = _patch(
        "P1", "ISS-1", compat_notes=["移除函数 foo", "变更签名 bar(a,b) -> bar(a,b,c)"]
    )
    desc = build_pr_description([patch], [_issue("ISS-1")])
    assert "## 兼容性说明" in desc
    assert "- `P1`：移除函数 foo" in desc
    assert "- `P1`：变更签名 bar(a,b) -> bar(a,b,c)" in desc


def test_pr_description_omits_compat_section_when_empty():
    desc = build_pr_description([_patch("P1", "ISS-1")], [_issue("ISS-1")])
    assert "兼容性说明" not in desc


def test_pr_description_empty_patch_set():
    assert build_pr_description([], [_issue("ISS-1")]) == ""


# ---------------------------------------------------------------- CLI 开关（端到端）


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _seed_audit(tmp_path: Path, patches: list[Patch], issues: list[Issue]) -> tuple[Path, Path]:
    """注入 <work-root>/<audit_id>/reports/report.json 与目标源码目录（server 布局）。"""
    report = AuditReport(audit_id=AID, project_name="proj", patches=patches, issues=issues)
    report_dir = tmp_path / "wr" / AID / "reports"
    report_dir.mkdir(parents=True)
    (report_dir / "report.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    proj = tmp_path / "proj"
    proj.mkdir(parents=True)
    (proj / "app.py").write_text(APP_PY, encoding="utf-8", newline="\n")
    return tmp_path / "wr", proj


def test_cli_apply_commit_message_flag_dry_run_and_yes(tmp_path: Path, capsys):
    patch = _patch("PATCH-0001", "ISS-1")
    patch.target_sha256 = {"app.py": _sha(APP_PY)}
    work_root, proj = _seed_audit(tmp_path, [patch], [_issue("ISS-1")])
    base = ["apply", AID, "--work-root", str(work_root), "--workdir", str(proj)]

    # dry-run：文案带（预览）标注，不落盘
    rc = cli.main([*base, "--commit-message", "--pr-description"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "========== 建议 commit message（预览） ==========" in out
    assert "fix(bug): 修复裸 except (PY-001)" in out
    assert "========== 建议 PR 描述（预览） ==========" in out
    assert (proj / "app.py").read_text(encoding="utf-8") == APP_PY

    # --yes：无（预览）标注，退出码语义不变（成功 0）
    rc = cli.main([*base, "--commit-message", "--yes"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "========== 建议 commit message ==========" in out
    assert "（预览）" not in out.split("建议 commit message", 1)[1]


def test_cli_apply_without_flags_keeps_legacy_output(tmp_path: Path, capsys):
    # 不加新开关：输出零变化（无建议文案段），既有行为不受影响
    patch = _patch("PATCH-0001", "ISS-1")
    patch.target_sha256 = {"app.py": _sha(APP_PY)}
    work_root, proj = _seed_audit(tmp_path, [patch], [_issue("ISS-1")])

    rc = cli.main(["apply", AID, "--work-root", str(work_root), "--workdir", str(proj)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "建议 commit message" not in out
    assert "建议 PR 描述" not in out
    assert "dry-run" in out and "预检通过" in out
