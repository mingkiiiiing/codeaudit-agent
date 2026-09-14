"""联调场景⑩（R2 必测场景 10）：并发与规模保护（真实流水线，无 LLM）。

- server 3 任务并发互不串扰：三个不同项目同时审计，各自报告/事件互不污染；
- 超限 zip（>2000 文件）→ ingest 阶段 IngestError 记入 stage error 事件 →
  空报告仍产出、任务终态 done、服务不崩；
- 任务表终态清理（ISSUE-R1-6）：容量 50 淘汰最旧终态项（W11 起经 store.prune）。

W11 最小适配：任务表为 TaskStore——autouse fixture 为每个用例注入独立临时库
（monkeypatch server_app._STORE）；后台任务在独立线程执行，TestClient 以上下文
管理器持有跨请求存活的 portal；事件断言改读 store（get_events）。
"""

from __future__ import annotations

import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from audit.taskstore import TaskStore
from server import app as server_app


@pytest.fixture(autouse=True)
def clean_audits(monkeypatch, tmp_path: Path):
    """W11 最小适配：前后替换 server_app._STORE 为用例独立临时库，避免用例串扰。"""
    st = TaskStore(tmp_path / "api-server.sqlite")
    monkeypatch.setattr(server_app, "_STORE", st)
    yield
    st.close()


def _wait_terminal(client: TestClient, audit_id: str, timeout: float = 90.0) -> dict:
    """条件等待任务到终态（轮询间隔 0.05s，非固定 sleep 断言时序）。"""
    deadline = time.monotonic() + timeout
    data: dict = {}
    while time.monotonic() < deadline:
        data = client.get(f"/api/audits/{audit_id}").json()
        if data["status"] in ("done", "failed"):
            return data
        time.sleep(0.05)
    raise AssertionError(f"等待任务终态超时：{audit_id} -> {data}")


def _post(client: TestClient, source: Path) -> str:
    resp = client.post("/api/audits", json={"source_path": str(source)})
    assert resp.status_code == 200
    return resp.json()["audit_id"]


# ---------------------------------------------------------------- 并发互不串扰


def test_three_concurrent_audits_no_cross_talk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_project
) -> None:
    monkeypatch.chdir(tmp_path)  # server 默认 work_root=".codeaudit" → 钉在 tmp_path

    # 三个项目：各自的缺陷文件名不同，作为"串扰探针"
    names = ("alpha", "beta", "gamma")
    projects = [
        make_project(
            {f"{n}_service.py": f"def collect_{n}(items, bucket=[]):\n    bucket.extend(items)\n    return bucket\n"},
            name=n,
        )
        for n in names
    ]

    with TestClient(server_app.create_app()) as client:
        audit_ids = {n: _post(client, p) for n, p in zip(names, projects, strict=True)}

        results = {n: _wait_terminal(client, audit_id) for n, audit_id in audit_ids.items()}

        for n in names:
            data = results[n]
            assert data["status"] == "done", (n, data)
            assert data["error"] is None
            report = data["report"]
            assert report["project_name"] == projects[names.index(n)].name, n
            # 问题全部来自本项目文件：没有任何"别家"文件混入
            issue_files = {i["file"] for i in report["issues"]}
            assert issue_files == {f"{n}_service.py"}, (n, issue_files)
            assert report["summary"]["high"] == 1

        # 三个任务的报告互不相同（audit_id 各自独立）
        assert len({results[n]["report"]["audit_id"] for n in names}) == 3
        # 每个任务的事件流各自非空（事件持久化于 store，emitter 与任务一一对应）
        store = server_app._get_store()
        for n, audit_id in audit_ids.items():
            assert store.get_events(audit_id), n


# ---------------------------------------------------------------- 规模保护：超限 zip


def test_oversize_zip_ingest_error_yields_empty_report_but_no_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """>2000 文件的 zip：IngestError 进 stage error 事件 → 空报告不崩（status=done）。"""
    monkeypatch.chdir(tmp_path)

    zip_path = tmp_path / "oversize.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for i in range(2001):  # ingest 默认上限 max_files=2000
            zf.writestr(f"pkg{i % 37}/mod_{i}.py", f"x_{i} = {i}\n")

    with TestClient(server_app.create_app()) as client:
        audit_id = _post(client, zip_path)
        data = _wait_terminal(client, audit_id)

        # 不崩：终态 done（而非 failed），报告为空
        assert data["status"] == "done", data
        assert data["error"] is None
        assert data["report"]["issues"] == []
        assert sum(data["report"]["summary"].values()) == 0

        # IngestError 被记录为 stage error（pipeline 记入 stage_errors 并发 error 事件，
        # 事件持久化于 store）
        events = [e for _seq, e in server_app._get_store().get_events(audit_id)]
        error_events = [e for e in events if e.get("error")]
        assert any("IngestError" in str(e.get("message", "")) for e in error_events), error_events
        assert any(e["stage"] == "ingest" for e in error_events)
        # 后续阶段照常走到 done 事件
        assert any(e.get("stage") == "done" for e in events)


# ---------------------------------------------------------------- 任务表终态清理（ISSUE-R1-6，已修复）


def test_terminal_audits_evicted_under_lru_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_project) -> None:
    """终态清理契约（R1-6 已修复）：任务表按容量 50 淘汰最旧终态项（W11 起 store.prune）。

    预置 50 个终态任务行占满容量，再完成 1 个新任务 → 最旧的终态行被淘汰，
    表长不超过容量，且新任务可正常查询。
    """
    monkeypatch.chdir(tmp_path)
    with TestClient(server_app.create_app()) as client:
        cap = server_app._AUDITS_MAX
        store = server_app._get_store()

        for i in range(cap):
            store.create(
                f"prefilled-{i}",
                status="done",
                created_at=f"2026-01-01T{i // 60:02d}:{i % 60:02d}:00+08:00",
                source_path="",
                do_fix=False,
                do_tests=False,
            )

        project = make_project({"mod.py": "value = 1\n"}, name="clean-last")
        audit_id = _post(client, project)
        data = _wait_terminal(client, audit_id)
        assert data["status"] == "done"

        total, _items = store.list(0, 0)
        assert total <= cap  # 容量上限生效
        assert store.get("prefilled-0") is None  # 最旧终态项被淘汰
        assert store.get(audit_id)["status"] == "done"  # 新任务可查询
