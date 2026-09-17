"""W14-A3 服务端修复自测：M-4 磁盘回收、M-5 治理上限惰性解析、M-6 上传 TOCTOU 二次校验。

- M-4：prune（上传触发）/ 启动 sweep / DELETE（终态）删行时同步回收
  <work_root>/<audit_id>/ 与 uploads zip；DELETE 对 running/queued 只删行
  （协作取消），目录由执行体幽灵收尾回收——绝不 rmtree 运行中任务的工作副本；
- M-5：_MAX_RUNNING_AUDITS/_MAX_PENDING_AUDITS 惰性解析——.env 运行期加载后
  即生效（对齐 CODEAUDIT_DB_PATH 时机），无需重导入；_RUN_GATE 容量随之惰性化；
- M-6：上传读流间隙容量被并发填满时，读流后、建任务前的权威二次校验返回与
  快速路径完全同形的 429，并清掉已落盘 zip（零残留）。

风格对齐既有用例：假流水线注入（monkeypatch server_app.run_audit）、条件等待
（deadline 轮询，无固定 sleep 同步）、work_root 重定向 tmp_path（conftest 公共
fixture）、全程离线。autouse store fixture 注入的库不带 work_root（向后兼容
口径），本文件经 gc_store fixture 重注入带 work_root 的 store 供磁盘回收断言。
"""

from __future__ import annotations

import asyncio
import io
import os
import threading
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from audit import config as audit_config
from audit.config import AuditConfig
from audit.taskstore import TaskStore
from server import app as server_app


# ---------------------------------------------------------------- 夹具与工具
@pytest.fixture
def gc_store(work_root_tmp, monkeypatch):
    """带 work_root 的 TaskStore（库与 uploads 同在 work_root_tmp 下），
    替换 autouse store 注入——磁盘回收断言的前提。"""
    st = TaskStore(work_root_tmp / "gc.sqlite", work_root=str(work_root_tmp))
    monkeypatch.setattr(server_app, "_STORE", st)
    yield st
    try:
        st.close()
    except Exception:  # noqa: BLE001 —— 任务线程收尾写与关库的良性竞态，静默即可
        pass


def _seed(store: TaskStore, audit_id: str, *, status: str, created_at: str) -> None:
    store.create(
        audit_id,
        status=status,
        error=None,
        created_at=created_at,
        source_path="seeded",
        do_fix=False,
        do_tests=False,
    )


def _materialize(work_root: Path, audit_id: str) -> tuple[Path, Path]:
    """物化一个任务的磁盘痕迹：工作目录 + 上传 zip，返回 (workdir, zip)。"""
    workdir = work_root / audit_id
    (workdir / "reports").mkdir(parents=True, exist_ok=True)
    (workdir / "copy.txt").write_text("project copy", encoding="utf-8")
    uploads = work_root / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    zip_path = uploads / f"{audit_id}.zip"
    zip_path.write_bytes(b"PK\x05\x06")
    return workdir, zip_path


def _uploads_files(work_root: Path) -> list[Path]:
    uploads = work_root / "uploads"
    return list(uploads.iterdir()) if uploads.is_dir() else []


def _make_zip_bytes(content: str = "x = 1\n") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mini_app/main.py", content)
    return buf.getvalue()


def _wait_no_running(timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while server_app._RUNNING and time.time() < deadline:
        time.sleep(0.02)
    assert not server_app._RUNNING, f"仍有运行句柄残留：{list(server_app._RUNNING)}"


@pytest.fixture
def fake_pipeline(monkeypatch):
    async def fake_run_audit(config, emitter):
        await emitter({"type": "progress", "stage": "ingest", "message": "假阶段"})
        return None

    monkeypatch.setattr(server_app, "run_audit", fake_run_audit)


# ---------------------------------------------------------------- M-4 ①：prune 回收
def test_upload_prune_recycles_evicted_workdir(
    monkeypatch, gc_store, work_root_tmp, fake_pipeline
):
    """上传建任务触发 prune：被淘汰的最旧终态行工作目录同步回收，其余保留。"""
    monkeypatch.setattr(server_app, "_AUDITS_MAX", 2)
    _seed(gc_store, "old-a", status="done", created_at="2026-01-01T00:00:01+08:00")
    _seed(gc_store, "old-b", status="done", created_at="2026-01-01T00:00:02+08:00")
    old_a_dir, _old_a_zip = _materialize(work_root_tmp, "old-a")
    old_b_dir, _old_b_zip = _materialize(work_root_tmp, "old-b")

    client = TestClient(server_app.create_app())
    resp = client.post(
        "/api/audits/upload",
        files={"file": ("src.zip", _make_zip_bytes(), "application/zip")},
    )
    assert resp.status_code == 200
    total, _ = gc_store.list(0, 0)
    assert total == 2  # 容量上限生效
    assert gc_store.get("old-a") is None  # 最旧终态行被淘汰
    assert not old_a_dir.exists()  # M-4：淘汰行的目录同步回收
    assert gc_store.get("old-b") is not None
    assert old_b_dir.is_dir()  # 未淘汰的目录保留


# ---------------------------------------------------------------- M-4 ②：sweep 回收
def test_startup_sweep_recycles_leftover_dirs(gc_store, work_root_tmp):
    """启动 sweep 把遗留 queued/running 置 failed 并同步回收目录；终态目录不动。"""
    _seed(gc_store, "left-run", status="running", created_at="2026-01-01T00:00:01+08:00")
    _seed(gc_store, "left-q", status="queued", created_at="2026-01-01T00:00:02+08:00")
    _seed(gc_store, "done-keep", status="done", created_at="2026-01-01T00:00:03+08:00")
    run_dir, _ = _materialize(work_root_tmp, "left-run")
    q_dir, _ = _materialize(work_root_tmp, "left-q")
    done_dir, _ = _materialize(work_root_tmp, "done-keep")

    with TestClient(server_app.create_app()) as client:  # lifespan 触发 sweep
        d = client.get("/api/audits/left-run").json()
        assert d["status"] == "failed"
        assert d["error"] == "服务重启中断"
        assert not run_dir.exists()  # 被 sweep → 目录回收
        assert not q_dir.exists()
        assert done_dir.is_dir()  # 终态不动


# ---------------------------------------------------------------- M-4 ③：DELETE 回收
def test_delete_terminal_recycles_dir_and_zip_via_http(gc_store, work_root_tmp):
    """DELETE 终态任务：204 + 删行 + 工作目录与上传 zip 一并回收。"""
    _seed(gc_store, "gone", status="done", created_at="2026-01-01T00:00:01+08:00")
    workdir, zip_path = _materialize(work_root_tmp, "gone")

    client = TestClient(server_app.create_app())
    resp = client.delete("/api/audits/gone")
    assert resp.status_code == 204
    assert gc_store.get("gone") is None
    assert not workdir.exists()
    assert not zip_path.exists()
    assert _uploads_files(work_root_tmp) == []


def test_delete_running_keeps_files_until_executor_finishes(
    gc_store, work_root_tmp, monkeypatch
):
    """DELETE running 任务（协作取消）：只删行不删文件；目录由执行体幽灵收尾回收。

    M-4 取舍（如实记录）：DELETE 对非终态任务绝不 rmtree 运行副本——文件先保留
    到执行体在事件边界退出（_cleanup_if_ghost），再由执行体回收。
    """
    release = threading.Event()

    async def two_phase_run(config, emitter):
        await emitter({"type": "progress", "stage": "ingest", "message": "第一段"})
        while not release.is_set():
            await asyncio.sleep(0.01)
        await emitter({"type": "progress", "stage": "detect", "message": "取消后边界"})

    monkeypatch.setattr(server_app, "run_audit", two_phase_run)
    with TestClient(server_app.create_app()) as client:
        audit_id = client.post(
            "/api/audits", json={"source_path": str(work_root_tmp)}
        ).json()["audit_id"]
        deadline = time.time() + 10
        while time.time() < deadline:
            if any(
                e.get("stage") == "ingest" for _s, e in gc_store.get_events(audit_id)
            ):
                break
            time.sleep(0.02)
        # 模拟流水线已物化的工作副本与 zip
        workdir, zip_path = _materialize(work_root_tmp, audit_id)

        resp = client.delete(f"/api/audits/{audit_id}")
        assert resp.status_code == 204
        assert gc_store.get(audit_id) is None  # 行消失即取消信号
        assert workdir.is_dir()  # running：绝不即时删目录
        assert zip_path.is_file()

        release.set()  # 放行 → 执行体在事件边界抛 CancelledError → 幽灵收尾回收
        _wait_no_running()
        assert not workdir.exists()  # 执行体收尾已回收
        assert not zip_path.exists()
        assert gc_store.get(audit_id) is None  # 幽灵静默：行不复活


# ---------------------------------------------------------------- M-5：惰性解析
def test_lazy_limits_take_effect_after_dotenv_load(tmp_path, monkeypatch):
    """.env 运行期加载后治理上限即生效（M-5）：无需重导入，对齐 CODEAUDIT_DB_PATH。

    进程环境无该变量时走默认值；from_env 触发 .env 自动加载（CODEAUDIT_* setdefault
    进 os.environ）后，惰性取值立即反映 .env 配置。
    """
    monkeypatch.delenv("CODEAUDIT_MAX_PENDING", raising=False)
    monkeypatch.delenv("CODEAUDIT_MAX_RUNNING", raising=False)
    # import 期不再固化：模块名默认 None（= 按 env 惰性解析）
    assert server_app._MAX_RUNNING_AUDITS is None
    assert server_app._MAX_PENDING_AUDITS is None
    assert server_app._get_max_running() == 4  # 默认
    assert server_app._get_max_pending() == 20  # 默认

    (tmp_path / ".env").write_text(
        "CODEAUDIT_MAX_RUNNING=3\nCODEAUDIT_MAX_PENDING=7\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(audit_config, "_ENV_LOADED", False)  # 允许本用例触发加载
    AuditConfig.from_env()  # lifespan/请求路径同款调用：触发 .env 加载
    assert os.environ["CODEAUDIT_MAX_RUNNING"] == "3"
    assert os.environ["CODEAUDIT_MAX_PENDING"] == "7"
    # 惰性取值立即生效（修复前：import 期固化，.env 静默失效）
    assert server_app._get_max_running() == 3
    assert server_app._get_max_pending() == 7


def test_run_gate_capacity_resolves_lazily_from_env(monkeypatch):
    """_RUN_GATE 惰性初始化：首次取用时按 CODEAUDIT_MAX_RUNNING 建容量并复用同一实例。"""
    monkeypatch.setattr(server_app, "_RUN_GATE", None)
    monkeypatch.setenv("CODEAUDIT_MAX_RUNNING", "2")
    gate = server_app._get_run_gate()
    assert isinstance(gate, asyncio.Semaphore)
    assert gate._value == 2
    assert server_app._get_run_gate() is gate  # acquire/release 必须同一信号量


def test_lazy_limit_override_entry_still_supported(monkeypatch):
    """覆写入口兼容：monkeypatch 模块名（非 None）直接生效，优先于 env。"""
    monkeypatch.setenv("CODEAUDIT_MAX_PENDING", "99")
    monkeypatch.setattr(server_app, "_MAX_PENDING_AUDITS", 2)
    assert server_app._get_max_pending() == 2  # 覆写优先（既有测试写法保持可用）
    monkeypatch.setattr(server_app, "_MAX_PENDING_AUDITS", None)  # 回到 env 口径
    assert server_app._get_max_pending() == 99


# ---------------------------------------------------------------- M-6：上传 TOCTOU
def test_upload_second_capacity_check_rejects_after_stream_and_cleans_zip(
    monkeypatch, store, work_root_tmp
):
    """M-6：读流间隙容量被并发填满 → 读流后、建任务前的权威校验返回同形 429，
    已落盘 zip 清理干净（零残留），且不创建任务行。

    用 monkeypatch 包裹 UploadFile.read 模拟读流间隙：首块读取后向 store 注入
    一个非终态任务（模拟并发上传抢先建任务），使首次检查（通过）与建任务之间
    的容量发生变化。
    """
    monkeypatch.setattr(server_app, "_MAX_PENDING_AUDITS", 1)  # 覆写入口（M-5 兼容）
    assert store.count_active() == 0  # 首次检查将通过

    original_read = UploadFile.read
    seeded = {"done": False}

    async def read_and_seed(self, size=-1):
        chunk = await original_read(self, size)
        if chunk and not seeded["done"]:
            seeded["done"] = True
            # 模拟并发上传在读流间隙抢先建任务（非终态）
            store.create(
                "racer",
                status="queued",
                error=None,
                created_at="2026-01-01T00:00:00+08:00",
                source_path="raced",
                do_fix=False,
                do_tests=False,
            )
        return chunk

    monkeypatch.setattr(UploadFile, "read", read_and_seed)

    client = TestClient(server_app.create_app())
    resp = client.post(
        "/api/audits/upload",
        files={"file": ("src.zip", _make_zip_bytes(), "application/zip")},
    )
    assert resp.status_code == 429
    # 与快速路径完全同形的 429 响应（契约零变化）
    assert resp.json()["detail"] == "服务器并发审计数已达上限（1），请稍后重试"
    # 已落盘 zip 清理干净 + 未创建任务行（零残留语义）
    assert _uploads_files(work_root_tmp) == []
    total, items = store.list(0, 0)
    assert total == 1
    assert items[0]["audit_id"] == "racer"


def test_upload_second_check_passes_when_capacity_still_ok(
    monkeypatch, gc_store, work_root_tmp, fake_pipeline
):
    """M-6 反向：读流后容量未越限 → 二次校验通过，上传正常建任务（无行为回归）。"""
    client = TestClient(server_app.create_app())
    resp = client.post(
        "/api/audits/upload",
        files={"file": ("src.zip", _make_zip_bytes(), "application/zip")},
    )
    assert resp.status_code == 200
    audit_id = resp.json()["audit_id"]
    assert gc_store.get(audit_id) is not None
    assert (work_root_tmp / "uploads" / f"{audit_id}.zip").is_file()
