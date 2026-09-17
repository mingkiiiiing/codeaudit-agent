"""W15-A1 服务安全治理自测（docs/20 §4.1/§5 卡A 压力项）：鉴权 / source_path
白名单 / 限流 / SSE 并发上限。

治理项全部 opt-in（env 缺省 = 关闭 = 既有行为，既有用例群即回归）；本文件用
monkeypatch.setenv 逐项开启验证。SSE 上限用真实长驻流（running 任务 + 后台线程
消费）验证 429 与名额归还；限流覆盖 POST/DELETE 计数与滑动窗口过期放行语义。
风格对齐 test_server_governance：假流水线注入、条件等待（deadline 轮询）、
work_root 重定向 tmp_path、全程离线。
"""

from __future__ import annotations

import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from audit.models import AuditReport
from server import app as server_app

_TOKEN_ENVS = ("CODEAUDIT_API_TOKEN", "CODEAUDIT_SOURCE_ROOTS", "CODEAUDIT_RATE_LIMIT")


@pytest.fixture
def clean_env(monkeypatch):
    """清理治理三键（防用户 shell / .env 残留串扰），用例内按需 setenv 开启。"""
    for name in _TOKEN_ENVS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def client(clean_env, store) -> TestClient:
    return TestClient(server_app.create_app())


@pytest.fixture
def fake_pipeline(monkeypatch):
    """假流水线：POST 建的任务立即落预填报告（离线、不真跑审计）。"""
    async def fake_run_audit(config, emitter):
        await emitter({"type": "progress", "stage": "ingest", "message": "假阶段开始"})
        return AuditReport(
            audit_id="w15sec01",
            project_name="demo_proj",
            languages={"python": 100.0},
            loc=10,
            health_score=99.0,
            summary={"critical": 0, "high": 0, "medium": 0, "low": 0},
            issues=[],
        )

    monkeypatch.setattr(server_app, "run_audit", fake_run_audit)


# ---------------------------------------------------------------- 鉴权


class TestAuth:
    def test_disabled_by_default(self, client):
        """token 空 = 鉴权关闭：既有行为零变化。"""
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/audits").status_code == 200

    def test_401_without_token_and_health_exempt(self, client, monkeypatch):
        """token 非空：无凭据 401；GET /api/health 豁免（存活探针）。"""
        monkeypatch.setenv("CODEAUDIT_API_TOKEN", "s3cret-token")
        r = client.get("/api/audits")
        assert r.status_code == 401
        assert "detail" in r.json()  # 错误结构与既有 HTTPException 输出同形
        assert client.get("/api/health").status_code == 200

    def test_bearer_and_x_api_token_accepted(self, client, monkeypatch):
        monkeypatch.setenv("CODEAUDIT_API_TOKEN", "s3cret-token")
        ok_bearer = client.get(
            "/api/audits", headers={"Authorization": "Bearer s3cret-token"}
        )
        assert ok_bearer.status_code == 200
        ok_header = client.get("/api/audits", headers={"X-API-Token": "s3cret-token"})
        assert ok_header.status_code == 200

    def test_wrong_token_rejected(self, client, monkeypatch):
        monkeypatch.setenv("CODEAUDIT_API_TOKEN", "s3cret-token")
        wrong_bearer = client.get(
            "/api/audits", headers={"Authorization": "Bearer wrong-token"}
        )
        assert wrong_bearer.status_code == 401
        wrong_header = client.get("/api/audits", headers={"X-API-Token": "wrong-token"})
        assert wrong_header.status_code == 401


# ---------------------------------------------------------------- source_path 白名单


class TestSourceRoots:
    def test_unrestricted_by_default(self, client, fake_pipeline, tmp_path):
        """白名单空 = 不限制（既有行为）：任意存在路径可建任务。"""
        proj = tmp_path / "anywhere"
        proj.mkdir()
        r = client.post("/api/audits", json={"source_path": str(proj)})
        assert r.status_code == 200
        assert r.json()["audit_id"]

    def test_inside_root_allowed(self, client, fake_pipeline, monkeypatch, tmp_path):
        roots = tmp_path / "roots"
        proj = roots / "proj"
        proj.mkdir(parents=True)
        monkeypatch.setenv("CODEAUDIT_SOURCE_ROOTS", str(roots))
        r = client.post("/api/audits", json={"source_path": str(proj)})
        assert r.status_code == 200

    def test_root_itself_allowed(self, client, fake_pipeline, monkeypatch, tmp_path):
        """根目录本身（relative_to == '.'）也放行。"""
        roots = tmp_path / "roots"
        roots.mkdir()
        monkeypatch.setenv("CODEAUDIT_SOURCE_ROOTS", str(roots))
        r = client.post("/api/audits", json={"source_path": str(roots)})
        assert r.status_code == 200

    def test_outside_root_rejected_400(self, client, fake_pipeline, monkeypatch, tmp_path):
        roots = tmp_path / "roots"
        roots.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        monkeypatch.setenv("CODEAUDIT_SOURCE_ROOTS", str(roots))
        r = client.post("/api/audits", json={"source_path": str(outside)})
        assert r.status_code == 400
        assert "不在允许的根目录内" in r.json()["detail"]
        # 越界请求不得建任务
        assert client.get("/api/audits").json()["total"] == 0

    def test_multiple_roots_via_pathsep(self, client, fake_pipeline, monkeypatch, tmp_path):
        root_a = tmp_path / "roots_a"
        root_b = tmp_path / "roots_b"
        proj_b = root_b / "proj"
        proj_b.mkdir(parents=True)
        monkeypatch.setenv(
            "CODEAUDIT_SOURCE_ROOTS", os.pathsep.join([str(root_a), str(root_b)])
        )
        r = client.post("/api/audits", json={"source_path": str(proj_b)})
        assert r.status_code == 200


# ---------------------------------------------------------------- 限流


class TestRateLimit:
    @pytest.fixture(autouse=True)
    def _reset_rate_limit(self, monkeypatch):
        """限流记账是模块级 dict：用例前换空表隔离（60s 窗口内的旧时间戳会串扰）。"""
        monkeypatch.setattr(server_app, "_RATE_LIMIT_EVENTS", {})

    def test_disabled_by_default(self, client, fake_pipeline, tmp_path):
        """rate=0 = 关闭：连续多个 POST 均放行。"""
        proj = tmp_path / "proj"
        proj.mkdir()
        for _ in range(3):
            assert client.post("/api/audits", json={"source_path": str(proj)}).status_code == 200

    def test_post_limit_429_then_get_unaffected(self, client, fake_pipeline, monkeypatch, tmp_path):
        monkeypatch.setenv("CODEAUDIT_RATE_LIMIT", "2")
        proj = tmp_path / "proj"
        proj.mkdir()
        assert client.post("/api/audits", json={"source_path": str(proj)}).status_code == 200
        assert client.post("/api/audits", json={"source_path": str(proj)}).status_code == 200
        limited = client.post("/api/audits", json={"source_path": str(proj)})
        assert limited.status_code == 429
        assert "频繁" in limited.json()["detail"]
        # 读方法不受限流
        assert client.get("/api/audits").status_code == 200
        assert client.get("/api/health").status_code == 200

    def test_delete_counts_toward_limit(self, client, monkeypatch):
        """DELETE（含 404 路径）同样消耗写配额。"""
        monkeypatch.setenv("CODEAUDIT_RATE_LIMIT", "2")
        assert client.delete("/api/audits/no-such-1").status_code == 404
        assert client.delete("/api/audits/no-such-2").status_code == 404
        assert client.delete("/api/audits/no-such-3").status_code == 429

    def test_sliding_window_expires(self, client, fake_pipeline, monkeypatch, tmp_path):
        """窗口滑过（记账时间戳移出 60s 窗口）后配额恢复——直接回填过期时间戳验证。"""
        monkeypatch.setenv("CODEAUDIT_RATE_LIMIT", "1")
        proj = tmp_path / "proj"
        proj.mkdir()
        assert client.post("/api/audits", json={"source_path": str(proj)}).status_code == 200
        assert client.post("/api/audits", json={"source_path": str(proj)}).status_code == 429
        # 把该 IP 的记账整体回填为 61s 前（已出窗）→ 下一次请求重新放行
        stale = time.monotonic() - 61.0
        for events in server_app._RATE_LIMIT_EVENTS.values():
            events[:] = [stale]
        assert client.post("/api/audits", json={"source_path": str(proj)}).status_code == 200


# ---------------------------------------------------------------- 存储接线（W15-A5）


class TestSweepWiring:
    """sweep grace_seconds 与关停 close 的接线验证。

    sweep_interrupted(grace_seconds=...) 的形参由卡B（W15-B）并行落地；server 侧
    以运行时签名探测兼容——本组用假 store 直接验证两条分支与 lifespan 关停路径，
    不依赖卡B 的实现进度。
    """

    def test_sweep_passes_grace_from_env_and_closes_on_shutdown(
        self, clean_env, monkeypatch
    ):
        calls: dict = {}

        class _FakeStore:
            def sweep_interrupted(self, grace_seconds: int = 0) -> int:
                calls["grace"] = grace_seconds
                return 0

            def close(self) -> None:
                calls["closed"] = True

        monkeypatch.setenv("CODEAUDIT_SWEEP_GRACE_SEC", "90")
        monkeypatch.setattr(server_app, "_STORE", _FakeStore())
        with TestClient(server_app.create_app()):
            assert calls["grace"] == 90  # 启动 sweep 携带 env 解析出的 grace
        assert calls.get("closed") is True  # yield 后调用 close（关停接线）

    def test_sweep_falls_back_to_legacy_signature(self, clean_env, monkeypatch):
        calls: list[str] = []

        class _LegacyStore:
            def sweep_interrupted(self) -> int:  # 旧签名（无 grace_seconds 形参）
                calls.append("legacy-sweep")
                return 0

            def close(self) -> None:
                calls.append("closed")

        monkeypatch.setattr(server_app, "_STORE", _LegacyStore())
        # env 有值但旧签名不支持 → 回落无参调用（既有语义），不抛 TypeError
        monkeypatch.setenv("CODEAUDIT_SWEEP_GRACE_SEC", "90")
        with TestClient(server_app.create_app()):
            pass
        assert calls == ["legacy-sweep", "closed"]


# ---------------------------------------------------------------- SSE 并发上限


def _wait_sse_active(count: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while server_app._SSE_ACTIVE < count and time.time() < deadline:
        time.sleep(0.02)
    assert server_app._SSE_ACTIVE >= count, (
        f"SSE 名额未达 {count}（当前 {server_app._SSE_ACTIVE}）"
    )


def _wait_sse_zero(timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while server_app._SSE_ACTIVE > 0 and time.time() < deadline:
        time.sleep(0.02)
    assert server_app._SSE_ACTIVE == 0, "SSE 名额未归还（泄漏）"


class TestSseLimit:
    def test_over_limit_429_and_slot_released(
        self, clean_env, store, seed_task, monkeypatch
    ):
        """真实长驻流占用名额：超限新建 429；流收尾后名额归还。"""
        monkeypatch.setattr(server_app, "_MAX_SSE_STREAMS", 1)
        monkeypatch.setattr(server_app, "_SSE_ACTIVE", 0)
        # running 任务 → 事件流持续跟随不收尾（名额被真实占用）
        seed_task("ssew15a", status="running")
        app = server_app.create_app()
        opener = TestClient(app)
        prober = TestClient(app)
        stream_open = threading.Event()

        def consume() -> None:
            with opener.stream("GET", "/api/audits/ssew15a/events") as resp:
                assert resp.status_code == 200
                stream_open.set()
                for _ in resp.iter_lines():
                    pass  # 阻塞跟随直到任务被 DELETE（流收尾）

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        try:
            _wait_sse_active(1)
            limited = prober.get("/api/audits/ssew15a/events")
            assert limited.status_code == 429
            assert "上限" in limited.json()["detail"]
        finally:
            # 删行 → 流收尾（entry is None → return）→ 名额归还
            assert store.delete("ssew15a") is True
            thread.join(timeout=5)
            assert not thread.is_alive(), "SSE 消费线程未按预期结束"
        _wait_sse_zero()

    def test_normal_stream_completes_and_releases(
        self, clean_env, store, seed_task, client, monkeypatch
    ):
        """终态任务：流回放完 done 事件即收尾，名额归零（一次请求即进出）。"""
        monkeypatch.setattr(server_app, "_MAX_SSE_STREAMS", 1)
        monkeypatch.setattr(server_app, "_SSE_ACTIVE", 0)
        seed_task("ssew15b", status="done")
        with client.stream("GET", "/api/audits/ssew15b/events") as resp:
            assert resp.status_code == 200
            body = "".join(chunk for chunk in resp.iter_text())
        assert '"type": "done"' in body
        _wait_sse_zero()
        # 名额已归还 → 新建流不受限
        assert client.get("/api/health").status_code == 200
