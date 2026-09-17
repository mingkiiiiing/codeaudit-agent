"""P0-2 POST /api/audits/{id}/patches/{n}/apply 端点自测（离线，TestClient）。

覆盖：dry-run 不落盘 / yes 落盘 / 序号越界 404 / 任务不存在 404 /
zip 源（非目录）400 / 老补丁无指纹 --yes 拒绝 / 重复应用防覆盖（指纹漂移拒绝）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from audit.models import AuditReport, Patch
from server import app as server_app

AID = "apiapply01"

APP_PY = """import logging

logger = logging.getLogger(__name__)


def divide(a, b):
    try:
        return a / b
    except:
        return None
"""

BARE_EXCEPT_DIFF = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -6,5 +6,6 @@\n"
    " def divide(a, b):\n"
    "     try:\n"
    "         return a / b\n"
    "-    except:\n"
    "+    except Exception:\n"
    '+        logger.exception("divide failed")\n'
    "         return None\n"
)

_FIXED_APP_PY = APP_PY.replace(
    "    except:\n", '    except Exception:\n        logger.exception("divide failed")\n'
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_lf(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _patch(sha_map: dict[str, str], *, status: str = "verified") -> Patch:
    return Patch(
        id="PATCH-0001",
        issue_id="ISS-0001",
        diff=BARE_EXCEPT_DIFF,
        rationale="API 测试补丁",
        apply_status=status,
        target_sha256=sha_map,
    )


def _seed(store, tmp_path: Path, patches: list[Patch], *, source: Path | None = None):
    """写入任务行 + 报告，并准备目标源码目录；返回源码目录。"""
    proj = tmp_path / "proj"
    _write_lf(proj / "app.py", APP_PY)
    report = AuditReport(audit_id=AID, project_name="proj", patches=patches)
    store.create(
        AID,
        status="done",
        created_at="2026-01-01T00:00:00+08:00",
        source_path=str(source if source is not None else proj),
        do_fix=True,
        do_tests=False,
    )
    store.set_report(AID, report)
    return proj


def _client() -> TestClient:
    return TestClient(server_app.create_app())


def test_api_apply_dry_run_default_no_write(store, tmp_path):
    proj = _seed(store, tmp_path, [_patch({"app.py": _sha(APP_PY)})])
    before = (proj / "app.py").read_bytes()
    with _client() as client:
        resp = client.post(f"/api/audits/{AID}/patches/0/apply")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["dry_run"] is True
    assert payload["applied"] == [] and payload["rejected"] == []
    assert (proj / "app.py").read_bytes() == before


def test_api_apply_yes_writes_source(store, tmp_path):
    proj = _seed(store, tmp_path, [_patch({"app.py": _sha(APP_PY)})])
    with _client() as client:
        resp = client.post(f"/api/audits/{AID}/patches/0/apply", json={"yes": True})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["dry_run"] is False
    assert len(payload["applied"]) == 1
    assert payload["applied"][0]["patch_id"] == "PATCH-0001"
    assert (proj / "app.py").read_text(encoding="utf-8") == _FIXED_APP_PY


def test_api_apply_reapply_rejected_by_fingerprint(store, tmp_path):
    """重复应用：目标已是补丁后内容 → sha256 漂移拒绝（防重复/防覆盖）。"""
    _seed(store, tmp_path, [_patch({"app.py": _sha(APP_PY)})])
    with _client() as client:
        first = client.post(f"/api/audits/{AID}/patches/0/apply", json={"yes": True})
        assert first.status_code == 200
        second = client.post(f"/api/audits/{AID}/patches/0/apply", json={"yes": True})
    assert second.status_code == 200
    payload = second.json()
    assert payload["applied"] == [] and len(payload["rejected"]) == 1
    assert "sha256 与审计时不一致" in payload["rejected"][0]["reason"]


def test_api_apply_index_out_of_range_404(store, tmp_path):
    _seed(store, tmp_path, [_patch({"app.py": _sha(APP_PY)})])
    with _client() as client:
        resp = client.post(f"/api/audits/{AID}/patches/9/apply", json={"yes": True})
    assert resp.status_code == 404
    assert "越界" in resp.json()["detail"]


def test_api_apply_audit_not_found_404(store, tmp_path):
    with _client() as client:
        resp = client.post("/api/audits/ghost0099/patches/0/apply")
    assert resp.status_code == 404


def test_api_apply_zip_source_rejected_400(store, tmp_path):
    zip_file = tmp_path / "bundle.zip"
    zip_file.write_bytes(b"PK\x05\x06")  # 仅作占位文件（端点按"非目录"拒绝）
    _seed(store, tmp_path, [_patch({"app.py": _sha(APP_PY)})], source=zip_file)
    with _client() as client:
        resp = client.post(f"/api/audits/{AID}/patches/0/apply", json={"yes": True})
    assert resp.status_code == 400
    assert "不是本机目录" in resp.json()["detail"]


def test_api_apply_legacy_patch_yes_rejected(store, tmp_path):
    legacy = Patch(id="PATCH-0001", issue_id="ISS-0001", diff=BARE_EXCEPT_DIFF, apply_status="verified")
    proj = _seed(store, tmp_path, [legacy])
    with _client() as client:
        dry = client.post(f"/api/audits/{AID}/patches/0/apply")
        yes = client.post(f"/api/audits/{AID}/patches/0/apply", json={"yes": True})
    assert dry.status_code == 200 and "缺少原文指纹" in dry.json()["rejected"][0]["reason"]
    assert yes.status_code == 200 and "重新审计" in yes.json()["rejected"][0]["reason"]
    assert (proj / "app.py").read_text(encoding="utf-8") == APP_PY  # 未落盘


def test_api_report_serialization_roundtrip_keeps_fingerprint(store, tmp_path):
    """指纹经 to_dict/from_dict 序列化往返不丢（API 层数据完整性）。"""
    _seed(store, tmp_path, [_patch({"app.py": _sha(APP_PY)})])
    report = store.get_report(AID)
    assert report is not None
    restored = AuditReport.from_dict(json.loads(json.dumps(report.to_dict())))
    assert restored.patches[0].target_sha256 == {"app.py": _sha(APP_PY)}
