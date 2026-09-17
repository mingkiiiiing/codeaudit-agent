"""W12-A1（F6）新增用例：任务 config_json 落库前脱敏（docs/17 §1.2）。

验收口径（契约 docs/17 §2）——db 中任何路径都取不到明文 Key：
- store API 口径：get/list 返回的 config_json 不含明文 Key，api_key 为 "<redacted>"；
- 绕过 store 层：直接 sqlite3 裸连 db 文件，全表扫描 config_json 列；
- 字节级兜底：对 db 全部文件（主库 + WAL + SHM）做全文扫描，不含测试假 Key 明文；
- 运行时语义断言：任务执行（假流水线实收对象）用的是创建请求时构造的原始
  config（内存中带真实 Key），db 中的 "<redacted>" 仅作审计痕迹——store 里
  任何值都取不到明文 Key。

全程零网络：run_audit 被替换为假协程，假 Key 仅用于断言序列化路径。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from audit.models import AuditReport
from server import app as server_app

# 测试假 Key（绝不真实；流水线被假替身替换，零网络）
_FAKE_KEY = "sk-test-secret-123"  # codeaudit: ignore[PY-HARDCODED-SECRET] 脱敏测试的假密钥夹具，非真实凭据（W21 卡2 定性）
_REDACTED = "<redacted>"


@pytest.fixture
def fake_pipeline(monkeypatch):
    """假流水线：记录收到的 config（验证运行时拿到原始带 Key 对象）并返回最小报告。"""
    seen: list = []

    async def fake_run_audit(config, emitter):
        seen.append(config)
        await emitter({"type": "progress", "stage": "ingest", "message": "假阶段"})
        return AuditReport(
            audit_id="redact01",
            project_name="demo_proj",
            languages={"python": 100.0},
            loc=1,
            health_score=100.0,
            summary={"critical": 0, "high": 0, "medium": 0, "low": 0},
            issues=[],
        )

    monkeypatch.setattr(server_app, "run_audit", fake_run_audit)
    return seen


def _zip_bytes(content: str = "x = 1\n") -> bytes:
    """最小合法 zip 源码包（对齐本目录既有工厂写法）。"""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mini_app/main.py", content)
    return buf.getvalue()


def _wait_terminal(client: TestClient, audit_id: str, timeout: float = 10.0) -> dict:
    """轮询等待任务到达终态（无固定 sleep 同步，对齐本目录风格）。"""
    deadline = time.time() + timeout
    data: dict = {}
    while time.time() < deadline:
        data = client.get(f"/api/audits/{audit_id}").json()
        if data["status"] in ("done", "failed"):
            return data
        time.sleep(0.05)
    raise AssertionError(f"任务未在时限内到达终态：{data}")


def test_config_json_redacted_across_store_sql_and_db_bytes(
    store, fake_pipeline, monkeypatch, tmp_path
):
    """创建任务后三口径扫描：store 元数据 / 裸 SQL 全表 / db 文件字节，均无明文 Key。"""
    monkeypatch.setenv("GLM_API_KEY", _FAKE_KEY)
    with TestClient(server_app.create_app()) as client:
        audit_id = client.post(
            "/api/audits", json={"source_path": str(tmp_path)}
        ).json()["audit_id"]
        assert _wait_terminal(client, audit_id)["status"] == "done"

        # a) store API 口径：config_json 已脱敏。
        # W15-A5：server.app lifespan 在 with 退出（yield 后）即关闭 store——
        # store API 的访问须在 with 块内完成（close 幂等，fixture teardown 重复关无妨）。
        row = store.get(audit_id)
        assert row is not None
        assert _FAKE_KEY not in row["config_json"]
        cfg_json = json.loads(row["config_json"])
        assert cfg_json["api_key"] == _REDACTED

        # b) 绕过 store 层：sqlite3 裸连 db，config_json 列全表扫描
        # （连接须显式关闭：`with conn` 只管事务；不关会阻止后续 WAL checkpoint）
        db_path: Path = store._db_path
        assert db_path.is_file()
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute("SELECT config_json FROM audits").fetchall()
        finally:
            conn.close()
        assert rows, "audits 表为空，任务行未落库"
        for (raw,) in rows:
            assert _FAKE_KEY not in raw
            assert _REDACTED in raw

        # c) 运行时语义：假流水线收到的是创建请求时的原始 config（真实 Key 在内存对象上）
        assert fake_pipeline, "任务未被执行"
        assert all(cfg.api_key == _FAKE_KEY for cfg in fake_pipeline)

    # d) 字节级兜底：W15-A5 起 store 由 lifespan 退出时关闭（等价原 store.close()
    # 促发 WAL checkpoint），扫描主库 + WAL + SHM 全部字节
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if not path.is_file():
            continue
        assert _FAKE_KEY.encode("utf-8") not in path.read_bytes(), f"{path.name} 含明文 Key"
    assert _REDACTED.encode("utf-8") in db_path.read_bytes()


def test_upload_endpoint_config_json_also_redacted(
    store, fake_pipeline, monkeypatch, work_root_tmp
):
    """上传建任务（/api/audits/upload）与直接创建共用 _start_audit，同样脱敏。"""
    monkeypatch.setenv("GLM_API_KEY", _FAKE_KEY)
    zip_bytes = _zip_bytes()
    with TestClient(server_app.create_app()) as client:
        audit_id = client.post(
            "/api/audits/upload",
            files={"file": ("src.zip", zip_bytes, "application/zip")},
        ).json()["audit_id"]
        assert _wait_terminal(client, audit_id)["status"] == "done"

        # W15-A5：store API 访问须在 with 块内完成（lifespan 退出即关闭 store）
        row = store.get(audit_id)
        assert row is not None
        assert _FAKE_KEY not in row["config_json"]
        assert json.loads(row["config_json"])["api_key"] == _REDACTED
    assert all(cfg.api_key == _FAKE_KEY for cfg in fake_pipeline)
