"""W27-B 报告历史版本化 server 端点自测：历史列表 / 指定版本的 200 与 404、鉴权跟随。

conftest 的 autouse ``store`` fixture 已注入 tmp 库（不落仓库根 .codeaudit）；
造数直接经 store.set_report 两次（seq 1/2），不依赖流水线执行，全程离线。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from audit.models import AuditReport, Category, Issue, Severity
from server import app as server_app


def _report(audit_id: str, *, health: float, title: str) -> AuditReport:
    return AuditReport(
        audit_id=audit_id,
        project_name="proj",
        languages={"python": 1.0},
        loc=100,
        health_score=health,
        summary={"high": 1},
        issues=[
            Issue(
                id="i1",
                category=Category.BUG,
                severity=Severity.HIGH,
                title=title,
                file="app/services/users.py",
                line_start=10,
                line_end=12,
            )
        ],
        created_at="2026-01-01T00:00:00",
    )


@pytest.fixture
def client() -> TestClient:
    return TestClient(server_app.create_app())


@pytest.fixture
def task_with_two_versions(store):
    """预置一个 done 任务并落两版报告（seq 1/2），供 200 断言用。"""
    store.create(
        "hist01",
        status="done",
        error=None,
        created_at="2026-01-01T00:00:00",
        source_path="seeded",
        do_fix=False,
        do_tests=False,
    )
    store.set_report("hist01", _report("hist01", health=88.5, title="第一版问题"))
    store.set_report("hist01", _report("hist01", health=70.0, title="第二版问题"))
    return store


# ---------------------------------------------------------------- 列表端点
def test_list_reports_200_with_two_versions(client, task_with_two_versions):
    resp = client.get("/api/audits/hist01/reports")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert [r["seq"] for r in data["reports"]] == [1, 2]
    for item in data["reports"]:
        assert set(item.keys()) == {"seq", "created_at", "health_score", "issue_count"}
        assert "report_json" not in item  # 摘要面不含报告全文
    assert data["reports"][0]["health_score"] == 88.5
    assert data["reports"][1]["health_score"] == 70.0


def test_list_reports_200_empty_when_task_has_no_version(client, store):
    """任务存在但尚未落任何报告：200 + 空列表（非 404）。"""
    store.create(
        "fresh01",
        status="queued",
        error=None,
        created_at="2026-01-01T00:00:00",
        source_path="seeded",
        do_fix=False,
        do_tests=False,
    )
    resp = client.get("/api/audits/fresh01/reports")
    assert resp.status_code == 200
    assert resp.json() == {"total": 0, "reports": []}


def test_list_reports_404_for_unknown_task(client):
    assert client.get("/api/audits/ghost/reports").status_code == 404


# ---------------------------------------------------------------- 版本端点
def test_get_report_version_200_returns_full_json(client, task_with_two_versions):
    resp = client.get("/api/audits/hist01/reports/1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["health_score"] == 88.5
    assert data["issues"][0]["title"] == "第一版问题"
    # seq=2 为第二版；形态与当前报告 JSON 同源（report.to_dict() 形状）
    resp2 = client.get("/api/audits/hist01/reports/2")
    assert resp2.status_code == 200
    assert resp2.json()["issues"][0]["title"] == "第二版问题"
    current_keys = set(
        client.get("/api/audits/hist01/report").json().keys()  # 既有当前报告端点
    )
    assert current_keys == set(data.keys())


def test_get_report_version_404_for_unknown_version_or_task(client, task_with_two_versions):
    assert client.get("/api/audits/hist01/reports/999").status_code == 404  # 版本越界
    assert client.get("/api/audits/hist01/reports/0").status_code == 404  # seq 从 1 起
    assert client.get("/api/audits/ghost/reports/1").status_code == 404  # 任务不存在


def test_get_report_version_422_for_non_integer_seq(client, task_with_two_versions):
    """seq 非整数：FastAPI 路径校验 422（与 patches/{patch_index} int 形参既有约定一致）。"""
    assert client.get("/api/audits/hist01/reports/abc").status_code == 422


# ---------------------------------------------------------------- 鉴权跟随
def test_reports_endpoints_follow_api_auth(client, monkeypatch):
    """鉴权跟随既有 /api/* 中间件：token 非空时无凭据 401，凭据通过后按任务存在性 404。"""
    monkeypatch.setenv("CODEAUDIT_API_TOKEN", "s3cret-token")
    assert client.get("/api/audits/x/reports").status_code == 401
    assert client.get("/api/audits/x/reports/1").status_code == 401
    ok = client.get("/api/audits/x/reports", headers={"Authorization": "Bearer s3cret-token"})
    assert ok.status_code == 404
