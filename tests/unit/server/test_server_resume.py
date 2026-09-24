"""P0-9 server resume 端点用例：POST /api/audits/{audit_id}/resume。

- 正常续跑：interrupted → mark_resuming → run_audit(resume_stages=判定集) →
  报告落库 + done，响应为任务行白名单字段；
- SIGKILL 滞留自愈：running+进度+updated_at 陈旧 → sweep 自愈后可续；窗口内
  有活动（新鲜）→ 409「仍在运行」；
- 不可续跑（done / queued / 无进度 failed）→ 409；任务不存在 → 404；
- F6-R1（W26）：resume 重建 config 后复验 SOURCE_ROOTS 白名单——越界 400、
  空 source_path 400，先校验后 mark_resuming（校验失败不改任务状态）；
- 限流钉住：resume 是 POST，被 W15-A1 限流中间件全局记账覆盖（超限 429）。

风格对齐既有 server 用例：假流水线注入（monkeypatch server_app.run_audit）、
条件等待（deadline 轮询）、上下文管理器持有 TestClient（lifespan + portal 存活）。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from audit.models import AuditReport, AuditStats
from server import app as server_app


@pytest.fixture(autouse=True)
def _isolate_sweep_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离安全治理环境变量：自愈窗口走默认分支；白名单/限流显式 setenv 即生效，
    未设置的用例不受进程环境（.env/其他窗口导出）漂移影响。"""
    monkeypatch.delenv("CODEAUDIT_SWEEP_GRACE_SEC", raising=False)
    monkeypatch.delenv("CODEAUDIT_SOURCE_ROOTS", raising=False)
    monkeypatch.delenv("CODEAUDIT_RATE_LIMIT", raising=False)
    # 限流记账是模块级滑动窗口（按 client IP 记时间戳）——清空防用例间串扰
    server_app._RATE_LIMIT_EVENTS.clear()


def _minimal_report(audit_id: str) -> AuditReport:
    return AuditReport(
        audit_id=audit_id,
        project_name="demo_proj",
        health_score=95.0,
        summary={"high": 2},
        stats=AuditStats(),
    )


@pytest.fixture
def fake_pipeline(monkeypatch: pytest.MonkeyPatch):
    """续跑形态的假流水线：接受 resume_stages 关键字，记录调用证据。

    calls["count"] 累计 run_audit 被拉起次数（双 resume「恰一成功一拒绝」断言用）。
    """
    calls: dict[str, Any] = {}

    async def fake_run_audit(
        config: Any, emitter: Any, *, audit_id: Any = None, resume_stages: Any = None
    ) -> AuditReport:
        calls["audit_id"] = audit_id
        calls["resume_stages"] = frozenset(resume_stages or ())
        calls["source_path"] = config.source_path
        calls["api_key_not_redacted"] = config.api_key != "<redacted>"
        calls["out_dir"] = config.out_dir
        calls["count"] = calls.get("count", 0) + 1
        await emitter({"type": "progress", "stage": "init", "message": "fake 续跑"})
        return _minimal_report(str(audit_id))

    monkeypatch.setattr(server_app, "run_audit", fake_run_audit)
    return calls


def _seed_resumable(
    store: Any,
    audit_id: str,
    source: Path,
    work_root: Path,
    *,
    status: str = "interrupted",
    stage_done: tuple[str, ...] = ("ingest",),
) -> None:
    """落一行可续跑任务：config_json 模拟 server 脱敏落库形态（含未知键）。"""
    store.create(
        audit_id,
        status=status,
        created_at="2026-01-01T00:00:00+08:00",
        source_path=str(source),
        do_fix=False,
        do_tests=False,
        config_json=json.dumps(
            {
                "source_path": str(source),
                "work_root": str(work_root),
                "api_key": "<redacted>",
                "unknown_future_key": 1,
            },
            ensure_ascii=False,
        ),
    )
    for stage in stage_done:
        store.record_stage_done(audit_id, stage)


def _backdate_updated_at(store: Any, audit_id: str, iso: str) -> None:
    """白盒构造「陈旧行」（形态同 tests/unit/taskstore 的 grace 用例）。"""
    with store._lock:
        store._conn.execute(
            "UPDATE audits SET updated_at = ? WHERE audit_id = ?", (iso, audit_id)
        )
        store._conn.commit()


def _stale_iso(seconds_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat(
        timespec="seconds"
    )


def _wait_status(client: TestClient, audit_id: str, status: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    data: dict = {}
    while time.time() < deadline:
        data = client.get(f"/api/audits/{audit_id}").json()
        if data["status"] == status:
            return data
        time.sleep(0.05)
    raise AssertionError(f"等待状态 {status} 超时，当前：{data}")


# ---------------------------------------------------------------- a) 正常续跑
def test_resume_happy_path_interrupted(
    fake_pipeline, store, tmp_path: Path
) -> None:
    """interrupted + 进度 → 200 任务行（running）→ 后台跑完落报告 + done。"""
    source = tmp_path / "proj"
    source.mkdir()
    (source / "a.py").write_text("x = 1\n", encoding="utf-8")
    work_root = tmp_path / "work"
    work_root.mkdir()
    (work_root / "resv01" / "src").mkdir(parents=True)  # 工作副本在盘 → 放行 ingest
    _seed_resumable(store, "resv01", source, work_root)

    with TestClient(server_app.create_app()) as client:
        resp = client.post("/api/audits/resv01/resume")
        assert resp.status_code == 200
        body = resp.json()
        assert body["audit_id"] == "resv01"
        assert body["status"] == "running"  # mark_resuming 已复位
        assert "config_json" not in body  # 任务行白名单字段，不泄漏内部列

        data = _wait_status(client, "resv01", "done")
        assert data["report"]["project_name"] == "demo_proj"
        entry = store.get("resv01")
        assert entry is not None and entry["status"] == "done" and entry["error"] is None
        report = store.get_report("resv01")
        assert report is not None and report.audit_id == "resv01"
        # 假流水线调用证据：断点集合 / 同源 config / 脱敏剔除 / 报告目录按任务隔离
        assert fake_pipeline["audit_id"] == "resv01"
        assert fake_pipeline["resume_stages"] == frozenset({"ingest"})
        assert fake_pipeline["source_path"] == str(source)
        assert fake_pipeline["api_key_not_redacted"] is True
        assert fake_pipeline["out_dir"] == str(work_root / "resv01" / "reports")
        events = [e for _seq, e in store.get_events("resv01")]
        assert any(e.get("message") == "fake 续跑" for e in events)  # 事件流落库


# ---------------------------------------------------------------- b) SIGKILL 滞留自愈
def test_resume_running_stale_sweep_self_heals(
    fake_pipeline, store, tmp_path: Path
) -> None:
    """running+进度+updated_at 陈旧（超缺省 30s 窗口）→ sweep 自愈 → 续跑成功。

    预置在 TestClient 进入之后：lifespan 启动 sweep（grace=0 既有语义）会把
    running+进度行提前转 interrupted，抢占本用例要验证的端点自愈分支。
    """
    source = tmp_path / "proj"
    source.mkdir()
    work_root = tmp_path / "work"
    work_root.mkdir()
    (work_root / "kill01" / "src").mkdir(parents=True)

    with TestClient(server_app.create_app()) as client:
        _seed_resumable(store, "kill01", source, work_root, status="running")
        _backdate_updated_at(store, "kill01", _stale_iso(60))
        resp = client.post("/api/audits/kill01/resume")
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"
        _wait_status(client, "kill01", "done")
        assert fake_pipeline["resume_stages"] == frozenset({"ingest"})
        assert store.get("kill01")["status"] == "done"


def test_resume_running_fresh_conflict(store, tmp_path: Path, monkeypatch) -> None:
    """running+进度但窗口内有 updated_at 活动 → 409「仍在运行」，状态与进度保持。"""

    async def _fail_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("窗口内活跃任务不应进入 run_audit")

    monkeypatch.setattr(server_app, "run_audit", _fail_run)
    source = tmp_path / "proj"
    source.mkdir()
    work_root = tmp_path / "work"
    work_root.mkdir()

    with TestClient(server_app.create_app()) as client:
        # 进程内预置（无盘上副本也不影响本用例——在守卫层即拒绝，不进判定）
        _seed_resumable(store, "live01", source, work_root, status="running")
        resp = client.post("/api/audits/live01/resume")
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "仍在运行" in detail and "请稍后重试" in detail
        entry = store.get("live01")
        assert entry is not None and entry["status"] == "running"  # 未被误伤
        assert store.get_stage_done("live01") == ["ingest"]  # 进度保留


# ---------------------------------------------------------------- c) 不可续跑 / 不存在
@pytest.mark.parametrize("status", ["done", "queued", "failed"])
def test_resume_non_resumable_conflict(store, tmp_path: Path, monkeypatch, status: str) -> None:
    """非可续跑态 → 409「不可续跑」，状态不被改动、流水线不被拉起。

    预置在 TestClient 进入之后：避开 lifespan 启动 sweep 对 queued/running 的
    既有清扫，保证各状态在用例内原样自洽。
    """

    async def _fail_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("不可续跑任务不应进入 run_audit")

    monkeypatch.setattr(server_app, "run_audit", _fail_run)
    source = tmp_path / "proj"
    source.mkdir()

    with TestClient(server_app.create_app()) as client:
        _seed_resumable(store, f"nogo-{status}", source, tmp_path, status=status, stage_done=())
        resp = client.post(f"/api/audits/nogo-{status}/resume")
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "不可续跑" in detail and status in detail
        entry = store.get(f"nogo-{status}")
        assert entry is not None and entry["status"] == status


def test_resume_missing_task_404(fake_pipeline, store) -> None:
    """任务不存在 → 404 中文 detail，与查询端点同语义。"""
    with TestClient(server_app.create_app()) as client:
        resp = client.post("/api/audits/ghost/resume")
        assert resp.status_code == 404
        assert "任务不存在" in resp.json()["detail"]
        assert fake_pipeline == {}  # 流水线未被拉起


# ---------------------------------------------------------------- d) F6-R1：resume 白名单复验
def test_resume_source_outside_whitelist_400(
    fake_pipeline, store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6-R1：越界 source_path 走创建 400、走 resume 同样必须 400（堵住绕过路径）。

    校验先于 mark_resuming——任务状态与阶段进度保持 interrupted，流水线不拉起。
    """
    roots = tmp_path / "roots"
    roots.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.py").write_text("x = 1\n", encoding="utf-8")
    work_root = tmp_path / "work"
    work_root.mkdir()
    (work_root / "f6r1" / "src").mkdir(parents=True)
    monkeypatch.setenv("CODEAUDIT_SOURCE_ROOTS", str(roots))
    _seed_resumable(store, "f6r1", outside, work_root)

    with TestClient(server_app.create_app()) as client:
        resp = client.post("/api/audits/f6r1/resume")
        assert resp.status_code == 400
        assert "不在允许的根目录内" in resp.json()["detail"]
        entry = store.get("f6r1")
        assert entry is not None and entry["status"] == "interrupted"  # 状态未被改动
        assert store.get_stage_done("f6r1") == ["ingest"]  # 进度未被清
        assert fake_pipeline == {}  # run_audit 未被拉起


def test_resume_inside_whitelist_still_resumes(
    fake_pipeline, store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """白名单含 source 所在根 → 合法续跑不受影响（200 → done）。"""
    roots = tmp_path / "roots"
    source = roots / "proj"
    source.mkdir(parents=True)
    (source / "a.py").write_text("x = 1\n", encoding="utf-8")
    work_root = tmp_path / "work"
    work_root.mkdir()
    (work_root / "f6ok" / "src").mkdir(parents=True)
    monkeypatch.setenv("CODEAUDIT_SOURCE_ROOTS", str(roots))
    _seed_resumable(store, "f6ok", source, work_root)

    with TestClient(server_app.create_app()) as client:
        resp = client.post("/api/audits/f6ok/resume")
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"
        _wait_status(client, "f6ok", "done")
        assert fake_pipeline["resume_stages"] == frozenset({"ingest"})
        assert fake_pipeline["source_path"] == str(source)
        assert store.get("f6ok")["status"] == "done"


def test_resume_missing_source_path_400(
    fake_pipeline, store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """config_json 空/缺 source_path → 直接 400 中文 detail（Path('') 语义下
    resolve 恒等于进程 cwd，交白名单判定会误导甚至放行），状态保持不拉流水线。"""
    roots = tmp_path / "roots"
    roots.mkdir()
    monkeypatch.setenv("CODEAUDIT_SOURCE_ROOTS", str(roots))
    store.create(
        "nosrc",
        status="interrupted",
        created_at="2026-01-01T00:00:00+08:00",
        source_path="",
        do_fix=False,
        do_tests=False,
        config_json="",  # 模拟空/损坏任务记录：重建 config 的 source_path 为缺省 ""
    )
    store.record_stage_done("nosrc", "ingest")

    with TestClient(server_app.create_app()) as client:
        resp = client.post("/api/audits/nosrc/resume")
        assert resp.status_code == 400
        assert "source_path" in resp.json()["detail"]
        entry = store.get("nosrc")
        assert entry is not None and entry["status"] == "interrupted"
        assert fake_pipeline == {}


def test_resume_double_fire_exactly_one_launch(
    store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """二连双 resume（并发语义钉住）：第一发 200，第二发 409（第一发已 mark_resuming
    置 running，sweep 自愈发现窗口内有活动 → 拒绝双执行体），run_audit 恰一次被拉起。

    本用例自带慢速假流水线（0.2s 执行窗口）：保证第二发请求抵达时任务仍在 running，
    语义确定，不与快速完成的假流水线竞速。
    """
    import asyncio

    calls: dict[str, Any] = {}

    async def slow_run_audit(
        config: Any, emitter: Any, *, audit_id: Any = None, resume_stages: Any = None
    ) -> AuditReport:
        calls["audit_id"] = audit_id
        calls["count"] = calls.get("count", 0) + 1
        await asyncio.sleep(0.2)
        await emitter({"type": "progress", "stage": "init", "message": "fake 续跑"})
        return _minimal_report(str(audit_id))

    monkeypatch.setattr(server_app, "run_audit", slow_run_audit)
    source = tmp_path / "proj"
    source.mkdir()
    (source / "a.py").write_text("x = 1\n", encoding="utf-8")
    work_root = tmp_path / "work"
    work_root.mkdir()
    (work_root / "twice" / "src").mkdir(parents=True)
    _seed_resumable(store, "twice", source, work_root)

    with TestClient(server_app.create_app()) as client:
        first = client.post("/api/audits/twice/resume")
        second = client.post("/api/audits/twice/resume")
        assert first.status_code == 200
        assert second.status_code == 409
        assert "仍在运行" in second.json()["detail"]
        _wait_status(client, "twice", "done")
        assert calls == {"audit_id": "twice", "count": 1}  # run_audit 恰一次
        assert store.get("twice")["status"] == "done"


# ---------------------------------------------------------------- e) 限流钉住：resume 被 POST 全局记账覆盖
def test_resume_rate_limit_covers_resume_endpoint(
    fake_pipeline, store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """限流定性钉住（W26 核实结论）：W15-A1 限流中间件对所有 /api/* 的 POST/DELETE
    记账、无路径排除——resume 是 POST，天然被覆盖。设极小限额连发：
    第一发放行 200，第二发在中间件层即 429（未进端点守卫，状态不动）。"""
    monkeypatch.setenv("CODEAUDIT_RATE_LIMIT", "1")
    source = tmp_path / "proj"
    source.mkdir()
    work_root = tmp_path / "work"
    work_root.mkdir()
    (work_root / "rate1" / "src").mkdir(parents=True)
    (work_root / "rate2" / "src").mkdir(parents=True)
    _seed_resumable(store, "rate1", source, work_root)
    _seed_resumable(store, "rate2", source, work_root)

    with TestClient(server_app.create_app()) as client:
        first = client.post("/api/audits/rate1/resume")
        assert first.status_code == 200  # 记账第 1 笔，放行
        second = client.post("/api/audits/rate2/resume")
        assert second.status_code == 429
        assert "请求过于频繁" in second.json()["detail"]
        # 429 在中间件层返回：rate2 未进端点逻辑，状态保持 interrupted
        entry = store.get("rate2")
        assert entry is not None and entry["status"] == "interrupted"
