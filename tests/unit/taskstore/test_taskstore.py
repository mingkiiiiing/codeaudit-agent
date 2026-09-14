"""TaskStore（W11-A1）单元测试：CRUD 全路径、事件流、报告往返、协作取消、sweep/prune、线程并发。

全部用例经 tmp_path 隔离，离线运行，不依赖 server 与 LLM。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

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


def _minimal_report(audit_id: str) -> AuditReport:
    """最小合法报告：含 1 个 Issue，覆盖 to_dict/from_dict 的关键字段。"""
    return AuditReport(
        audit_id=audit_id,
        project_name="proj",
        languages={"python": 1.0},
        loc=100,
        health_score=88.5,
        summary={"high": 1},
        issues=[
            Issue(
                id="i1",
                category=Category.BUG,
                severity=Severity.HIGH,
                title="空指针风险",
                file="app/services/users.py",
                line_start=10,
                line_end=12,
            )
        ],
        created_at="2026-01-01T00:00:00",
    )


def _create(store: TaskStore, audit_id: str, **overrides: Any) -> None:
    store.create(audit_id, **_task_kwargs(**overrides))


# ---------------------------------------------------------------- CRUD 全路径
def test_create_and_get_roundtrip(tmp_path: Path):
    store = _make_store(tmp_path)
    _create(
        store,
        "a1",
        status="running",
        created_at="2026-01-02T03:04:05",
        do_fix=True,
        do_tests=True,
        config_json='{"k": 1}',
    )
    got = store.get("a1")
    assert got is not None
    assert set(got.keys()) == {
        "audit_id",
        "status",
        "error",
        "created_at",
        "source_path",
        "do_fix",
        "do_tests",
        "config_json",
    }
    assert got["audit_id"] == "a1"
    assert got["status"] == "running"
    assert got["error"] is None
    assert got["created_at"] == "2026-01-02T03:04:05"
    assert got["do_fix"] is True
    assert got["do_tests"] is True
    assert got["config_json"] == '{"k": 1}'
    assert store.exists("a1") is True


def test_get_missing_returns_none(tmp_path: Path):
    store = _make_store(tmp_path)
    assert store.get("ghost") is None
    assert store.exists("ghost") is False
    total, items = store.list()
    assert (total, items) == (0, [])


def test_list_ordering_new_to_old_and_same_second_insert_order(tmp_path: Path):
    store = _make_store(tmp_path)
    # 同秒插入：后建者在前（rowid 降序）
    _create(store, "t1", created_at="2026-01-01T00:00:00")
    _create(store, "t2", created_at="2026-01-01T00:00:00")
    _create(store, "t3", created_at="2026-01-01T00:00:00")
    # 更晚时间戳者更前
    _create(store, "t4", created_at="2026-01-02T00:00:00")
    total, items = store.list()
    assert total == 4
    assert [it["audit_id"] for it in items] == ["t4", "t3", "t2", "t1"]


def test_list_pagination_keeps_total(tmp_path: Path):
    store = _make_store(tmp_path)
    for i in range(5):
        _create(store, f"p{i}", created_at=f"2026-01-0{i + 1}T00:00:00")
    total, items = store.list(limit=2, offset=1)
    assert total == 5
    assert [it["audit_id"] for it in items] == ["p3", "p2"]  # 新→旧，跳过 p4
    total, items = store.list(limit=2)
    assert total == 5
    assert [it["audit_id"] for it in items] == ["p4", "p3"]


def test_delete_removes_row_and_returns_existence(tmp_path: Path):
    store = _make_store(tmp_path)
    _create(store, "d1")
    assert store.delete("d1") is True
    assert store.get("d1") is None
    assert store.exists("d1") is False
    # 再删一次：不存在返回 False，不抛异常
    assert store.delete("d1") is False


def test_duplicate_create_raises_value_error_with_audit_id(tmp_path: Path):
    store = _make_store(tmp_path)
    _create(store, "dup1")
    with pytest.raises(ValueError, match="dup1"):
        store.create("dup1", **_task_kwargs())


def test_close_is_idempotent(tmp_path: Path):
    store = _make_store(tmp_path)
    _create(store, "c1")
    store.close()
    store.close()  # 重复 close 无异常


# ---------------------------------------------------------------- set_status 语义
def test_set_status_terminal_keeps_error_and_active_forces_none(tmp_path: Path):
    store = _make_store(tmp_path)
    _create(store, "s1")
    store.set_status("s1", "running", error="应被清空")  # 非终态：error 强制 None
    assert store.get("s1")["error"] is None
    store.set_status("s1", "failed", error="解析失败")
    got = store.get("s1")
    assert got["status"] == "failed"
    assert got["error"] == "解析失败"


def test_set_status_on_missing_is_silent(tmp_path: Path):
    store = _make_store(tmp_path)
    store.set_status("ghost", "done")  # DELETE 竞态容忍：不抛异常


def test_count_active_by_status(tmp_path: Path):
    store = _make_store(tmp_path)
    assert store.count_active() == 0
    _create(store, "a", status="queued")
    _create(store, "b", status="running")
    _create(store, "c", status="done")
    _create(store, "d", status="failed")
    assert store.count_active() == 2  # 仅 queued + running
    store.set_status("a", "done")
    assert store.count_active() == 1
    store.set_status("b", "failed")
    assert store.count_active() == 0


# ---------------------------------------------------------------- 事件流
def test_append_event_allocates_sequential_seq(tmp_path: Path):
    store = _make_store(tmp_path)
    _create(store, "e1")
    assert store.append_event("e1", {"stage": "ingest"}) == 1
    assert store.append_event("e1", {"stage": "detect"}) == 2
    assert store.append_event("e1", {"stage": "report"}) == 3
    events = store.get_events("e1")
    assert [seq for seq, _ in events] == [1, 2, 3]
    assert events[0][1] == {"stage": "ingest"}
    assert events[2][1] == {"stage": "report"}


def test_get_events_after_seq_cursor(tmp_path: Path):
    store = _make_store(tmp_path)
    _create(store, "e2")
    for i in range(4):
        store.append_event("e2", {"i": i})
    assert [seq for seq, _ in store.get_events("e2", after_seq=0)] == [1, 2, 3, 4]
    assert [seq for seq, _ in store.get_events("e2", after_seq=2)] == [3, 4]
    assert store.get_events("e2", after_seq=4) == []


def test_append_event_missing_task_returns_minus_one(tmp_path: Path):
    store = _make_store(tmp_path)
    assert store.append_event("ghost", {"x": 1}) == -1


def test_delete_cascades_events(tmp_path: Path):
    store = _make_store(tmp_path)
    _create(store, "e3")
    store.append_event("e3", {"a": 1})
    store.append_event("e3", {"a": 2})
    assert store.delete("e3") is True
    assert store.get_events("e3") == []
    # 同 id 重建后事件从 1 重新计数（旧行已级联清除）
    _create(store, "e3")
    assert store.append_event("e3", {"fresh": True}) == 1


# ---------------------------------------------------------------- 报告往返
def test_set_report_get_report_roundtrip(tmp_path: Path):
    store = _make_store(tmp_path)
    _create(store, "r1")
    report = _minimal_report("r1")
    store.set_report("r1", report)
    loaded = store.get_report("r1")
    assert loaded is not None
    assert loaded.to_dict() == report.to_dict()
    assert loaded.issues[0].id == "i1"
    assert loaded.issues[0].severity == Severity.HIGH
    assert loaded.issues[0].file == "app/services/users.py"
    # 元数据行不携带 report_json
    assert "report_json" not in store.get("r1")


def test_get_report_none_when_not_set_or_missing(tmp_path: Path):
    store = _make_store(tmp_path)
    _create(store, "r2")
    assert store.get_report("r2") is None  # 未落报告
    assert store.get_report("ghost") is None  # 任务不存在


# ---------------------------------------------------------------- 协作式取消
def test_request_cancel_and_is_cancelled(tmp_path: Path):
    store = _make_store(tmp_path)
    _create(store, "k1")
    assert store.is_cancelled("k1") is False  # 未取消
    assert store.request_cancel("k1") is True  # 任务存在
    assert store.is_cancelled("k1") is True
    # 哨兵经 error 列可观测（实现契约：error 列借用）
    assert store.get("k1")["error"] == "__cancel_requested__"


def test_request_cancel_missing_task_returns_false(tmp_path: Path):
    store = _make_store(tmp_path)
    assert store.request_cancel("ghost") is False


def test_is_cancelled_true_when_row_deleted(tmp_path: Path):
    store = _make_store(tmp_path)
    _create(store, "k2")
    assert store.is_cancelled("k2") is False
    store.delete("k2")
    assert store.is_cancelled("k2") is True  # 表项消失即取消（DELETE 竞态容忍）


# ---------------------------------------------------------------- sweep_interrupted
def test_sweep_interrupted_only_touches_non_terminal(tmp_path: Path):
    store = _make_store(tmp_path)
    _create(store, "q1", status="queued")
    _create(store, "r1", status="running")
    _create(store, "d1", status="done")
    _create(store, "f1", status="failed", error="旧错误")
    swept = store.sweep_interrupted()
    assert swept == 2
    q = store.get("q1")
    assert q["status"] == "failed"
    assert q["error"] == "服务重启中断"
    r = store.get("r1")
    assert r["status"] == "failed"
    assert r["error"] == "服务重启中断"
    assert store.get("d1")["status"] == "done"  # 终态不动
    f = store.get("f1")
    assert f["status"] == "failed"
    assert f["error"] == "旧错误"  # 已 failed 的不覆盖
    assert store.count_active() == 0


def test_sweep_on_empty_store_returns_zero(tmp_path: Path):
    store = _make_store(tmp_path)
    assert store.sweep_interrupted() == 0


# ---------------------------------------------------------------- prune 容量淘汰
def test_prune_evicts_oldest_terminal_only(tmp_path: Path):
    store = _make_store(tmp_path)
    # 60 行：i%3==0 -> queued，i%3==1 -> done，i%3==2 -> failed；created_at 逐行递增
    for i in range(60):
        status = ("queued", "done", "failed")[i % 3]
        _create(store, f"t{i:02d}", status=status, created_at=f"2026-01-01T00:{i:02d}:00")
    store.append_event("t01", {"keep": False})  # 将被淘汰
    store.append_event("t15", {"keep": True})  # 终态但靠后，保留
    evicted = store.prune(keep=50)
    assert evicted == 10
    total, items = store.list()
    assert total == 50
    ids = {it["audit_id"] for it in items}
    # 只少了终态：淘汰的是最早的 10 个终态行（i=1,2,4,5,7,8,10,11,13,14）
    evicted_ids = {f"t{i:02d}" for i in (1, 2, 4, 5, 7, 8, 10, 11, 13, 14)}
    assert ids.isdisjoint(evicted_ids)
    # queued/running 全保留
    for i in range(0, 60, 3):
        assert f"t{i:02d}" in ids
    # 保留的终态行仍可查
    assert store.get("t15") is not None
    # 事件级联：被淘汰者事件清空，保留者事件仍在
    assert store.get_events("t01") == []
    assert store.get_events("t15") == [(1, {"keep": True})]


def test_prune_same_second_tiebreak_by_insert_order(tmp_path: Path):
    store = _make_store(tmp_path)
    for i in range(5):
        _create(store, f"same{i}", status="done", created_at="2026-01-01T00:00:00")
    assert store.prune(keep=3) == 2  # 同秒按插入序旧→新淘汰：same0、same1 出局
    total, items = store.list()
    assert total == 3
    assert [it["audit_id"] for it in items] == ["same4", "same3", "same2"]


def test_prune_noop_when_under_capacity(tmp_path: Path):
    store = _make_store(tmp_path)
    for i in range(3):
        _create(store, f"u{i}", status="done")
    assert store.prune(keep=50) == 0
    assert store.list()[0] == 3


# ---------------------------------------------------------------- 线程并发（check_same_thread=False + 锁的核心验证）
def test_concurrent_append_event_seq_unique_and_contiguous(tmp_path: Path):
    store = _make_store(tmp_path)
    _create(store, "conc")
    seqs: list[int] = []
    collect_lock = threading.Lock()
    barrier = threading.Barrier(10)

    def _worker(n_events: int) -> None:
        local: list[int] = []
        barrier.wait()  # 同时起跑，最大化争用
        for _ in range(n_events):
            local.append(store.append_event("conc", {"pid": id(threading.current_thread())}))
        with collect_lock:
            seqs.extend(local)

    threads = [threading.Thread(target=_worker, args=(20,)) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(seqs) == list(range(1, 201))  # 无重复且连续 1..200
    events = store.get_events("conc")
    assert [seq for seq, _ in events] == list(range(1, 201))


def test_concurrent_create_distinct_ids_and_count_active(tmp_path: Path):
    store = _make_store(tmp_path)
    barrier = threading.Barrier(10)

    def _worker(k: int) -> None:
        barrier.wait()
        status = "queued" if k % 2 == 0 else "running"
        _create(store, f"c{k:02d}", status=status, created_at=f"2026-01-01T00:{k:02d}:00")

    threads = [threading.Thread(target=_worker, args=(k,)) for k in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total, items = store.list()
    assert total == 10
    assert len(items) == 10
    assert store.count_active() == 10  # 5 queued + 5 running，计数全局准确
    for k in range(10):
        assert store.exists(f"c{k:02d}")
    # 并发后逐个落终态，计数归零
    for k in range(10):
        store.set_status(f"c{k:02d}", "done")
    assert store.count_active() == 0


def test_concurrent_readers_during_writes(tmp_path: Path):
    """写线程并发 create/append 期间读线程 list/get/get_events 不报错、结果一致。"""
    store = _make_store(tmp_path)
    _create(store, "base")
    stop = threading.Event()
    read_errors: list[Exception] = []

    def _reader() -> None:
        try:
            while not stop.is_set():
                total, _ = store.list()
                assert total >= 1
                assert store.get("base") is not None
                store.get_events("base")
                store.exists("base")
                store.count_active()
        except Exception as exc:  # 读线程异常统一收集，主线程 join 后断言
            read_errors.append(exc)

    def _writer(k: int) -> None:
        _create(store, f"w{k:02d}", created_at=f"2026-01-01T00:{k:02d}:00")
        for _ in range(10):
            store.append_event(f"w{k:02d}", {"k": k})

    readers = [threading.Thread(target=_reader) for _ in range(3)]
    for r in readers:
        r.start()
    writers = [threading.Thread(target=_writer, args=(k,)) for k in range(5)]
    for w in writers:
        w.start()
    for w in writers:
        w.join()
    stop.set()
    for r in readers:
        r.join()

    assert read_errors == []
    total, _ = store.list()
    assert total == 6
    assert store.count_active() == 6  # 全部 queued
