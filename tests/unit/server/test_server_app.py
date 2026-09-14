"""T5 FastAPI 服务自测：任务创建/查询/SSE 事件流/报告下载/演示页。

注入假流水线（monkeypatch server.app.run_audit），禁止真实网络与固定端口。
W11 适配：任务表为 TaskStore（conftest autouse store fixture 注入 tmp 库），
后台任务在独立线程执行——需要等待任务完成/事件流收尾的用例一律以上下文
管理器持有 TestClient（跨请求存活的 portal），断言为条件等待（无固定 sleep）。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from audit.models import AuditReport, Category, Issue, Severity
from server import app as server_app


@pytest.fixture
def fake_pipeline(monkeypatch):
    """把 server.app.run_audit 替换为返回预填报告的假协程。"""
    calls: list = []

    async def fake_run_audit(config, emitter):
        calls.append(config)
        await emitter({"type": "progress", "stage": "ingest", "message": "假阶段开始"})
        await emitter({"type": "progress", "stage": "detect", "message": "发现 1 个问题", "total": 1})
        report = AuditReport(
            audit_id="srvtest01",
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
        return report

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


def _wait_entry_status(audit_id: str, status: str, timeout: float = 5.0) -> dict:
    """条件等待注入 store 中某任务进入指定状态（轮询 0.02s，非固定 sleep 同步）。"""
    store = server_app._get_store()
    deadline = time.time() + timeout
    entry: dict | None = None
    while time.time() < deadline:
        entry = store.get(audit_id)
        if entry is not None and entry["status"] == status:
            return entry
        time.sleep(0.02)
    raise AssertionError(f"等待状态 {status} 超时，当前：{entry}")


def test_create_audit_missing_body_path_returns_400():
    client = TestClient(server_app.create_app())
    resp = client.post("/api/audits", json={"source_path": ""})
    assert resp.status_code == 400


def test_create_audit_nonexistent_path_returns_400():
    client = TestClient(server_app.create_app())
    resp = client.post("/api/audits", json={"source_path": "Z:/definitely/not/exist"})
    assert resp.status_code == 400
    assert "不存在" in resp.json()["detail"]


def test_unknown_audit_returns_404():
    client = TestClient(server_app.create_app())
    assert client.get("/api/audits/no-such-id").status_code == 404
    assert client.get("/api/audits/no-such-id/events").status_code == 404
    assert client.get("/api/audits/no-such-id/report").status_code == 404


def test_full_lifecycle_create_done_report(fake_pipeline, tmp_path):
    """创建 → 轮询完成 → 取 json/md/html 报告全链路。"""
    with TestClient(server_app.create_app()) as client:
        resp = client.post("/api/audits", json={"source_path": str(tmp_path), "do_fix": True})
        assert resp.status_code == 200
        audit_id = resp.json()["audit_id"]
        assert audit_id

        # 状态推进：running → done
        data = _wait_status(client, audit_id, "done")
        assert data["error"] is None
        assert data["report"]["audit_id"] == "srvtest01"
        assert data["report"]["health_score"] == 88.5
        assert data["report"]["issues"][0]["file"] == "app/services/orders.py"

        # 配置透传
        assert fake_pipeline[0].do_fix is True
        assert fake_pipeline[0].source_path == str(tmp_path)

        # 事件已落 store（emitter 收集）
        events = [e for _seq, e in server_app._get_store().get_events(audit_id)]
        assert any(e["stage"] == "detect" for e in events)

        # 三种格式报告
        r_json = client.get(f"/api/audits/{audit_id}/report", params={"format": "json"})
        assert r_json.status_code == 200
        assert r_json.json()["summary"]["high"] == 1

        r_md = client.get(f"/api/audits/{audit_id}/report", params={"format": "md"})
        assert r_md.status_code == 200
        assert "text/markdown" in r_md.headers["content-type"]
        assert "健康分" in r_md.text and "ISS-0001" in r_md.text

        r_html = client.get(f"/api/audits/{audit_id}/report", params={"format": "html"})
        assert r_html.status_code == 200
        assert "<html" in r_html.text and "ISS-0001" in r_html.text

        assert (
            client.get(f"/api/audits/{audit_id}/report", params={"format": "xml"}).status_code
            == 400
        )


def test_report_not_ready_returns_404(monkeypatch, tmp_path):
    """任务未完成时取报告 → 404（W11：假流水线挂起在 threading.Event 上，可控放行）。"""
    release = threading.Event()

    async def never_finish(config, emitter):
        # 任务线程内可控挂起（有界等待）：不 emit 事件、不返回，
        # 用例断言完成后放行，线程尽快结束不拖累会话
        release.wait(timeout=10)

    monkeypatch.setattr(server_app, "run_audit", never_finish)
    with TestClient(server_app.create_app()) as client:
        try:
            audit_id = client.post("/api/audits", json={"source_path": str(tmp_path)}).json()[
                "audit_id"
            ]
            _wait_entry_status(audit_id, "running")
            resp = client.get(f"/api/audits/{audit_id}/report")
            assert resp.status_code == 404
            assert "任务未完成" in resp.json()["detail"]
        finally:
            release.set()


def test_sse_replays_events_then_done(fake_pipeline, tmp_path):
    """SSE：回放历史事件 → 追加 done 事件 → 关闭。"""
    with TestClient(server_app.create_app()) as client:
        audit_id = client.post("/api/audits", json={"source_path": str(tmp_path)}).json()[
            "audit_id"
        ]

        payloads: list[dict] = []
        with client.stream("GET", f"/api/audits/{audit_id}/events") as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    payloads.append(json.loads(line[len("data:") :].strip()))
                if payloads and payloads[-1].get("type") == "done":
                    break

        types_seq = [p.get("type") for p in payloads]
        assert types_seq[-1] == "done"
        progress = [p for p in payloads if p.get("type") == "progress"]
        assert [p["stage"] for p in progress] == ["ingest", "detect"]


def test_sse_emits_done_for_failed_task(monkeypatch, tmp_path):
    """任务失败：事件流仍以 done 收尾（含 failed 状态可查）。"""

    async def failing(config, emitter):
        raise RuntimeError("boom")

    monkeypatch.setattr(server_app, "run_audit", failing)
    with TestClient(server_app.create_app()) as client:
        audit_id = client.post("/api/audits", json={"source_path": str(tmp_path)}).json()[
            "audit_id"
        ]
        data = _wait_status(client, audit_id, "failed")
        assert "boom" in data["error"]

        payloads: list[dict] = []
        with client.stream("GET", f"/api/audits/{audit_id}/events") as resp:
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    payloads.append(json.loads(line[len("data:") :].strip()))
                if payloads and payloads[-1].get("type") == "done":
                    break
        assert payloads[-1]["type"] == "done"


def test_index_page_served(tmp_path, monkeypatch):
    """无 SPA 构建产物时根路径回退单文件演示页（显式指向不存在的 dist，保证确定性）。"""
    monkeypatch.setattr(server_app, "_SPA_DIST", tmp_path / "no-dist")
    client = TestClient(server_app.create_app())
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "EventSource" in resp.text  # 演示页使用 SSE 订阅进度


# ---------------------------------------------------------------- R1-6 / R1-7 / R1-30 回归


def test_audits_prune_evicts_oldest_terminal_entries(fake_pipeline, tmp_path):
    """R1-6：任务表超过上限时经 store.prune 淘汰最旧的 done/failed 项（running 不动）。"""
    store = server_app._get_store()
    for i in range(server_app._AUDITS_MAX):
        store.create(
            f"old{i:02d}",
            status="done",
            created_at=f"2026-01-01T00:{i:02d}:00+08:00",
            source_path="seed",
            do_fix=False,
            do_tests=False,
        )
    with TestClient(server_app.create_app()) as client:
        audit_id = client.post("/api/audits", json={"source_path": str(tmp_path)}).json()[
            "audit_id"
        ]

        assert store.list(0, 0)[0] == server_app._AUDITS_MAX
        assert store.get("old00") is None  # 最旧 done 项被淘汰
        assert store.get("old01") is not None
        assert store.get(audit_id) is not None

        # running/queued 不被淘汰（再注入 50 个 running 行后 prune：只动终态）
        for i in range(server_app._AUDITS_MAX):
            store.create(
                f"run{i:02d}",
                status="running",
                created_at=f"2026-02-01T00:{i:02d}:00+08:00",
                source_path="seed",
                do_fix=False,
                do_tests=False,
            )
        store.prune(server_app._AUDITS_MAX)
        assert all(store.get(f"run{i:02d}") is not None for i in range(server_app._AUDITS_MAX))


def test_background_task_refs_discarded_on_done(fake_pipeline, tmp_path):
    """R1-7：create_task 引用被 _TASKS 持有，任务结束后 discard（无泄漏）。"""
    with TestClient(server_app.create_app()) as client:
        audit_id = client.post("/api/audits", json={"source_path": str(tmp_path)}).json()[
            "audit_id"
        ]
        _wait_status(client, audit_id, "done")
        deadline = time.time() + 5
        while server_app._TASKS and time.time() < deadline:
            time.sleep(0.02)  # 等 done callback 执行
        assert server_app._TASKS == set()


def test_report_out_dir_isolated_per_audit(fake_pipeline, tmp_path):
    """R1-30：未显式配置 out_dir 时，报告目录按 audit_id 隔离，不互相覆盖。"""
    with TestClient(server_app.create_app()) as client:
        id1 = client.post("/api/audits", json={"source_path": str(tmp_path)}).json()["audit_id"]
        id2 = client.post("/api/audits", json={"source_path": str(tmp_path)}).json()["audit_id"]
        # 条件等待两个任务线程都进入假流水线（W11 线程形态，启动无固定时序）
        deadline = time.time() + 5
        while len(fake_pipeline) < 2 and time.time() < deadline:
            time.sleep(0.02)
        out1 = fake_pipeline[0].out_dir
        out2 = fake_pipeline[1].out_dir
        assert id1 in out1 and id2 in out2
        assert out1 != out2


# ---------------------------------------------------------------- R4-9 回归


def test_run_audit_sync_cancelled_lands_failed(monkeypatch):
    """R4-9（W11 线程形态）：CancelledError 等异常路径也落终态 failed（任务表不残留僵尸项）。

    _run_audit_sync 是任务线程主体：直接以注入 store 的行调用，验证兜底落库。
    """
    store = server_app._get_store()

    async def cancelled(config, emitter):
        raise asyncio.CancelledError()

    monkeypatch.setattr(server_app, "run_audit", cancelled)
    store.create(
        "cancel01",
        status="running",
        created_at="2026-01-01T00:00:00+08:00",
        source_path="seed",
        do_fix=False,
        do_tests=False,
    )
    server_app._run_audit_sync("cancel01", None)
    entry = store.get("cancel01")
    assert entry is not None
    assert entry["status"] == "failed"
    assert entry["error"] is not None
    assert "CancelledError" in entry["error"]
