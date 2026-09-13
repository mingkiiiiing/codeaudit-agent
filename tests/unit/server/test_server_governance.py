"""W10-A1 服务治理自测：F2 流式上传（分块读取 / 超限 413 即停 / 失败路径零残留）
与 F5 准入控制（_RUN_GATE 排队语义、非终态上限 429、DELETE 排队任务取消不泄漏闸门）。

风格对齐既有用例：假流水线注入（monkeypatch server_app.run_audit）、条件等待
（deadline 轮询，无固定 sleep 同步）、work_root 重定向 tmp_path、全程离线。

注意：_RUN_GATE 的容量在创建时固化，测试排队语义时 monkeypatch 替换为新
Semaphore；被替换的闸门仅在单用例内使用，用例结束前所有任务均已落终态，
不会向模块级默认闸门泄漏计数。
"""

from __future__ import annotations

import asyncio
import io
import threading
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from audit.config import AuditConfig
from audit.models import AuditReport
from server import app as server_app


@pytest.fixture(autouse=True)
def clean_audits():
    """每个用例前后清空共享任务表，避免串扰。"""
    server_app.AUDITS.clear()
    yield
    server_app.AUDITS.clear()


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


def _minimal_report(audit_id: str = "govtest01") -> AuditReport:
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
    """把 server_app.run_audit 替换为返回预填报告的假协程（记录收到的 config）。"""
    calls: list = []

    async def fake_run_audit(config, emitter):
        calls.append(config)
        await emitter({"type": "progress", "stage": "ingest", "message": "假阶段开始"})
        return _minimal_report()

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
    """条件等待共享任务表中某任务进入指定状态（轮询 0.02s，非固定 sleep 同步）。"""
    deadline = time.time() + timeout
    entry: dict | None = None
    while time.time() < deadline:
        entry = server_app.AUDITS.get(audit_id)
        if entry is not None and entry["status"] == status:
            return entry
        time.sleep(0.02)
    raise AssertionError(f"等待状态 {status} 超时，当前：{entry}")


def _wait_task_done(task: asyncio.Task, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while not task.done() and time.time() < deadline:
        time.sleep(0.02)
    assert task.done(), "后台任务未按预期结束"


def _make_zip_bytes(content: str = "x = 1\n") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mini_app/main.py", content)
    return buf.getvalue()


def _uploads_files(work_root: Path) -> list[Path]:
    """列出 work_root/uploads 下残留的文件（目录不存在视为无残留）。"""
    uploads = work_root / "uploads"
    return list(uploads.iterdir()) if uploads.is_dir() else []


# ---------------------------------------------------------------- F5 env 上限解析


def test_env_limit_parsing_falls_back_to_defaults(monkeypatch):
    """上限 env 解析：合法值生效；缺失/非法/负数/低于下限均回落默认值。"""
    for raw, expected in [("4", 4), ("abc", 7), ("-1", 7), ("0", 7)]:
        monkeypatch.setenv("CODEAUDIT_TEST_LIMIT", raw)
        assert server_app._int_from_env("CODEAUDIT_TEST_LIMIT", 7, minimum=1) == expected
    monkeypatch.delenv("CODEAUDIT_TEST_LIMIT", raising=False)
    assert server_app._int_from_env("CODEAUDIT_TEST_LIMIT", 7, minimum=1) == 7

    # pending 下限与 running 绑定：低于 running 的取值回落默认
    monkeypatch.setenv("CODEAUDIT_TEST_PENDING", "2")
    assert server_app._int_from_env("CODEAUDIT_TEST_PENDING", 20, minimum=4) == 20
    monkeypatch.setenv("CODEAUDIT_TEST_PENDING", "5")
    assert server_app._int_from_env("CODEAUDIT_TEST_PENDING", 20, minimum=4) == 5


def test_module_limit_invariants():
    """模块级上限常量满足契约不变量：running ≥ 1，pending ≥ running。"""
    assert server_app._MAX_RUNNING_AUDITS >= 1
    assert server_app._MAX_PENDING_AUDITS >= server_app._MAX_RUNNING_AUDITS


# ---------------------------------------------------------------- F2 流式上传


def test_upload_streaming_413_stops_and_leaves_no_residue(monkeypatch, work_root_tmp, client):
    """F2：超限立即 413（monkeypatch 上限=10），磁盘与任务表均零残留。"""
    monkeypatch.setattr(server_app, "_UPLOAD_MAX_BYTES", 10)
    resp = client.post(
        "/api/audits/upload",
        files={"file": ("big.zip", _make_zip_bytes(), "application/zip")},
    )
    assert resp.status_code == 413
    assert "上传文件过大" in resp.json()["detail"]
    assert str(server_app._UPLOAD_MAX_BYTES) in resp.json()["detail"]
    # 半截 .part 文件与任务表条目都不残留
    assert _uploads_files(work_root_tmp) == []
    assert server_app.AUDITS == {}


def test_upload_multichunk_path_keeps_bytes_exact(monkeypatch, fake_pipeline, work_root_tmp, client):
    """F2：chunk 缩到 4 字节走多块路径，落盘字节仍与请求体逐字节一致且任务完成。"""
    monkeypatch.setattr(server_app, "_UPLOAD_CHUNK_BYTES", 4)
    zip_bytes = _make_zip_bytes("y = 2\n")
    resp = client.post(
        "/api/audits/upload",
        files={"file": ("src.zip", zip_bytes, "application/zip")},
    )
    assert resp.status_code == 200
    audit_id = resp.json()["audit_id"]
    stored = work_root_tmp / "uploads" / f"{audit_id}.zip"
    assert stored.is_file()
    assert stored.read_bytes() == zip_bytes
    _wait_status(client, audit_id, "done")
    assert fake_pipeline[0].source_path == str(stored)


def test_upload_non_zip_rejected_and_leaves_no_file(work_root_tmp, client):
    """F2：非法 zip 内容 400，落盘的临时文件被清理（既有 400 语义不变）。"""
    resp = client.post(
        "/api/audits/upload",
        files={"file": ("fake.zip", b"this is definitely not a zip", "application/zip")},
    )
    assert resp.status_code == 400
    assert "zip" in resp.json()["detail"]
    assert _uploads_files(work_root_tmp) == []
    assert server_app.AUDITS == {}


# ---------------------------------------------------------------- F5 pending 上限 429


def _never_finish_factory():
    """构造永不返回的假流水线（被取消时经 CancelledError 退出）。"""

    async def never_finish(config, emitter):
        await asyncio.Event().wait()

    return never_finish


def test_create_returns_429_when_pending_full(monkeypatch, tmp_path):
    """F5：非终态任务数达到上限后 POST /api/audits 返回 429；删除任务后闸口重开。"""
    monkeypatch.setattr(server_app, "_MAX_PENDING_AUDITS", 2)
    monkeypatch.setattr(server_app, "run_audit", _never_finish_factory())
    with TestClient(server_app.create_app()) as client:
        for _ in range(2):
            resp = client.post("/api/audits", json={"source_path": str(tmp_path)})
            assert resp.status_code == 200
        resp = client.post("/api/audits", json={"source_path": str(tmp_path)})
        assert resp.status_code == 429
        assert resp.json()["detail"] == "服务器并发审计数已达上限（2），请稍后重试"

        # 删除一个非终态任务 → active 降回 2 以下 → 可再次创建
        first_id = client.get("/api/audits").json()["audits"][-1]["audit_id"]
        assert client.delete(f"/api/audits/{first_id}").status_code == 204
        assert client.post("/api/audits", json={"source_path": str(tmp_path)}).status_code == 200


def test_upload_returns_429_when_pending_full(monkeypatch, work_root_tmp):
    """F5：pending 满时 POST /api/audits/upload 返回 429，且在任何落盘之前拒绝。"""
    monkeypatch.setattr(server_app, "_MAX_PENDING_AUDITS", 2)
    monkeypatch.setattr(server_app, "run_audit", _never_finish_factory())
    with TestClient(server_app.create_app()) as client:
        for _ in range(2):
            assert client.post("/api/audits", json={"source_path": str(work_root_tmp)}).status_code == 200
        resp = client.post(
            "/api/audits/upload",
            files={"file": ("src.zip", _make_zip_bytes(), "application/zip")},
        )
        assert resp.status_code == 429
        assert resp.json()["detail"] == "服务器并发审计数已达上限（2），请稍后重试"
        # 拒绝发生在读取/落盘之前：uploads 目录无任何残留
        assert _uploads_files(work_root_tmp) == []


# ---------------------------------------------------------------- F5 running 闸门排队


def test_max_running_gate_queues_excess_tasks(monkeypatch, tmp_path):
    """F5：max_running=1 时提交 3 个任务，恰好 1 running + 2 queued；放行后按 FIFO 依次完成。"""
    monkeypatch.setattr(server_app, "_MAX_RUNNING_AUDITS", 1)
    gate = asyncio.Semaphore(1)
    monkeypatch.setattr(server_app, "_RUN_GATE", gate)

    sources = []
    for i in range(3):
        p = tmp_path / f"proj_{i}.py"
        p.write_text(f"value = {i}\n", encoding="utf-8")
        sources.append(p)

    started: list[str] = []
    release = threading.Event()

    async def blocking_run(config, emitter):
        started.append(config.source_path)
        await emitter({"type": "progress", "stage": "ingest", "message": "假阶段开始"})
        # 条件等待：轮询测试线程置位的 threading.Event，保持事件循环可响应
        while not release.is_set():
            await asyncio.sleep(0.01)
        return _minimal_report()

    monkeypatch.setattr(server_app, "run_audit", blocking_run)
    with TestClient(server_app.create_app()) as client:
        ids = [
            client.post("/api/audits", json={"source_path": str(p)}).json()["audit_id"]
            for p in sources
        ]
        _wait_entry_status(ids[0], "running")
        for queued_id in ids[1:]:
            _wait_entry_status(queued_id, "queued")
        statuses = [server_app.AUDITS[aid]["status"] for aid in ids]
        assert statuses.count("running") == 1
        assert statuses.count("queued") == 2
        assert len(started) == 1

        # 放行 → 排队任务按提交序（信号量 FIFO）依次执行并全部完成
        release.set()
        for aid in ids:
            data = _wait_status(client, aid, "done")
            assert data["error"] is None
        assert started == [str(p) for p in sources]
        assert gate._value == 1  # 严格配对，闸门计数无泄漏


def test_delete_queued_task_cancels_cleanly_without_gate_leak(monkeypatch, tmp_path):
    """F5：DELETE 排队中任务 → 立即落「任务已取消」且任务被取消、闸门计数不泄漏。"""
    monkeypatch.setattr(server_app, "_MAX_RUNNING_AUDITS", 1)
    gate = asyncio.Semaphore(1)
    monkeypatch.setattr(server_app, "_RUN_GATE", gate)

    paths = []
    for i in range(3):
        p = tmp_path / f"del_case_{i}.py"
        p.write_text(f"value = {i}\n", encoding="utf-8")
        paths.append(p)

    release = threading.Event()

    async def blocking_run(config, emitter):
        while not release.is_set():
            await asyncio.sleep(0.01)
        return _minimal_report()

    monkeypatch.setattr(server_app, "run_audit", blocking_run)
    with TestClient(server_app.create_app()) as client:
        running_id = client.post("/api/audits", json={"source_path": str(paths[0])}).json()["audit_id"]
        _wait_entry_status(running_id, "running")

        queued_id = client.post("/api/audits", json={"source_path": str(paths[1])}).json()["audit_id"]
        queued_entry = _wait_entry_status(queued_id, "queued")
        queued_task = queued_entry["task"]
        assert queued_task is not None

        # DELETE 排队中任务：表项移除 + 以引用观察落「任务已取消」
        resp = client.delete(f"/api/audits/{queued_id}")
        assert resp.status_code == 204
        assert queued_id not in server_app.AUDITS
        assert queued_entry["status"] == "failed"
        assert queued_entry["error"] == "任务已取消"
        _wait_task_done(queued_task)
        assert queued_task.cancelled() is True

        # 闸门不泄漏：放行 running 任务后，后续任务仍能正常排队并完成
        release.set()
        assert _wait_status(client, running_id, "done")["error"] is None
        third_id = client.post("/api/audits", json={"source_path": str(paths[2])}).json()["audit_id"]
        assert _wait_status(client, third_id, "done")["error"] is None
        assert gate._value == 1
