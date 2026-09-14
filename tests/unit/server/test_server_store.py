"""W11-A2 新增用例：持久化 + 线程池执行 + 协作式取消形态下的服务端行为（契约 docs/16）。

- 重启恢复（§1.1）：create_app 启动 sweep 把遗留 queued/running 置 failed
  （error=服务重启中断），done 任务带报告重启后仍可查询与下载；
- 协作式取消（§1.2）：DELETE（204）移除表行即取消信号，任务线程在下一个
  emitter 事件边界停止，无 running 残留；
- 任务完成落库：报告经 store.set_report 序列化，get_report 反序列化正确、status=done；
- 上传建任务触发容量淘汰：store.prune 只淘汰终态（R1-6 语义迁移到 store 口径）；
- 幽灵静默：DELETE 竞态下任务线程的收尾写不报错、不复活已删行。

风格对齐既有用例：假流水线注入（monkeypatch server_app.run_audit）、条件等待
（deadline 轮询，无固定 sleep 同步）、挂起一律挂在 threading.Event 上并在用例
结束前放行（任务线程不可 cancel），以上下文管理器持有 TestClient。
"""

from __future__ import annotations

import io
import threading
import time
import zipfile

from fastapi.testclient import TestClient

import pytest

from audit.models import AuditReport, Category, Issue, Severity
from server import app as server_app


def _minimal_report(audit_id: str = "storep01") -> AuditReport:
    return AuditReport(
        audit_id=audit_id,
        project_name="demo_proj",
        languages={"python": 100.0},
        loc=10,
        health_score=99.0,
        summary={"critical": 0, "high": 0, "medium": 0, "low": 0},
        issues=[],
    )


@pytest.fixture
def fake_pipeline(monkeypatch):
    """把 server_app.run_audit 替换为返回预填报告的假协程。"""
    calls: list = []

    async def fake_run_audit(config, emitter):
        calls.append(config)
        await emitter({"type": "progress", "stage": "ingest", "message": "假阶段开始"})
        await emitter({"type": "progress", "stage": "detect", "message": "发现 1 个问题", "total": 1})
        return AuditReport(
            audit_id="storep02",
            project_name="demo_proj",
            languages={"python": 100.0},
            loc=142,
            health_score=88.5,
            summary={"critical": 0, "high": 1, "medium": 0, "low": 0},
            issues=[
                Issue(
                    id="ISS-0001",
                    category=Category.BUG,
                    severity=Severity.HIGH,
                    title="未判空",
                    file="app/services/orders.py",
                    line_start=10,
                    line_end=11,
                )
            ],
        )

    monkeypatch.setattr(server_app, "run_audit", fake_run_audit)
    return calls


def _wait_status(client: TestClient, audit_id: str, status: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    data: dict = {}
    while time.time() < deadline:
        data = client.get(f"/api/audits/{audit_id}").json()
        if data["status"] == status:
            return data
        time.sleep(0.05)
    raise AssertionError(f"等待状态 {status} 超时，当前：{data}")


def _make_zip_bytes(content: str = "x = 1\n") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mini_app/main.py", content)
    return buf.getvalue()


# ---------------------------------------------------------------- a) 重启恢复


def test_startup_sweep_recovers_done_and_fails_interrupted(store, seed_task):
    """重启恢复：done 任务带报告重启后可查可下载；遗留 queued/running 被 sweep 为 failed。

    create_app 的 lifespan 在 TestClient 上下文管理器进入时执行 sweep_interrupted。
    """
    seed_task("done01", status="done", report=_minimal_report("done01"))
    seed_task("run01", status="running", created_at="2026-01-01T00:00:02+08:00")
    seed_task("q01", status="queued", created_at="2026-01-01T00:00:03+08:00")

    with TestClient(server_app.create_app()) as client:
        # 终态任务与报告重启后仍可查询与下载（新能力）
        data = client.get("/api/audits/done01").json()
        assert data["status"] == "done"
        assert data["error"] is None
        assert data["report"]["project_name"] == "demo_proj"
        r = client.get("/api/audits/done01/report", params={"format": "json"})
        assert r.status_code == 200
        assert r.json()["health_score"] == 99.0

        # 遗留非终态任务被启动 sweep 置 failed（error=服务重启中断）
        for aid in ("run01", "q01"):
            d = client.get(f"/api/audits/{aid}").json()
            assert d["status"] == "failed"
            assert d["error"] == "服务重启中断"
        # 列表仍可见全部 3 个任务
        assert client.get("/api/audits").json()["total"] == 3


# ---------------------------------------------------------------- b) 协作式取消


def test_delete_running_cooperative_cancel_stops_thread_at_event_boundary(store, monkeypatch, tmp_path):
    """DELETE（204）→ 行消失即取消信号 → 线程在 emitter 边界停止 → 无行无 running 残留。"""
    release = threading.Event()
    thread_hit_boundary = threading.Event()

    async def two_phase_run(config, emitter):
        try:
            await emitter({"type": "progress", "stage": "ingest", "message": "第一段"})
            release.wait(timeout=10)  # 可控挂起（任务线程内）
            await emitter({"type": "progress", "stage": "detect", "message": "取消后不应写入"})
        finally:
            thread_hit_boundary.set()

    monkeypatch.setattr(server_app, "run_audit", two_phase_run)
    with TestClient(server_app.create_app()) as client:
        audit_id = client.post("/api/audits", json={"source_path": str(tmp_path)}).json()[
            "audit_id"
        ]
        # 第一段事件已落库、任务已 running
        deadline = time.time() + 10
        while time.time() < deadline:
            events = [e for _seq, e in store.get_events(audit_id)]
            if any(e.get("stage") == "ingest" for e in events):
                break
            time.sleep(0.02)
        assert [e.get("stage") for _seq, e in store.get_events(audit_id)] == ["ingest"]

        resp = client.delete(f"/api/audits/{audit_id}")
        assert resp.status_code == 204
        assert store.get(audit_id) is None  # 行消失即取消信号

        release.set()
        assert thread_hit_boundary.wait(timeout=10)  # 线程抵达下一个事件边界后退出
        # 等外层 task 完成（= 幽灵回写已发生），确认无 running 残留
        deadline = time.time() + 10
        while audit_id in server_app._RUNNING and time.time() < deadline:
            time.sleep(0.02)
        assert audit_id not in server_app._RUNNING
        # 取消路径的 failed 回写被幽灵静默：任务行与其事件保持删除状态
        assert store.get(audit_id) is None
        assert store.get_events(audit_id) == []
        assert client.get(f"/api/audits/{audit_id}").status_code == 404


def test_run_audit_sync_ghost_silent_after_delete(store, monkeypatch):
    """DELETE 竞态兜底：表行已删除时 _run_audit_sync 的收尾写静默（不报错、不复活行）。"""

    async def quick(config, emitter):
        return None  # 无事件、无报告的快速假流水线

    monkeypatch.setattr(server_app, "run_audit", quick)
    store.create(
        "ghost01",
        status="running",
        created_at="2026-01-01T00:00:00+08:00",
        source_path="seed",
        do_fix=False,
        do_tests=False,
    )
    store.delete("ghost01")
    # 收尾 set_report / set_status 均为幽灵更新（行不存在 → 静默），不得抛错
    server_app._run_audit_sync("ghost01", None)
    assert store.get("ghost01") is None


# ---------------------------------------------------------------- c) 任务完成落库


def test_done_report_persisted_in_store(fake_pipeline, store, tmp_path):
    """任务完成落库：报告经 store.set_report 序列化，get_report 反序列化正确、status=done。"""
    with TestClient(server_app.create_app()) as client:
        audit_id = client.post("/api/audits", json={"source_path": str(tmp_path)}).json()[
            "audit_id"
        ]
        data = _wait_status(client, audit_id, "done")

        entry = store.get(audit_id)
        assert entry is not None
        assert entry["status"] == "done"
        assert entry["error"] is None

        report = store.get_report(audit_id)
        assert isinstance(report, AuditReport)
        assert report.audit_id == "storep02"
        assert report.health_score == 88.5
        assert [i.file for i in report.issues] == ["app/services/orders.py"]
        # 详情端点的 report 即反序列化产物
        assert data["report"]["summary"]["high"] == 1
        # 事件同样持久化在 store
        stages = [e.get("stage") for _seq, e in store.get_events(audit_id)]
        assert stages == ["ingest", "detect"]


# ---------------------------------------------------------------- d) 上传后容量淘汰


def test_upload_triggers_prune_evicting_oldest_terminal(monkeypatch, store, seed_task, work_root_tmp):
    """上传建任务后触发 store.prune：只淘汰最旧终态行，新任务行不受影响（R1-6 迁移口径）。"""
    monkeypatch.setattr(server_app, "_AUDITS_MAX", 2)
    seed_task("old-a", status="done", created_at="2026-01-01T00:00:01+08:00")
    seed_task("old-b", status="done", created_at="2026-01-01T00:00:02+08:00")

    client = TestClient(server_app.create_app())
    resp = client.post(
        "/api/audits/upload",
        files={"file": ("src.zip", _make_zip_bytes(), "application/zip")},
    )
    assert resp.status_code == 200
    audit_id = resp.json()["audit_id"]

    total, _items = store.list(0, 0)
    assert total == 2  # 容量上限生效（淘汰 1 个最旧终态）
    assert store.get("old-a") is None  # 最旧终态行被淘汰
    assert store.get("old-b") is not None
    assert store.get(audit_id) is not None  # 新任务行（queued）不被淘汰
