"""W2-A6 新端点自测：/issues 过滤分页、/patches、/summary 与 404/400 一致性。

直接向注入的 TaskStore（conftest autouse store fixture）预置"已完成/运行中"任务
（不触发流水线，经 seed_task 工厂落库），零网络。W11 适配：AUDITS 内存表 → store。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from audit.models import (
    AuditReport,
    AuditStats,
    Category,
    FixStatus,
    Issue,
    IssueSource,
    Patch,
    Severity,
)
from server import app as server_app

DONE_ID = "done0001"
RUNNING_ID = "runn0001"


def _make_report() -> AuditReport:
    """5 个问题覆盖全部严重度与 3 个类别，1 个已验证补丁。"""
    return AuditReport(
        audit_id=DONE_ID,
        project_name="demo_proj",
        languages={"python": 100.0},
        loc=300,
        health_score=72.5,
        summary={"critical": 1, "high": 2, "medium": 1, "low": 1},
        issues=[
            Issue(id="ISS-0001", category=Category.SECURITY, severity=Severity.CRITICAL,
                  title="硬编码密钥", file="app/config.py", line_start=3, confidence=0.95,
                  source=IssueSource.RULE, fix_status=FixStatus.VERIFIED, patch_id="FIX-0001"),
            Issue(id="ISS-0002", category=Category.BUG, severity=Severity.HIGH,
                  title="未判空", file="app/services/orders.py", line_start=10, confidence=0.9),
            Issue(id="ISS-0003", category=Category.BUG, severity=Severity.HIGH,
                  title="除零风险", file="app/utils/mathx.py", line_start=5, confidence=0.8),
            Issue(id="ISS-0004", category=Category.PERFORMANCE, severity=Severity.MEDIUM,
                  title="循环内重复编译正则", file="app/utils/net.py", line_start=30, confidence=0.7),
            Issue(id="ISS-0005", category=Category.STYLE, severity=Severity.LOW,
                  title="魔法数字", file="app/config.py", line_start=8, confidence=0.6),
        ],
        patches=[
            Patch(id="FIX-0001", issue_id="ISS-0001",
                  diff="--- a/app/config.py\n+++ b/app/config.py\n@@ -3,1 +3,1 @@\n-KEY = 'abc'\n+KEY = os.environ['KEY']",
                  rationale="从环境变量读取密钥", apply_status="verified", tests_run=4, tests_passed=4),
        ],
        stats=AuditStats(duration_sec=12.34, llm_calls=7, prompt_tokens=1200, completion_tokens=340,
                         cache_hits=2, cache_misses=5, files_total=6, loc_total=300),
    )


@pytest.fixture(autouse=True)
def seeded_audits(seed_task):
    """每个用例前注入 1 个已完成 + 1 个运行中的任务（W11：经 store 落库）。"""
    seed_task(DONE_ID, status="done", report=_make_report(), created_at="2026-01-01T00:00:01+08:00")
    seed_task(RUNNING_ID, status="running", created_at="2026-01-01T00:00:02+08:00")
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(server_app.create_app())


# ---------------------------------------------------------------- /issues


def test_issues_default_returns_all_with_pagination_meta(client):
    resp = client.get(f"/api/audits/{DONE_ID}/issues")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 5
    assert data["offset"] == 0 and data["limit"] == 50
    assert [i["id"] for i in data["issues"]] == ["ISS-0001", "ISS-0002", "ISS-0003", "ISS-0004", "ISS-0005"]
    # 枚举字段已序列化为字符串
    assert data["issues"][0]["severity"] == "critical" and data["issues"][0]["category"] == "security"


def test_issues_filter_by_severity(client):
    resp = client.get(f"/api/audits/{DONE_ID}/issues", params={"severity": "high"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert {i["id"] for i in data["issues"]} == {"ISS-0002", "ISS-0003"}
    # 大小写不敏感
    assert client.get(f"/api/audits/{DONE_ID}/issues", params={"severity": "HIGH"}).json()["total"] == 2


def test_issues_filter_by_category_and_severity_combined(client):
    resp = client.get(f"/api/audits/{DONE_ID}/issues", params={"category": "bug"})
    assert resp.json()["total"] == 2
    resp = client.get(f"/api/audits/{DONE_ID}/issues", params={"category": "bug", "severity": "medium"})
    assert resp.json()["total"] == 0 and resp.json()["issues"] == []


def test_issues_pagination_limit_offset(client):
    page1 = client.get(f"/api/audits/{DONE_ID}/issues", params={"limit": 2, "offset": 0}).json()
    page2 = client.get(f"/api/audits/{DONE_ID}/issues", params={"limit": 2, "offset": 2}).json()
    page3 = client.get(f"/api/audits/{DONE_ID}/issues", params={"limit": 2, "offset": 4}).json()
    assert page1["total"] == page2["total"] == page3["total"] == 5
    assert [i["id"] for i in page1["issues"]] == ["ISS-0001", "ISS-0002"]
    assert [i["id"] for i in page2["issues"]] == ["ISS-0003", "ISS-0004"]
    assert [i["id"] for i in page3["issues"]] == ["ISS-0005"]
    # 越界 offset → 空页但 total 仍正确
    beyond = client.get(f"/api/audits/{DONE_ID}/issues", params={"offset": 99}).json()
    assert beyond["issues"] == [] and beyond["total"] == 5


def test_issues_invalid_params_return_400(client):
    r = client.get(f"/api/audits/{DONE_ID}/issues", params={"severity": "fatal"})
    assert r.status_code == 400 and "不支持的 severity" in r.json()["detail"]
    r = client.get(f"/api/audits/{DONE_ID}/issues", params={"category": "smell"})
    assert r.status_code == 400 and "不支持的 category" in r.json()["detail"]
    r = client.get(f"/api/audits/{DONE_ID}/issues", params={"limit": "abc"})
    assert r.status_code == 400 and "整数" in r.json()["detail"]
    assert client.get(f"/api/audits/{DONE_ID}/issues", params={"limit": 0}).status_code == 400
    assert client.get(f"/api/audits/{DONE_ID}/issues", params={"offset": -1}).status_code == 400


# ---------------------------------------------------------------- /patches


def test_patches_contains_diff_and_apply_status(client):
    resp = client.get(f"/api/audits/{DONE_ID}/patches")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    patch = data["patches"][0]
    assert patch["id"] == "FIX-0001" and patch["issue_id"] == "ISS-0001"
    assert patch["apply_status"] == "verified"
    assert patch["diff"].startswith("--- a/app/config.py") and "+KEY = os.environ" in patch["diff"]
    assert patch["tests_passed"] == 4 and patch["tests_run"] == 4


# ---------------------------------------------------------------- /summary


def test_summary_fields_for_dashboard_header(client):
    resp = client.get(f"/api/audits/{DONE_ID}/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["audit_id"] == DONE_ID and data["project_name"] == "demo_proj"
    assert data["health_score"] == 72.5
    assert data["summary"] == {"critical": 1, "high": 2, "medium": 1, "low": 1}
    assert data["total_issues"] == 5 and data["loc"] == 300
    assert data["duration_sec"] == 12.34
    assert data["tokens"] == {
        "llm_calls": 7, "prompt_tokens": 1200, "completion_tokens": 340,
        "cache_hits": 2, "cache_misses": 5,
    }


# ---------------------------------------------------------------- 404 一致性


@pytest.mark.parametrize("suffix", ["issues", "patches", "summary"])
def test_unknown_audit_returns_404_with_same_message(client, suffix):
    resp = client.get(f"/api/audits/no-such-id/{suffix}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "任务不存在：no-such-id"


@pytest.mark.parametrize("suffix", ["issues", "patches", "summary"])
def test_not_done_audit_returns_404_like_report(client, suffix):
    """未完成任务：与 /report 端点同样 404 + '任务未完成：<status>'。"""
    resp = client.get(f"/api/audits/{RUNNING_ID}/{suffix}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "任务未完成：running"
    # 与既有 /report 端点完全一致
    assert client.get(f"/api/audits/{RUNNING_ID}/report").json()["detail"] == "任务未完成：running"


def test_unknown_audit_404_takes_precedence_over_bad_params(client):
    """任务不存在时优先 404，不因参数非法而变成 400。"""
    resp = client.get("/api/audits/no-such-id/issues", params={"severity": "fatal"})
    assert resp.status_code == 404
