"""TaskStore（W11-A1）单元测试：CRUD 全路径、事件流、报告往返、协作取消、sweep/prune、线程并发。

全部用例经 tmp_path 隔离，离线运行，不依赖 server 与 LLM。
"""

from __future__ import annotations

import json
import sqlite3
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


# ---------------------------------------------------------------- 磁盘工作区回收（W14 M-4）
def _materialize_task_files(work_root: Path, audit_id: str) -> tuple[Path, Path]:
    """在工作区物化一个任务的目录与上传 zip，返回 (workdir, zip) 路径。"""
    workdir = work_root / audit_id
    (workdir / "reports").mkdir(parents=True, exist_ok=True)
    (workdir / "marker.txt").write_text("artifact", encoding="utf-8")
    uploads = work_root / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    zip_path = uploads / f"{audit_id}.zip"
    zip_path.write_bytes(b"PK\x05\x06")
    return workdir, zip_path


def test_cleanup_workdir_removes_workdir_and_uploads(tmp_path: Path):
    """终态/已删任务：工作目录与上传 zip（含 .part）一并回收。"""
    store = TaskStore(tmp_path / "db.sqlite", work_root=tmp_path)
    _create(store, "w1", status="done")
    workdir, zip_path = _materialize_task_files(tmp_path, "w1")
    part = tmp_path / "uploads" / "w1.zip.part"
    part.write_bytes(b"half")

    assert store.cleanup_workdir("w1") is True
    assert not workdir.exists()
    assert not zip_path.exists()
    assert not part.exists()


def test_cleanup_workdir_refuses_non_terminal_task(tmp_path: Path):
    """硬约束：queued/running 任务的工作副本绝不回收（cleanup 直接拒绝）。"""
    store = TaskStore(tmp_path / "db.sqlite", work_root=tmp_path)
    for status in ("queued", "running"):
        _create(store, f"act-{status}", status=status)
        workdir, zip_path = _materialize_task_files(tmp_path, f"act-{status}")
        assert store.cleanup_workdir(f"act-{status}") is False
        assert workdir.is_dir()  # 目录原样保留
        assert zip_path.is_file()
        store.delete(f"act-{status}")  # 清理行，下一轮干净


def test_cleanup_workdir_noop_without_work_root(tmp_path: Path):
    """未配置 work_root（向后兼容构造）：no-op 返回 False，不抛错。"""
    store = _make_store(tmp_path)
    _create(store, "nw1", status="done")
    assert store.cleanup_workdir("nw1") is False


def test_cleanup_workdir_rejects_unsafe_ids(tmp_path: Path):
    """防路径注入：非单一安全路径段（../、子路径、. .. 空）一律拒绝。"""
    store = TaskStore(tmp_path / "db.sqlite", work_root=tmp_path)
    outside = tmp_path.parent / "evil"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "keep.txt").write_text("x", encoding="utf-8")
    for bad_id in ("../evil", "a/b", ".", "..", "", " x ", "x\n"):
        assert store.cleanup_workdir(bad_id) is False
    # work_root 之外的目录原样保留
    assert (outside / "keep.txt").is_file()


def test_cleanup_workdir_failure_warns_but_never_raises(tmp_path: Path, monkeypatch):
    """清理失败（Windows 文件占用）：记 warning 返回 False，绝不抛错。"""
    import shutil as _shutil

    store = TaskStore(tmp_path / "db.sqlite", work_root=tmp_path)
    _create(store, "f1", status="done")
    workdir, _zip = _materialize_task_files(tmp_path, "f1")

    def _boom(_path, *a, **kw):
        raise OSError("文件被占用（模拟 Windows 场景）")

    monkeypatch.setattr(_shutil, "rmtree", _boom)
    assert store.cleanup_workdir("f1") is False  # 不抛错
    assert workdir.is_dir()  # 目录回收失败保留
    # 删行（权威操作）不受影响
    assert store.delete("f1") is True
    assert store.get("f1") is None


def test_prune_recycles_evicted_rows_workdirs(tmp_path: Path):
    """prune 淘汰行时同步回收其工作目录；未被淘汰（queued/靠后终态）的目录保留。"""
    store = TaskStore(tmp_path / "db.sqlite", work_root=tmp_path)
    _create(store, "old-done", status="done", created_at="2026-01-01T00:00:01")
    _create(store, "old-run", status="running", created_at="2026-01-01T00:00:02")
    _create(store, "keep-done", status="done", created_at="2026-01-01T00:00:03")
    for aid in ("old-done", "old-run", "keep-done"):
        _materialize_task_files(tmp_path, aid)
    store.append_event("old-done", {"x": 1})

    assert store.prune(keep=2) == 1
    assert store.get("old-done") is None
    assert not (tmp_path / "old-done").exists()  # 被淘汰 → 目录回收
    assert (tmp_path / "old-run").is_dir()  # 非终态不淘汰 → 目录保留
    assert (tmp_path / "keep-done").is_dir()  # 未被淘汰 → 目录保留


def test_sweep_interrupted_recycles_swept_dirs_only(tmp_path: Path):
    """启动 sweep 置 failed 的遗留任务同步回收目录；已终态任务目录不动。"""
    store = TaskStore(tmp_path / "db.sqlite", work_root=tmp_path)
    _create(store, "q1", status="queued", created_at="2026-01-01T00:00:01")
    _create(store, "r1", status="running", created_at="2026-01-01T00:00:02")
    _create(store, "done1", status="done", created_at="2026-01-01T00:00:03")
    for aid in ("q1", "r1", "done1"):
        _materialize_task_files(tmp_path, aid)

    assert store.sweep_interrupted() == 2
    assert not (tmp_path / "q1").exists()
    assert not (tmp_path / "r1").exists()
    assert (tmp_path / "done1").is_dir()  # 终态不动
    assert store.get("q1")["status"] == "failed"  # 置 failed 语义不变


def test_delete_terminal_recycles_dir_but_running_keeps_files(tmp_path: Path):
    """DELETE：终态任务删行 + 回收目录与 zip；running 任务只删行（协作取消），
    文件留给执行体幽灵收尾回收——绝不在此处 rmtree 运行中任务的工作副本。"""
    store = TaskStore(tmp_path / "db.sqlite", work_root=tmp_path)
    _create(store, "dt", status="done", created_at="2026-01-01T00:00:01")
    dt_dir, dt_zip = _materialize_task_files(tmp_path, "dt")
    _create(store, "dr", status="running", created_at="2026-01-01T00:00:02")
    dr_dir, dr_zip = _materialize_task_files(tmp_path, "dr")

    assert store.delete("dt") is True
    assert not dt_dir.exists()
    assert not dt_zip.exists()

    assert store.delete("dr") is True
    assert store.get("dr") is None  # 行已删（取消信号）
    assert dr_dir.is_dir()  # 运行中副本保留
    assert dr_zip.is_file()
    # 执行体幽灵收尾（行已删）时回收放行
    assert store.cleanup_workdir("dr") is True
    assert not dr_dir.exists()
    assert not dr_zip.exists()


# ---------------------------------------------------------------- 断点续跑（W24-C）
def test_record_and_get_stage_done_roundtrip(tmp_path: Path):
    """stage_done 记录往返：记录序保留、幂等去重、任务不存在返回空/False。"""
    store = _make_store(tmp_path)
    _create(store, "sd1")
    assert store.get_stage_done("sd1") == []
    assert store.record_stage_done("sd1", "ingest") is True
    assert store.record_stage_done("sd1", "index") is True
    assert store.record_stage_done("sd1", "ingest") is True  # 幂等：重复记录不报错
    assert store.get_stage_done("sd1") == ["ingest", "index"]  # 记录序，无重复
    assert store.record_stage_done("ghost", "ingest") is False  # 任务不存在
    assert store.get_stage_done("ghost") == []
    assert store.record_stage_done("sd1", "  ") is False  # 空白阶段名拒绝


def test_append_event_stage_done_type_persists_column(tmp_path: Path):
    """生产通路：type=stage_done 的事件经 append_event 同事务并入 stage_done 列。"""
    store = _make_store(tmp_path)
    _create(store, "ev1")
    assert store.append_event("ev1", {"type": "progress", "stage": "ingest", "message": "x"}) == 1
    assert store.append_event("ev1", {"type": "stage_done", "stage": "ingest"}) == 2
    assert store.append_event("ev1", {"type": "stage_done", "stage": "ingest"}) == 3  # 重复事件仍落事件流
    assert store.get_stage_done("ev1") == ["ingest"]  # 但列里幂等去重
    store.append_event("ev1", {"type": "stage_done", "stage": ""})  # 空阶段名：只落事件
    assert store.get_stage_done("ev1") == ["ingest"]
    # 普通事件不带 type=stage_done：不影响列
    store.append_event("ev1", {"type": "progress", "stage": "detect", "message": "阶段 detect 完成"})
    assert store.get_stage_done("ev1") == ["ingest"]


def test_stage_done_survives_reopen(tmp_path: Path):
    """持久化：关连接后重开同一库，stage_done 与状态原样可读。"""
    db = tmp_path / "audits.db"
    store = TaskStore(db)
    _create(store, "p1", status="running")
    store.record_stage_done("p1", "ingest")
    store.record_stage_done("p1", "index")
    store.close()
    reopened = TaskStore(db)
    try:
        assert reopened.get_stage_done("p1") == ["ingest", "index"]
        assert reopened.get("p1")["status"] == "running"
    finally:
        reopened.close()


def test_legacy_db_without_stage_done_column_migrates(tmp_path: Path):
    """老库加列兼容：W23 形态（有 updated_at 无 stage_done）打开即自动补列可用。"""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE audits ("
        " audit_id TEXT PRIMARY KEY, status TEXT NOT NULL, error TEXT,"
        " created_at TEXT NOT NULL, source_path TEXT NOT NULL DEFAULT '',"
        " do_fix INTEGER NOT NULL DEFAULT 0, do_tests INTEGER NOT NULL DEFAULT 0,"
        " config_json TEXT NOT NULL DEFAULT '', report_json TEXT,"
        " updated_at TEXT NOT NULL DEFAULT '')"
    )
    conn.execute(
        "INSERT INTO audits (audit_id, status, created_at, source_path, updated_at)"
        " VALUES ('old1', 'running', '2026-01-01T00:00:00', '/tmp/proj', '2026-01-01T00:00:00')"
    )
    conn.execute(
        "CREATE TABLE events (audit_id TEXT NOT NULL, seq INTEGER NOT NULL,"
        " event_json TEXT NOT NULL, PRIMARY KEY (audit_id, seq))"
    )
    conn.commit()
    conn.close()

    store = TaskStore(db)  # 打开即迁移（不抛错）
    try:
        assert store.get("old1") is not None
        assert store.get_stage_done("old1") == []  # 存量行无进度
        assert store.record_stage_done("old1", "ingest") is True  # 补列后立即可写
        assert store.get_stage_done("old1") == ["ingest"]
    finally:
        store.close()


def test_legacy_db_stage_done_garbage_value_tolerated(tmp_path: Path):
    """stage_done 列出现非法 JSON（人为改库）时解析容错为无进度，sweep 走 failed。"""
    store = _make_store(tmp_path)
    _create(store, "bad", status="running")
    with store._lock:  # 直改列值模拟脏数据（测试专用后门）
        store._conn.execute("UPDATE audits SET stage_done = '{not-json' WHERE audit_id = 'bad'")
        store._conn.commit()
    assert store.get_stage_done("bad") == []
    assert store.sweep_interrupted() == 1
    assert store.get("bad")["status"] == "failed"  # 解析不出进度：按无进度置 failed


def test_sweep_running_with_stage_done_becomes_interrupted(tmp_path: Path):
    """W24-C 核心：带阶段进度的 running → interrupted（可续跑）；其余行既有语义。"""
    store = _make_store(tmp_path)
    _create(store, "run-done", status="running")  # 跑完 ingest/index 后被杀
    store.record_stage_done("run-done", "ingest")
    store.record_stage_done("run-done", "index")
    _create(store, "run-fresh", status="running")  # 一个阶段都没跑完
    _create(store, "queued1", status="queued")
    _create(store, "done1", status="done")
    _create(store, "failed1", status="failed", error="旧错误")

    assert store.sweep_interrupted() == 3
    interrupted = store.get("run-done")
    assert interrupted["status"] == "interrupted"
    assert interrupted["error"] == "服务重启中断（阶段进度已保留，可续跑）"
    # 无进度 running 与 queued：仍按既有语义 failed
    assert store.get("run-fresh")["status"] == "failed"
    assert store.get("run-fresh")["error"] == "服务重启中断"
    assert store.get("queued1")["status"] == "failed"
    # 终态不动
    assert store.get("done1")["status"] == "done"
    assert store.get("failed1")["status"] == "failed"
    assert store.get("failed1")["error"] == "旧错误"


def test_sweep_queued_with_stage_done_stays_failed(tmp_path: Path):
    """interrupted 只放行 running：queued 行即使有进度记录也置 failed（从未开跑）。"""
    store = _make_store(tmp_path)
    _create(store, "q1", status="queued")
    store.record_stage_done("q1", "ingest")
    assert store.sweep_interrupted() == 1
    assert store.get("q1")["status"] == "failed"


def test_sweep_interrupted_preserves_workdir_failed_recycled(tmp_path: Path):
    """磁盘语义：interrupted 组工作副本保留（resume 前提），failed 组照常回收。"""
    store = TaskStore(tmp_path / "db.sqlite", work_root=tmp_path)
    _create(store, "keep-me", status="running", created_at="2026-01-01T00:00:01")
    store.record_stage_done("keep-me", "ingest")
    _create(store, "clean-me", status="queued", created_at="2026-01-01T00:00:02")
    keep_dir, _zip = _materialize_task_files(tmp_path, "keep-me")
    clean_dir, _zip2 = _materialize_task_files(tmp_path, "clean-me")

    assert store.sweep_interrupted() == 2
    assert (tmp_path / "keep-me").is_dir()  # interrupted：目录与索引保留
    assert store.get("keep-me")["status"] == "interrupted"
    assert not (tmp_path / "clean-me").exists()  # failed：目录照常回收
    # interrupted 非终态：cleanup 的终态守卫拒绝（双保险）
    assert store.cleanup_workdir("keep-me") is False
    assert keep_dir.is_dir()


def test_count_active_excludes_interrupted(tmp_path: Path):
    """429 准入口径：interrupted 无执行体在跑，不计入活跃数。"""
    store = _make_store(tmp_path)
    _create(store, "a", status="queued")
    _create(store, "b", status="running")
    store.record_stage_done("b", "ingest")
    assert store.sweep_interrupted() == 2
    assert store.get("b")["status"] == "interrupted"
    assert store.count_active() == 0  # interrupted 不算活跃
    # resume 重建：interrupted → running 后重新计入
    store.set_status("b", "running")
    assert store.count_active() == 1


def test_prune_keeps_interrupted_rows(tmp_path: Path):
    """容量淘汰只清终态：interrupted 行保留（可续跑任务不淘汰）。"""
    store = _make_store(tmp_path)
    _create(store, "int1", status="running")
    store.record_stage_done("int1", "ingest")
    store.sweep_interrupted()
    _create(store, "old-done", status="done", created_at="2026-01-01T00:00:01")
    assert store.prune(keep=1) == 1  # 只淘汰 done 行
    assert store.get("int1") is not None  # interrupted 保留
    assert store.get_stage_done("int1") == ["ingest"]


# ---------------------------------------------------------------- mark_resuming（W24-E resume 扩展）
def test_mark_resuming_interrupted_and_failed_to_running(tmp_path: Path):
    """可续跑态复位：interrupted / failed → running，error 同步清空（非终态口径）。"""
    store = _make_store(tmp_path)
    _create(store, "m1", status="interrupted", error="服务重启中断（阶段进度已保留，可续跑）")
    assert store.mark_resuming("m1") is True
    m1 = store.get("m1")
    assert m1["status"] == "running"
    assert m1["error"] is None  # 复位同时清错，与 set_status 非终态强制 error=None 同口径

    _create(store, "m2", status="failed", error="boom", created_at="2026-01-02T00:00:00")
    assert store.mark_resuming("m2") is True
    m2 = store.get("m2")
    assert m2["status"] == "running"
    assert m2["error"] is None


def test_mark_resuming_keeps_stage_done_progress(tmp_path: Path):
    """复位不动阶段进度：stage_done 原样保留（续跑判定仍以它为依据）。"""
    store = _make_store(tmp_path)
    _create(store, "m3", status="interrupted")
    store.record_stage_done("m3", "ingest")
    store.record_stage_done("m3", "index")
    assert store.mark_resuming("m3") is True
    assert store.get_stage_done("m3") == ["ingest", "index"]


def test_mark_resuming_rejects_other_states_and_missing(tmp_path: Path):
    """非法状态拒绝：queued/running/done 原样保留；任务不存在返回 False。"""
    store = _make_store(tmp_path)
    for status in ("queued", "running", "done"):
        _create(store, f"rej-{status}", status=status)
        assert store.mark_resuming(f"rej-{status}") is False
        assert store.get(f"rej-{status}")["status"] == status  # 状态未被改动
    assert store.mark_resuming("ghost") is False
