"""W28-A CLI diff 子命令 seq 历史版本消费自测（tmp_path 离线，真实 TaskStore 落库零 mock）。

覆盖：--from-seq/--to-seq 正常取历史版本对比 / seq 超界中文报错退出 1 /
缺省不传 seq 行为零变化（走既有 locate_audit 当前报告路径）/ 文件路径给 seq
中文报错。seed 形态参考 tests/unit/taskstore/test_report_history_w27.py
（set_report 两次造两个版本）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import cli
from audit.models import AuditReport, Category, Issue, Severity
from audit.taskstore import TaskStore

AID = "diffseq001"


@pytest.fixture(autouse=True)
def _clean_db_env(monkeypatch: pytest.MonkeyPatch):
    """隔离环境变量：CODEAUDIT_DB_PATH 若在环境中残留会把定位引去别处。"""
    monkeypatch.delenv("CODEAUDIT_DB_PATH", raising=False)


# ---------------------------------------------------------------- seed 工具
def _issue(iid: str, title: str, line: int) -> Issue:
    return Issue(
        id=iid,
        category=Category.BUG,
        severity=Severity.HIGH,
        title=title,
        file="app/core.py",
        line_start=line,
        line_end=line + 2,
        evidence=[f"rule:TEST-{iid}"],  # 规则身份标记（comparator 匹配口径）
    )


def _report(audit_id: str, *, health: float, issues: list[Issue]) -> AuditReport:
    return AuditReport(
        audit_id=audit_id,
        project_name="proj",
        languages={"python": 1.0},
        loc=100,
        health_score=health,
        summary={"high": len(issues)},
        issues=issues,
        created_at="2026-01-01T00:00:00",
    )


def _seed(tmp_path: Path) -> Path:
    """seed 两个报告版本：seq=1 只有旧问题 A；seq=2 修掉 A、新增问题 B。返回 work_root。"""
    work_root = tmp_path / "wr"
    store = TaskStore(work_root / "audits.db", work_root=work_root)
    try:
        store.create(
            AID,
            status="done",
            error=None,
            created_at="2026-01-01T00:00:00",
            source_path=str(tmp_path),
            do_fix=False,
            do_tests=False,
            config_json="{}",
        )
        store.set_report(AID, _report(AID, health=80.0, issues=[_issue("A", "旧问题", 10)]))
        store.set_report(AID, _report(AID, health=70.0, issues=[_issue("B", "新问题", 50)]))
    finally:
        store.close()
    return work_root


def _read_version(work_root: Path, seq: int) -> dict:
    store = TaskStore(work_root / "audits.db", work_root=work_root)
    try:
        data = store.get_report(AID, seq=seq)
    finally:
        store.close()
    assert isinstance(data, dict)
    return data


# ---------------------------------------------------------------- seq 正常路径
def test_diff_from_seq_to_seq_compares_history_versions(tmp_path: Path, capsys):
    work_root = _seed(tmp_path)

    rc = cli.main(
        ["diff", AID, AID, "--work-root", str(work_root), "--from-seq", "1", "--to-seq", "2"]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "已修复（A 有 B 无）：1 个" in out  # seq1 的旧问题在 seq2 被修掉
    assert "新增（B 有 A 无）：1 个" in out  # seq2 的新问题
    assert "#seq=1" in out and "#seq=2" in out  # 定位标注历史版本号


def test_diff_from_seq_vs_current_mixed(tmp_path: Path, capsys):
    """单侧 seq：A 取历史 seq=1，B 缺省取当前报告（seq2 同源的最新版）。"""
    work_root = _seed(tmp_path)

    rc = cli.main(["diff", AID, AID, "--work-root", str(work_root), "--from-seq", "1"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "#seq=1" in out
    assert "已修复（A 有 B 无）：1 个" in out  # 历史旧问题在当前版已被修掉
    assert "新增（B 有 A 无）：1 个" in out  # 当前版的新问题


# ---------------------------------------------------------------- 错误路径
def test_diff_seq_out_of_range_chinese_error_exit_1(tmp_path: Path, capsys):
    work_root = _seed(tmp_path)

    rc = cli.main(
        ["diff", AID, AID, "--work-root", str(work_root), "--from-seq", "9", "--to-seq", "2"]
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "历史版本 seq=9" in err and "越界" in err


def test_diff_file_path_with_seq_rejected(tmp_path: Path, capsys):
    """report.json 文件路径目标不适用 seq（给了就中文报错退出 1）。"""
    work_root = _seed(tmp_path)
    report_json = tmp_path / "report.json"
    report_json.write_text(
        json.dumps(_read_version(work_root, 1), ensure_ascii=False), encoding="utf-8"
    )

    rc = cli.main(
        ["diff", str(report_json), AID, "--work-root", str(work_root), "--from-seq", "1"]
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "文件路径" in err and "seq" in err


# ---------------------------------------------------------------- 缺省零变化
def test_diff_default_without_seq_unchanged(tmp_path: Path, capsys):
    """不传 seq：走既有 locate_audit 当前报告路径，定位标注与对比口径零变化。"""
    work_root = _seed(tmp_path)

    rc = cli.main(["diff", AID, AID, "--work-root", str(work_root)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "seq=" not in out  # 缺省定位标注不带历史版本号
    assert "已修复（A 有 B 无）：0 个" in out  # 当前 vs 当前：无差异
    assert "仍存在（两侧同现）：1 个" in out  # 当前版问题两侧同现
