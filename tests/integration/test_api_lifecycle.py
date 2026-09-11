"""联调场景④（R2 必测场景 4）：API 完整链路（真实流水线，无 LLM）。

POST /api/audits → SSE 收齐七阶段进度事件到 {"type": "done"} →
GET /api/audits/{id}/report 三种格式 → GET /api/audits/{id}/issues
过滤/分页，total 与 summary 计数一致。

要点：
- 不注入假流水线：server.app.run_audit 原样执行真实七阶段流水线；
- monkeypatch.chdir(tmp_path)：server 侧 AuditConfig.from_env 的 work_root 默认为
  ".codeaudit"（相对 CWD），必须把 CWD 钉在 tmp_path，保证零仓库根污染
  （与 test_hygiene 的卫生约束配套）；
- GLM_API_KEY 已被 conftest 消毒 → 纯规则模式，全程零网络。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app as server_app

# 已知缺陷样本：可变默认参数（high）+ 裸 except（medium）
_ISSUE_PY = (
    "def collect(items, bucket=[]):\n"
    "    bucket.extend(items)\n"
    "    return bucket\n"
    "\n"
    "\n"
    "def load(path):\n"
    "    try:\n"
    "        return open(path).read()\n"
    "    except:\n"
    "        return None\n"
)


@pytest.fixture(autouse=True)
def clean_audits():
    """前后清空共享任务表（server.app.AUDITS 为模块级内存表），避免用例串扰。"""
    server_app.AUDITS.clear()
    yield
    server_app.AUDITS.clear()


@pytest.fixture
def project(tmp_path: Path, make_project) -> Path:
    return make_project({"app/worker.py": _ISSUE_PY}, name="api_proj")


def _post_audit(client: TestClient, source: Path) -> str:
    resp = client.post("/api/audits", json={"source_path": str(source)})
    assert resp.status_code == 200
    audit_id = resp.json()["audit_id"]
    assert audit_id
    return audit_id


def _collect_sse_to_done(client: TestClient, audit_id: str) -> list[dict]:
    """SSE 事件流读到 {"type": "done"} 为止（服务端轮询推送，事件/条件等待，无固定 sleep）。"""
    payloads: list[dict] = []
    with client.stream("GET", f"/api/audits/{audit_id}/events") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        for line in resp.iter_lines():
            if line.startswith("data:"):
                payloads.append(json.loads(line[len("data:") :].strip()))
                if payloads[-1].get("type") == "done":
                    return payloads
    raise AssertionError("SSE 流在 done 之前关闭")


def test_api_full_lifecycle_with_real_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # work_root=".codeaudit" 相对 CWD → 钉在 tmp_path
    client = TestClient(server_app.create_app())

    # ---- 创建任务（真实流水线，后台执行）
    audit_id = _post_audit(client, project)

    # ---- SSE 收齐七阶段事件到 done
    payloads = _collect_sse_to_done(client, audit_id)
    assert payloads[-1] == {"type": "done"}
    progress = [p for p in payloads if p.get("type") == "progress"]
    stages = list(dict.fromkeys(p["stage"] for p in progress))
    assert stages == ["init", "ingest", "index", "understand", "detect", "fix", "testgen", "report", "done"]
    # 检测阶段事件携带真实问题计数（>0，样本含已知缺陷）
    detect_events = [p for p in progress if p["stage"] == "detect" and str(p.get("message", "")).startswith("检测完成")]
    assert detect_events and detect_events[-1].get("total", 0) > 0
    # 纯规则模式警告（GLM_API_KEY 已消毒 → LLM 未配置）
    assert any("LLM 未配置" in p.get("message", "") for p in progress)

    # ---- 任务状态与报告
    data = client.get(f"/api/audits/{audit_id}").json()
    assert data["status"] == "done"
    assert data["error"] is None
    assert data["report"]["project_name"] == project.name
    assert data["report"]["summary"]["high"] >= 1

    # ---- 三种格式报告
    r_json = client.get(f"/api/audits/{audit_id}/report", params={"format": "json"})
    assert r_json.status_code == 200
    report = r_json.json()
    assert report["audit_id"] == data["report"]["audit_id"]
    assert sum(report["summary"].values()) == len(report["issues"])

    r_md = client.get(f"/api/audits/{audit_id}/report", params={"format": "md"})
    assert r_md.status_code == 200
    assert "text/markdown" in r_md.headers["content-type"]
    assert "健康分" in r_md.text

    r_html = client.get(f"/api/audits/{audit_id}/report", params={"format": "html"})
    assert r_html.status_code == 200
    assert "<html" in r_html.text.lower()

    assert client.get(f"/api/audits/{audit_id}/report", params={"format": "xml"}).status_code == 400

    # ---- /issues 过滤：total 与 summary 一致
    summary = report["summary"]
    for severity, expected in summary.items():
        resp = client.get(
            f"/api/audits/{audit_id}/issues", params={"severity": severity, "limit": 1}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == expected, (severity, body["total"], expected)
        assert len(body["issues"]) <= 1

    # ---- /issues 分页：翻页遍历不重不漏
    total_all = client.get(f"/api/audits/{audit_id}/issues", params={"limit": 1}).json()["total"]
    seen_ids: list[str] = []
    offset = 0
    while True:
        page = client.get(
            f"/api/audits/{audit_id}/issues", params={"limit": 1, "offset": offset}
        ).json()
        assert page["total"] == total_all
        seen_ids.extend(i["id"] for i in page["issues"])
        if not page["issues"]:
            break
        offset += 1
        if offset > total_all:
            break
    assert len(seen_ids) == total_all
    assert len(set(seen_ids)) == total_all

    # ---- /issues 非法过滤值 → 400（契约语义）
    assert client.get(f"/api/audits/{audit_id}/issues", params={"severity": "sev0"}).status_code == 400
    assert client.get(f"/api/audits/{audit_id}/issues", params={"limit": "0"}).status_code == 400


def test_api_audit_completes_asynchronously(monkeypatch: pytest.MonkeyPatch, project: Path, tmp_path: Path) -> None:
    """创建后立即查询：任务从 queued/running 推进到 done（条件等待，无固定 sleep）。"""
    monkeypatch.chdir(tmp_path)
    client = TestClient(server_app.create_app())
    audit_id = _post_audit(client, project)

    deadline = time.monotonic() + 60.0
    data: dict = {}
    while time.monotonic() < deadline:
        data = client.get(f"/api/audits/{audit_id}").json()
        if data["status"] in ("done", "failed"):
            break
        time.sleep(0.05)  # 轮询间隔（条件等待），非时序断言
    assert data["status"] == "done", data
    assert data["report"]["issues"], "真实流水线应检出样本缺陷"
