"""TaskStore 报告历史版本化（W27-B）单元测试：双写、seq 原子递增、查询面、老库迁移。

P0 存量项「结果版本化」收口：set_report 双写 report_history 后，resume/重跑的旧
报告可追溯（seq 每任务从 1 递增），既有读路径（get_report 单参返回 AuditReport）
零变化一并回归。全部用例经 tmp_path 隔离，离线运行。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from audit.models import AuditReport, Category, Issue, Severity
from audit.taskstore import TaskStore


# ---------------------------------------------------------------- 夹具与工具
def _make_store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "audits.db")


def _task_kwargs(**overrides: Any) -> dict[str, Any]:
    """create() 的默认关键字参数（除 audit_id 外全量给出，便于覆写单字段）。"""
    kw: dict[str, Any] = {
        "status": "queued",
        "error": None,
        "created_at": "2026-01-01T00:00:00",
        "source_path": "/tmp/proj",
        "do_fix": False,
        "do_tests": False,
        "config_json": "{}",
    }
    kw.update(overrides)
    return kw


def _report(audit_id: str, *, health: float, title: str) -> AuditReport:
    """最小合法报告：含 1 个 Issue，title 可区分版本内容。"""
    return AuditReport(
        audit_id=audit_id,
        project_name="proj",
        languages={"python": 1.0},
        loc=100,
        health_score=health,
        summary={"high": 1},
        issues=[
            Issue(
                id="i1",
                category=Category.BUG,
                severity=Severity.HIGH,
                title=title,
                file="app/services/users.py",
                line_start=10,
                line_end=12,
            )
        ],
        created_at="2026-01-01T00:00:00",
    )


# ---------------------------------------------------------------- 双写与 seq 递增
def test_set_report_twice_yields_two_versions_seq_1_2(tmp_path: Path):
    """核心双写：set_report 两次 → 历史 2 条且 seq 1/2；seq=1 为第一版内容；
    既有读路径零变化——当前 get_report(audit_id) 仍返回最新版 AuditReport 对象。"""
    store = _make_store(tmp_path)
    store.create("a1", **_task_kwargs())
    store.set_report("a1", _report("a1", health=88.5, title="第一版问题"))
    store.set_report("a1", _report("a1", health=70.0, title="第二版问题"))

    versions = store.list_reports("a1")
    assert [v["seq"] for v in versions] == [1, 2]
    v1 = store.get_report("a1", seq=1)
    v2 = store.get_report("a1", seq=2)
    assert v1 is not None and v2 is not None
    assert v1["health_score"] == 88.5
    assert v1["issues"][0]["title"] == "第一版问题"
    assert v2["health_score"] == 70.0
    assert v2["issues"][0]["title"] == "第二版问题"

    # 既有读路径零变化：不带 seq 仍返回 AuditReport，且内容 = 最新版（历史最大 seq）
    current = store.get_report("a1")
    assert isinstance(current, AuditReport)
    assert current.health_score == 70.0
    assert current.issues[0].title == "第二版问题"
    assert current.to_dict() == v2


def test_history_is_append_only_first_version_immutable(tmp_path: Path):
    """历史表只增不改不删：再次 set_report 后 seq=1 原样保留，不被覆盖。"""
    store = _make_store(tmp_path)
    store.create("a2", **_task_kwargs())
    store.set_report("a2", _report("a2", health=88.5, title="第一版问题"))
    v1_before = store.get_report("a2", seq=1)
    store.set_report("a2", _report("a2", health=60.0, title="第二版问题"))
    assert store.get_report("a2", seq=1) == v1_before
    assert [v["seq"] for v in store.list_reports("a2")] == [1, 2]


def test_set_report_on_missing_task_is_silent_and_writes_no_history(tmp_path: Path):
    """既有幽灵静默语义保持：任务行不存在时 set_report 不抛错，历史同样不写。"""
    store = _make_store(tmp_path)
    store.set_report("ghost", _report("ghost", health=50.0, title="不应落库"))
    assert store.get_report("ghost") is None
    assert store.list_reports("ghost") == []


def test_concurrent_set_report_allocates_unique_contiguous_seq(tmp_path: Path):
    """seq 原子性（W15-B3 同形态）：多线程并发 set_report，seq 无重号无断号。"""
    store = _make_store(tmp_path)
    store.create("conc", **_task_kwargs())
    barrier = threading.Barrier(8)

    def _worker(k: int) -> None:
        barrier.wait()  # 同时起跑，最大化争用
        for i in range(3):
            store.set_report("conc", _report("conc", health=50.0 + k + i, title=f"t{k}-{i}"))

    threads = [threading.Thread(target=_worker, args=(k,)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert [v["seq"] for v in store.list_reports("conc")] == list(range(1, 25))


# ---------------------------------------------------------------- 查询面
def test_list_reports_summary_fields_without_full_json(tmp_path: Path):
    """摘要面：恰好 seq/created_at/health_score/issue_count 四键，不含报告全文。"""
    store = _make_store(tmp_path)
    store.create("a3", **_task_kwargs())
    store.set_report("a3", _report("a3", health=66.0, title="x"))
    (summary,) = store.list_reports("a3")
    assert set(summary.keys()) == {"seq", "created_at", "health_score", "issue_count"}
    assert summary["seq"] == 1
    assert summary["health_score"] == 66.0
    assert summary["issue_count"] == 1
    assert isinstance(summary["created_at"], str) and summary["created_at"]


def test_list_reports_empty_when_missing_or_no_version(tmp_path: Path):
    """任务不存在 / 存在但未落报告：list_reports 返回空列表。"""
    store = _make_store(tmp_path)
    assert store.list_reports("ghost") == []
    store.create("fresh", **_task_kwargs())
    assert store.list_reports("fresh") == []


def test_get_report_version_none_for_missing_task_or_seq(tmp_path: Path):
    """版本读取：任务不存在 / 未落报告 / seq 越界（含 0 与超大值）一律 None。"""
    store = _make_store(tmp_path)
    assert store.get_report("ghost", seq=1) is None
    store.create("a4", **_task_kwargs())
    assert store.get_report("a4", seq=1) is None  # 未落报告
    store.set_report("a4", _report("a4", health=80.0, title="v1"))
    assert store.get_report("a4", seq=2) is None  # 版本越界
    assert store.get_report("a4", seq=0) is None  # seq 从 1 起


def test_delete_cascades_history_and_recreated_task_restarts_seq(tmp_path: Path):
    """删行是权威操作：report_history 随任务行级联清除（与 events 同语义），
    同 ID 重建后 seq 重新从 1 计数；prune 同样级联（被淘汰行历史不残留）。"""
    store = _make_store(tmp_path)
    store.create("a5", **_task_kwargs())
    store.set_report("a5", _report("a5", health=90.0, title="v1"))
    store.set_report("a5", _report("a5", health=80.0, title="v2"))
    assert len(store.list_reports("a5")) == 2

    assert store.delete("a5") is True
    assert store.list_reports("a5") == []
    store.create("a5", **_task_kwargs())
    store.set_report("a5", _report("a5", health=95.0, title="重建后唯一版"))
    assert [v["seq"] for v in store.list_reports("a5")] == [1]
    assert store.get_report("a5", seq=1)["health_score"] == 95.0


def test_prune_evicts_history_with_row(tmp_path: Path):
    """容量淘汰：被淘汰终态行的历史随行清除，保留行历史原样。"""
    store = _make_store(tmp_path)
    store.create("old", **_task_kwargs(status="done", created_at="2026-01-01T00:00:01"))
    store.set_report("old", _report("old", health=50.0, title="旧任务"))
    store.create("keep", **_task_kwargs(status="done", created_at="2026-01-01T00:00:02"))
    store.set_report("keep", _report("keep", health=99.0, title="新任务"))
    assert store.prune(keep=1) == 1
    assert store.get("old") is None
    assert store.list_reports("old") == []
    assert [v["seq"] for v in store.list_reports("keep")] == [1]


def test_report_history_persists_across_reopen(tmp_path: Path):
    """持久化：关连接后重开同一库，历史版本原样可读、seq 续接不重置。"""
    db = tmp_path / "audits.db"
    store = TaskStore(db)
    store.create("p1", **_task_kwargs())
    store.set_report("p1", _report("p1", health=88.0, title="v1"))
    store.set_report("p1", _report("p1", health=77.0, title="v2"))
    store.close()
    reopened = TaskStore(db)
    try:
        assert [v["seq"] for v in reopened.list_reports("p1")] == [1, 2]
        assert reopened.get_report("p1", seq=1)["health_score"] == 88.0
        reopened.set_report("p1", _report("p1", health=66.0, title="v3"))
        assert [v["seq"] for v in reopened.list_reports("p1")] == [1, 2, 3]
    finally:
        reopened.close()


# ---------------------------------------------------------------- 老库迁移（零破坏）
def test_legacy_db_without_report_history_table_migrates(tmp_path: Path):
    """老库零破坏：手工建无 report_history 表的旧库文件（W26 形态），构造
    TaskStore 不报错且可用——建表后双写/查询立即可用，存量行不受影响。"""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE audits ("
        " audit_id TEXT PRIMARY KEY, status TEXT NOT NULL, error TEXT,"
        " created_at TEXT NOT NULL, source_path TEXT NOT NULL DEFAULT '',"
        " do_fix INTEGER NOT NULL DEFAULT 0, do_tests INTEGER NOT NULL DEFAULT 0,"
        " config_json TEXT NOT NULL DEFAULT '', report_json TEXT,"
        " updated_at TEXT NOT NULL DEFAULT '', stage_done TEXT NOT NULL DEFAULT '')"
    )
    conn.execute(
        "INSERT INTO audits (audit_id, status, created_at, source_path, updated_at)"
        " VALUES ('old1', 'done', '2026-01-01T00:00:00', '/tmp/proj', '2026-01-01T00:00:00')"
    )
    conn.execute(
        "CREATE TABLE events (audit_id TEXT NOT NULL, seq INTEGER NOT NULL,"
        " event_json TEXT NOT NULL, PRIMARY KEY (audit_id, seq))"
    )
    conn.commit()
    conn.close()

    store = TaskStore(db)  # 打开即补建 report_history（不抛错）
    try:
        assert store.get("old1") is not None
        assert store.list_reports("old1") == []  # 存量任务无历史（report_json 旧值不回填）
        store.set_report("old1", _report("old1", health=70.0, title="迁移后首版"))
        assert [v["seq"] for v in store.list_reports("old1")] == [1]  # 从 1 起递增
        assert store.get_report("old1", seq=1)["health_score"] == 70.0
        assert isinstance(store.get_report("old1"), AuditReport)  # 既有读路径可用
    finally:
        store.close()
