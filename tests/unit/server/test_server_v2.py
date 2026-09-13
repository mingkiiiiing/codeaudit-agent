"""W8-A1 API 契约 v2.0 增量端点自测：health、任务列表、zip 上传、DELETE、
understand、refactors、SPA 托管与 CORS。

风格对齐既有测试：假流水线注入（monkeypatch server.app.run_audit）、直接注入
共享任务表、零网络；上传用 zipfile 真造小 zip 并重定向 work_root 到 tmp_path。
"""

from __future__ import annotations

import asyncio
import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from audit import __version__
from audit.config import AuditConfig
from audit.models import (
    ArchitectureCard,
    AuditReport,
    Category,
    Issue,
    RefactorProposal,
    Severity,
)
from server import app as server_app


@pytest.fixture(autouse=True)
def clean_audits():
    """每个用例前后清空共享任务表，避免串扰。"""
    server_app.AUDITS.clear()
    yield
    server_app.AUDITS.clear()


@pytest.fixture
def fake_pipeline(monkeypatch):
    """把 server.app.run_audit 替换为返回预填报告（无架构/无重构方案）的假协程。"""
    calls: list = []

    async def fake_run_audit(config, emitter):
        calls.append(config)
        await emitter({"type": "progress", "stage": "ingest", "message": "假阶段开始"})
        return AuditReport(
            audit_id="v2test01",
            project_name="demo_proj",
            languages={"python": 100.0},
            loc=100,
            health_score=90.0,
            summary={"critical": 0, "high": 0, "medium": 0, "low": 0},
            issues=[
                Issue(
                    id="ISS-0001",
                    category=Category.BUG,
                    severity=Severity.LOW,
                    title="占位",
                    file="main.py",
                )
            ],
        )

    monkeypatch.setattr(server_app, "run_audit", fake_run_audit)
    return calls


@pytest.fixture
def work_root_tmp(monkeypatch, tmp_path):
    """把 from_env 的 work_root 重定向到 tmp_path，上传落盘不污染仓库工作区。"""
    real_from_env = AuditConfig.from_env.__func__

    def patched(cls, source_path=None, **overrides):
        overrides.setdefault("work_root", str(tmp_path))
        return real_from_env(cls, source_path=source_path, **overrides)

    monkeypatch.setattr(AuditConfig, "from_env", classmethod(patched))
    return tmp_path


@pytest.fixture
def client() -> TestClient:
    return TestClient(server_app.create_app())


def _wait_status(client: TestClient, audit_id: str, status: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    data: dict = {}
    while time.time() < deadline:
        data = client.get(f"/api/audits/{audit_id}").json()
        if data["status"] == status:
            return data
        time.sleep(0.05)
    raise AssertionError(f"等待状态 {status} 超时，当前：{data}")


def _wait_running(audit_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        entry = server_app.AUDITS.get(audit_id)
        if entry and entry["status"] == "running":
            return entry
        time.sleep(0.02)
    raise AssertionError(f"任务未进入 running，当前表：{server_app.AUDITS.get(audit_id)}")


def _make_zip_bytes(content: str = "x = 1\n") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mini_app/main.py", content)
    return buf.getvalue()


def _done_entry(report: AuditReport | None) -> dict:
    return {"status": "done", "report": report, "events": [], "error": None, "config": None}


def _running_entry() -> dict:
    return {"status": "running", "report": None, "events": [], "error": None, "config": None}


def _minimal_report(audit_id: str = "v2rep01") -> AuditReport:
    return AuditReport(
        audit_id=audit_id,
        project_name="demo_proj",
        languages={"python": 100.0},
        loc=10,
        health_score=99.0,
        summary={"critical": 0, "high": 0, "medium": 0, "low": 0},
        issues=[],
    )


# ---------------------------------------------------------------- /api/health


def test_health_empty_table(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "version": __version__,
        "audits": {"active": 0, "total": 0},
    }


def test_health_counts_active_and_total(client):
    server_app.AUDITS["run1"] = _running_entry()
    server_app.AUDITS["queue1"] = {"status": "queued", "report": None, "events": [], "error": None}
    server_app.AUDITS["done1"] = _done_entry(None)
    data = client.get("/api/health").json()
    assert data["audits"] == {"active": 2, "total": 3}


# ---------------------------------------------------------------- GET /api/audits


def test_list_audits_empty(client):
    assert client.get("/api/audits").json() == {"total": 0, "audits": []}


def test_list_audits_newest_first_with_pagination(fake_pipeline, client, tmp_path):
    id1 = client.post("/api/audits", json={"source_path": str(tmp_path)}).json()["audit_id"]
    id2 = client.post("/api/audits", json={"source_path": str(__file__)}).json()["audit_id"]

    data = client.get("/api/audits").json()
    assert data["total"] == 2
    assert [a["audit_id"] for a in data["audits"]] == [id2, id1]  # 新→旧
    item = data["audits"][0]
    assert set(item) == {
        "audit_id",
        "status",
        "created_at",
        "source_path",
        "do_fix",
        "do_tests",
        "error",
    }
    assert "task" not in item and "config" not in item and "events" not in item
    assert item["created_at"]  # ISO 串非空
    assert item["status"] == "done" and item["error"] is None

    # 分页：limit=1&offset=1 取到较旧的 id1，total 不变
    page = client.get("/api/audits", params={"limit": 1, "offset": 1}).json()
    assert page["total"] == 2
    assert [a["audit_id"] for a in page["audits"]] == [id1]


def test_list_audits_invalid_pagination_returns_400(client):
    assert client.get("/api/audits", params={"limit": "abc"}).status_code == 400
    assert client.get("/api/audits", params={"limit": -1}).status_code == 400
    assert client.get("/api/audits", params={"offset": -1}).status_code == 400


# ---------------------------------------------------------------- GET /api/audits/{id} 增补字段


def test_get_audit_detail_includes_v2_fields(fake_pipeline, client, tmp_path):
    audit_id = client.post(
        "/api/audits", json={"source_path": str(tmp_path), "do_fix": True, "do_tests": True}
    ).json()["audit_id"]
    data = _wait_status(client, audit_id, "done")
    assert data["created_at"] and "T" in data["created_at"]
    assert data["source_path"] == str(tmp_path)
    assert data["do_fix"] is True
    assert data["do_tests"] is True
    assert "task" not in data  # 内部键不得泄漏


# ---------------------------------------------------------------- POST /api/audits/upload


def test_upload_zip_creates_audit_and_persists_file(fake_pipeline, work_root_tmp, client):
    zip_bytes = _make_zip_bytes()
    resp = client.post(
        "/api/audits/upload",
        files={"file": ("src.zip", zip_bytes, "application/zip")},
        data={"do_fix": "true"},
    )
    assert resp.status_code == 200
    audit_id = resp.json()["audit_id"]
    assert audit_id

    # 落盘 <work_root>/uploads/<audit_id>.zip
    stored = work_root_tmp / "uploads" / f"{audit_id}.zip"
    assert stored.is_file()
    assert stored.read_bytes() == zip_bytes

    # 假流水线收到的 config 指向落盘 zip，开关透传
    data = _wait_status(client, audit_id, "done")
    config = fake_pipeline[0]
    assert config.source_path == str(stored)
    assert config.do_fix is True and config.do_tests is False
    assert data["source_path"] == str(stored)
    entry = server_app.AUDITS[audit_id]
    assert entry["do_fix"] is True and entry["created_at"]


def test_upload_rejects_non_zip_extension(client):
    resp = client.post(
        "/api/audits/upload",
        files={"file": ("src.tar", b"not a zip", "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert ".zip" in resp.json()["detail"]


def test_upload_rejects_fake_zip_content(client):
    resp = client.post(
        "/api/audits/upload",
        files={"file": ("fake.zip", b"this is definitely not a zip", "application/zip")},
    )
    assert resp.status_code == 400
    assert "zip" in resp.json()["detail"]


def test_upload_too_large_returns_413(monkeypatch, client):
    monkeypatch.setattr(server_app, "_UPLOAD_MAX_BYTES", 10)  # zip 头部即超限
    resp = client.post(
        "/api/audits/upload",
        files={"file": ("big.zip", _make_zip_bytes(), "application/zip")},
    )
    assert resp.status_code == 413


# ---------------------------------------------------------------- DELETE /api/audits/{id}


def test_delete_unknown_returns_404(client):
    resp = client.delete("/api/audits/no-such-id")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "任务不存在：no-such-id"


def test_delete_done_returns_204_and_removes_entry(fake_pipeline, client, tmp_path):
    audit_id = client.post("/api/audits", json={"source_path": str(tmp_path)}).json()["audit_id"]
    _wait_status(client, audit_id, "done")
    resp = client.delete(f"/api/audits/{audit_id}")
    assert resp.status_code == 204
    assert audit_id not in server_app.AUDITS
    assert client.get(f"/api/audits/{audit_id}").status_code == 404
    # 再删一次：已不存在 → 404
    assert client.delete(f"/api/audits/{audit_id}").status_code == 404


def test_delete_running_cancels_task_and_lands_failed(monkeypatch, tmp_path):
    async def never_finish(config, emitter):
        await asyncio.Event().wait()  # 永久等待，直到被取消

    monkeypatch.setattr(server_app, "run_audit", never_finish)
    # 以上下文管理器持有一个跨请求存活的 portal：否则每个请求各自起停事件循环，
    # POST 返回时挂起的后台任务就会被清理取消，无法构造"运行中"前置状态。
    with TestClient(server_app.create_app()) as client:
        audit_id = client.post(
            "/api/audits", json={"source_path": str(tmp_path)}
        ).json()["audit_id"]
        entry = _wait_running(audit_id)
        task = entry["task"]
        assert task is not None

        resp = client.delete(f"/api/audits/{audit_id}")
        assert resp.status_code == 204
        assert audit_id not in server_app.AUDITS  # 条目已移除
        # 被移除的条目（以引用观察）落 failed + 取消 error，且不被取消异常回写覆盖
        assert entry["status"] == "failed"
        assert entry["error"] == "任务已取消"
        # 后台任务确实被取消
        deadline = time.time() + 5
        while not task.done() and time.time() < deadline:
            time.sleep(0.02)
        assert task.cancelled() is True


# ---------------------------------------------------------------- /understand


def test_understand_none_when_no_architecture(client):
    server_app.AUDITS["und1"] = _done_entry(_minimal_report())
    resp = client.get("/api/audits/und1/understand")
    assert resp.status_code == 200
    assert resp.json() == {"architecture": None}


def test_understand_returns_card_dict(client):
    card = ArchitectureCard(
        text="分层架构",
        tech_stack=["python"],
        modules={"app": "业务逻辑", "server": "Web 服务"},
        hotspots=["app/core.py"],
    )
    report = _minimal_report()
    report.architecture = card
    server_app.AUDITS["und2"] = _done_entry(report)
    data = client.get("/api/audits/und2/understand").json()
    assert data["architecture"] == {
        "text": "分层架构",
        "tech_stack": ["python"],
        "modules": {"app": "业务逻辑", "server": "Web 服务"},
        "hotspots": ["app/core.py"],
    }


@pytest.mark.parametrize("suffix", ["understand", "refactors"])
def test_understand_refactors_404_semantics(client, suffix):
    resp = client.get(f"/api/audits/no-such-id/{suffix}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "任务不存在：no-such-id"
    server_app.AUDITS["runn1"] = _running_entry()
    resp = client.get(f"/api/audits/runn1/{suffix}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "任务未完成：running"


# ---------------------------------------------------------------- /refactors


def test_refactors_lists_proposals_with_contract_fields(client):
    p1 = RefactorProposal(
        id="RF-0001",
        title="拆分长函数",
        target="app/services/orders.py::create",
        kind="decompose",
        rationale="圈复杂度过高",
        steps=["提取参数对象", "拆分分支"],
        benefits="可读性与可测性提升",
        related_issues=["ISS-0001"],
        source="heuristic+llm",
        confidence=0.9,
    )
    p2 = RefactorProposal(id="RF-0002", title="去重", confidence=0.5)
    report = _minimal_report()
    report.refactor_proposals = [p1, p2]
    server_app.AUDITS["ref1"] = _done_entry(report)

    resp = client.get("/api/audits/ref1/refactors")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert [p["id"] for p in data["proposals"]] == ["RF-0001", "RF-0002"]
    assert set(data["proposals"][0]) == {
        "id",
        "title",
        "target",
        "kind",
        "rationale",
        "steps",
        "benefits",
        "related_issues",
        "source",
        "confidence",
    }
    assert data["proposals"][0]["steps"] == ["提取参数对象", "拆分分支"]
    assert data["proposals"][0]["related_issues"] == ["ISS-0001"]
    assert data["proposals"][1]["kind"] == "other"


# ---------------------------------------------------------------- SPA 静态托管


def test_root_falls_back_to_demo_when_no_dist(tmp_path, monkeypatch):
    monkeypatch.setattr(server_app, "_SPA_DIST", tmp_path / "absent-dist")
    client = TestClient(server_app.create_app())
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "EventSource" in resp.text  # 单文件演示页


def test_spa_dist_served_with_catch_all_and_api_passthrough(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>SPA-SHELL-v8</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log('spa');", encoding="utf-8")
    monkeypatch.setattr(server_app, "_SPA_DIST", dist)
    client = TestClient(server_app.create_app())

    # 根路径 → SPA index
    resp = client.get("/")
    assert resp.status_code == 200 and "SPA-SHELL-v8" in resp.text
    # SPA 客户端路由兜底（非文件路径回 index）
    resp = client.get("/audits/xyz")
    assert resp.status_code == 200 and "SPA-SHELL-v8" in resp.text
    # dist 内真实静态文件直出
    resp = client.get("/index.html")
    assert resp.status_code == 200 and "SPA-SHELL-v8" in resp.text
    # /assets/* 由挂载的 StaticFiles 服务
    resp = client.get("/assets/app.js")
    assert resp.status_code == 200 and "console.log" in resp.text
    # API 前缀不被 catch-all 吞：未知 API 路径 404
    resp = client.get("/api/nonexistent")
    assert resp.status_code == 404
    # 目录穿越被拒
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    resp = client.get("/..%2Fsecret.txt")
    assert resp.status_code == 404


# ---------------------------------------------------------------- CORS


def test_cors_preflight_allows_dev_origins_only(client):
    for origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
        resp = client.options(
            "/api/audits",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
        )
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == origin
    # 非白名单源：不回 allow-origin 头
    resp = client.options(
        "/api/audits",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.headers.get("access-control-allow-origin") is None
