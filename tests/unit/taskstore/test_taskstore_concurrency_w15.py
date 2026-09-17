"""TaskStore W15-B 存储一致性测试（契约 docs/20 §4.3）：双连接并发压测 + grace 清扫。

覆盖：
- 双 TaskStore 实例指向同一 db 文件，多线程并发 append_event：零 OperationalError
  泄漏、每任务 seq 严格 1..N 无重复无断档、事件总数正确（A3 seq 竞态收口验证）；
- sweep_interrupted(grace_seconds) 的 brother-worker 语义：grace 内不清扫、
  超 grace 清扫、grace=0 保持既有语义；
- 旧库（无 updated_at 列）打开时自动迁移回填 created_at；
- 库锁退避重试路径（注入一次性 locked 错误 + monkeypatch sleep，离线确定性）。

全部用例经 tmp_path 隔离，离线运行，不依赖 server 与 LLM。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from audit import taskstore as taskstore_mod
from audit.taskstore import TaskStore


# ---------------------------------------------------------------- 夹具与工具
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


def _backdate_updated_at(store: TaskStore, audit_id: str, iso: str) -> None:
    """白盒构造「陈旧行」：把 updated_at 改写为指定历史时刻（grace 语义测试专用）。"""
    with store._lock:
        store._conn.execute(
            "UPDATE audits SET updated_at = ? WHERE audit_id = ?", (iso, audit_id)
        )
        store._conn.commit()


def _read_updated_at(store: TaskStore, audit_id: str) -> str:
    with store._lock:
        row = store._conn.execute(
            "SELECT updated_at FROM audits WHERE audit_id = ?", (audit_id,)
        ).fetchone()
    assert row is not None
    return str(row["updated_at"])


def _stale_iso(seconds_ago: int) -> str:
    """生成 seconds_ago 前的 UTC ISO 字符串（与 _utc_now_iso 同格式）。"""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat(
        timespec="seconds"
    )


# ---------------------------------------------------------------- 双连接并发压测（W15-B3）
def test_two_stores_concurrent_append_zero_error_and_strict_seq(tmp_path: Path):
    """2 实例 × 8 线程 × 50 事件并发追加同一批任务：零 OperationalError 泄漏，
    每任务 seq 严格 1..400 无重复无断档，事件总数正确（跨连接 BEGIN IMMEDIATE 收口）。"""
    db = tmp_path / "audits.db"
    store_a = TaskStore(db)
    store_b = TaskStore(db)
    store_a.create("task-even", **_task_kwargs())
    store_a.create("task-odd", **_task_kwargs())

    errors: list[Exception] = []
    results: dict[str, list[int]] = {"task-even": [], "task-odd": []}
    collect_lock = threading.Lock()
    threads_per_store = 8
    events_per_thread = 50
    barrier = threading.Barrier(2 * threads_per_store)

    def _worker(store: TaskStore, task: str, tag: tuple[int, int]) -> None:
        local: list[int] = []
        try:
            barrier.wait()  # 16 线程同时起跑，最大化跨连接争用
            for i in range(events_per_thread):
                local.append(store.append_event(task, {"tag": tag, "i": i}))
        except Exception as exc:  # OperationalError 泄漏即在此暴露
            with collect_lock:
                errors.append(exc)
            return
        with collect_lock:
            results[task].extend(local)

    threads: list[threading.Thread] = []
    for s_idx, store in enumerate((store_a, store_b)):
        for k in range(threads_per_store):
            task = "task-even" if k % 2 == 0 else "task-odd"
            threads.append(threading.Thread(target=_worker, args=(store, task, (s_idx, k))))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 验收一：零 OperationalError（或其他异常）泄漏到调用方
    assert errors == []
    expected = threads_per_store * events_per_thread  # 每任务 8 线程 × 50 = 400
    for task in ("task-even", "task-odd"):
        seqs = sorted(results[task])
        # 验收二：每任务 seq 严格 1..N，无重复无断档
        assert seqs == list(range(1, expected + 1))
        # 验收三：落库事件总数正确且读取序一致（双实例读同一答案）
        events_a = store_a.get_events(task)
        events_b = store_b.get_events(task)
        assert len(events_a) == expected
        assert [s for s, _ in events_a] == list(range(1, expected + 1))
        assert [s for s, _ in events_b] == list(range(1, expected + 1))
    # 全库事件总数 = 两任务之和，无一条丢失或写到错任务
    total_events = len(store_a.get_events("task-even")) + len(store_a.get_events("task-odd"))
    assert total_events == 2 * expected

    store_a.close()
    store_b.close()


# ---------------------------------------------------------------- sweep grace 语义（W15-B4）
def test_sweep_grace_spares_fresh_and_sweeps_stale_brother_worker(tmp_path: Path):
    """brother-worker 场景：grace 内的 queued/running 行不被清扫，超 grace 的被清扫，
    终态行永不动。"""
    store = TaskStore(tmp_path / "audits.db")
    store.create("fresh-running", **_task_kwargs(status="running"))
    store.create("fresh-queued", **_task_kwargs(status="queued"))
    store.create("stale-running", **_task_kwargs(status="running"))
    store.create("stale-queued", **_task_kwargs(status="queued"))
    store.create("old-done", **_task_kwargs(status="done"))
    _backdate_updated_at(store, "stale-running", _stale_iso(60))
    _backdate_updated_at(store, "stale-queued", _stale_iso(60))

    swept = store.sweep_interrupted(grace_seconds=10)

    assert swept == 2  # 只清扫两行陈旧非终态
    assert store.get("fresh-running")["status"] == "running"  # 兄弟 worker 活跃任务存活
    assert store.get("fresh-queued")["status"] == "queued"
    assert store.get("stale-running")["status"] == "failed"
    assert store.get("stale-running")["error"] == "服务重启中断"
    assert store.get("stale-queued")["status"] == "failed"
    assert store.get("old-done")["status"] == "done"  # 终态永不动
    assert store.count_active() == 2  # 剩余 = fresh-running + fresh-queued


def test_sweep_grace_zero_keeps_legacy_semantics(tmp_path: Path):
    """grace=0（含不传参）= 既有语义：新创建的非终态行也一律清扫。"""
    store = TaskStore(tmp_path / "audits.db")
    store.create("fresh-running", **_task_kwargs(status="running"))
    store.create("fresh-queued", **_task_kwargs(status="queued"))
    store.create("done-row", **_task_kwargs(status="done"))

    assert store.sweep_interrupted() == 2  # 不传参：既有调用形态行为不变
    assert store.get("fresh-running")["status"] == "failed"
    assert store.get("fresh-queued")["status"] == "failed"
    assert store.get("done-row")["status"] == "done"

    store.create("fresh2", **_task_kwargs(status="running"))
    assert store.sweep_interrupted(grace_seconds=0) == 1  # 显式 0 同语义
    assert store.get("fresh2")["status"] == "failed"


def test_touch_paths_keep_updated_at_fresh(tmp_path: Path):
    """写状态路径（create/set_status/set_report/request_cancel/append_event）
    统一前移 updated_at——touch 后该值落在最近窗口内。"""
    store = TaskStore(tmp_path / "audits.db")
    store.create("t1", **_task_kwargs())
    _backdate_updated_at(store, "t1", _stale_iso(300))

    def _assert_fresh(audit_id: str) -> None:
        ts = datetime.fromisoformat(_read_updated_at(store, audit_id))
        assert abs(datetime.now(timezone.utc) - ts) < timedelta(seconds=30)

    store.append_event("t1", {"x": 1})
    _assert_fresh("t1")  # 事件流即活性证据
    _backdate_updated_at(store, "t1", _stale_iso(300))
    store.request_cancel("t1")
    _assert_fresh("t1")
    _backdate_updated_at(store, "t1", _stale_iso(300))
    store.set_status("t1", "done")
    _assert_fresh("t1")
    _backdate_updated_at(store, "t1", _stale_iso(300))
    store.set_report("t1", _minimal_report("t1"))
    _assert_fresh("t1")


def _minimal_report(audit_id: str):
    """最小合法 AuditReport（复用主测试文件口径的最简字段组）。"""
    from audit.models import AuditReport, Category, Issue, Severity

    return AuditReport(
        audit_id=audit_id,
        project_name="proj",
        languages={"python": 1.0},
        loc=10,
        health_score=90.0,
        summary={"low": 0},
        issues=[
            Issue(
                id="i1",
                category=Category.BUG,
                severity=Severity.LOW,
                title="示例",
                file="a.py",
                line_start=1,
                line_end=1,
            )
        ],
        created_at="2026-01-01T00:00:00",
    )


# ---------------------------------------------------------------- 旧库迁移（W15-B4）
def test_legacy_db_without_updated_at_migrated_and_backfilled(tmp_path: Path):
    """W14 及更早的库（audits 无 updated_at 列）打开时自动 ALTER 补列，
    存量行回填 created_at，随后读写与 grace 清扫均正常。"""
    db = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(db))
    raw.execute(
        "CREATE TABLE audits ("
        " audit_id TEXT PRIMARY KEY, status TEXT NOT NULL, error TEXT,"
        " created_at TEXT NOT NULL, source_path TEXT NOT NULL DEFAULT '',"
        " do_fix INTEGER NOT NULL DEFAULT 0, do_tests INTEGER NOT NULL DEFAULT 0,"
        " config_json TEXT NOT NULL DEFAULT '', report_json TEXT)"
    )
    raw.execute(
        "CREATE TABLE events (audit_id TEXT NOT NULL, seq INTEGER NOT NULL,"
        " event_json TEXT NOT NULL, PRIMARY KEY (audit_id, seq))"
    )
    raw.execute(
        "INSERT INTO audits (audit_id, status, created_at)"
        " VALUES ('legacy-1', 'running', '2026-01-01T00:00:00')"
    )
    raw.commit()
    raw.close()

    store = TaskStore(db)  # 打开即迁移
    assert _read_updated_at(store, "legacy-1") == "2026-01-01T00:00:00"  # 回填 created_at
    # 既有读写不受迁移影响
    assert store.get("legacy-1")["status"] == "running"
    assert store.append_event("legacy-1", {"x": 1}) == 1
    # 二次打开不重复迁移（幂等），且 touch 后 updated_at 变为 UTC 新格式
    store.set_status("legacy-1", "done")
    store.close()
    store2 = TaskStore(db)
    ts = datetime.fromisoformat(_read_updated_at(store2, "legacy-1"))
    assert abs(datetime.now(timezone.utc) - ts) < timedelta(seconds=30)
    store2.close()


def test_new_db_schema_has_updated_at_and_create_touches_it(tmp_path: Path):
    """新建库：schema 自带 updated_at 列，create() 落当前 UTC 时刻。"""
    store = TaskStore(tmp_path / "fresh.db")
    store.create("n1", **_task_kwargs())
    ts = datetime.fromisoformat(_read_updated_at(store, "n1"))
    assert abs(datetime.now(timezone.utc) - ts) < timedelta(seconds=30)
    store.close()


# ---------------------------------------------------------------- 库锁退避重试（W15-B2）
class _LockedOnceConn:
    """包装真实连接：前 fail_times 次命中 marker SQL 时抛 database is locked。

    用于离线确定性验证 _retry_on_locked 路径（真实跨进程锁争用无法稳定复现）。
    """

    def __init__(self, real: sqlite3.Connection, fail_times: int, marker: str, message: str):
        self._real = real
        self._remaining = fail_times
        self._marker = marker
        self._message = message

    def execute(self, sql: str, params: Any = ()):  # 与 sqlite3.Connection.execute 签名对齐
        if self._marker in sql and self._remaining > 0:
            self._remaining -= 1
            raise sqlite3.OperationalError(self._message)
        return self._real.execute(sql, params)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


def _install_sleep_recorder(monkeypatch) -> list[float]:
    """把 taskstore 模块可见的 time.sleep 换成记录器（不真等待），返回记录列表。"""
    sleeps: list[float] = []
    monkeypatch.setattr(taskstore_mod.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


def test_create_retries_on_locked_then_succeeds(tmp_path: Path, monkeypatch):
    """前两次 INSERT 抛 locked：退避 0.05/0.1 后第三次成功，调用方无感。"""
    sleeps = _install_sleep_recorder(monkeypatch)
    store = TaskStore(tmp_path / "audits.db")
    store._conn = _LockedOnceConn(
        store._conn, fail_times=2, marker="INSERT INTO audits", message="database is locked"
    )
    store.create("r1", **_task_kwargs())
    assert sleeps == [0.05, 0.1]  # 指数退避序列
    assert store.get("r1") is not None  # 重试后落库成功
    store.close()


def test_retry_exhausted_raises_last_operational_error(tmp_path: Path, monkeypatch):
    """持续 locked：重试 3 次耗尽后原样抛出，退避序列 0.05/0.1/0.2。"""
    sleeps = _install_sleep_recorder(monkeypatch)
    store = TaskStore(tmp_path / "audits.db")
    store._conn = _LockedOnceConn(
        store._conn, fail_times=99, marker="INSERT INTO audits", message="database is locked"
    )
    try:
        store.create("r2", **_task_kwargs())
        raise AssertionError("应当抛出 OperationalError")
    except sqlite3.OperationalError as exc:
        assert "locked" in str(exc)
    assert sleeps == [0.05, 0.1, 0.2]
    assert store.get("r2") is None  # 未落库
    store.close()


def test_non_locked_operational_error_not_retried(tmp_path: Path, monkeypatch):
    """非锁类 OperationalError（如 no such table）不重试、立即上抛。"""
    sleeps = _install_sleep_recorder(monkeypatch)
    store = TaskStore(tmp_path / "audits.db")
    store._conn = _LockedOnceConn(
        store._conn, fail_times=99, marker="INSERT INTO audits", message="no such table: audits"
    )
    try:
        store.create("r3", **_task_kwargs())
        raise AssertionError("应当抛出 OperationalError")
    except sqlite3.OperationalError as exc:
        assert "no such table" in str(exc)
    assert sleeps == []  # 零退避：非争用错误不进重试循环
    store.close()


def test_append_event_retries_after_locked_mid_transaction(tmp_path: Path, monkeypatch):
    """append_event 事务中途（INSERT）遇 locked：回滚后整段重放成功，seq 无断档。"""
    sleeps = _install_sleep_recorder(monkeypatch)
    store = TaskStore(tmp_path / "audits.db")
    store.create("e1", **_task_kwargs())
    store.append_event("e1", {"n": 0})  # seq=1
    store._conn = _LockedOnceConn(
        store._conn, fail_times=1, marker="INSERT INTO events", message="database is locked"
    )
    assert store.append_event("e1", {"n": 1}) == 2  # 重放后分配 seq=2
    assert sleeps == [0.05]
    assert [s for s, _ in store.get_events("e1")] == [1, 2]
    store.close()
