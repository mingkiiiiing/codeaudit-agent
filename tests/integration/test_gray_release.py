"""灰度发布保障测试（W9-C1）：契约向后兼容 / 特性开关 A/B 等价 / API 路由面冻结。

灰度三层防线在本仓库的落点：
- C1 契约向后兼容：v0.3 形态的旧版报告 dict（缺 v1.7 的 architecture /
  refactor_proposals 字段）必须能被当前 AuditReport.from_dict 反序列化，且
  markdown / html / sarif 三种渲染不炸——旧产物在新版本上永远可读可渲染；
- C2 特性开关 A/B 等价性：实验开关（rule_scan_workers 规则扫描并行度、
  ingest 硬链接物化）切换不得改变审计语义——同输入两路报告逐字段一致
  （时延类字段除外），这是灰度期双路对拍、按流量百分比放量不改结果的前提；
- C3 API 路由面冻结：server/app.py 的 /api 路由集合与契约 v2 冻结清单精确
  相等（多删都红）——金丝雀部署前后任何路由意外漂移在 CI 即拦截。

全程离线（GLM_* 已被 tests/integration/conftest 消毒）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from audit.config import AuditConfig
from audit.ingest.core import ingest
from audit.models import AuditReport
from audit.orchestrator.pipeline import run_audit
from audit.report.render import render_html, render_markdown
from audit.report.sarif import render_sarif

ROOT = Path(__file__).resolve().parents[2]
MINI_APP = ROOT / "demo" / "mini_app"

# 契约 v2 冻结的 API 路由面（server/app.py 必须精确提供，不多不少）
FROZEN_API_ROUTES = {
    "/api/health",
    "/api/audits",
    "/api/audits/upload",
    "/api/audits/{audit_id}",
    "/api/audits/{audit_id}/events",
    "/api/audits/{audit_id}/report",
    "/api/audits/{audit_id}/issues",
    "/api/audits/{audit_id}/patches",
    # P0-2（apply-to-source 预览确认回路）：补丁回写源码端点，语义与 CLI apply 一致
    "/api/audits/{audit_id}/patches/{patch_index}/apply",
    "/api/audits/{audit_id}/summary",
    "/api/audits/{audit_id}/understand",
    "/api/audits/{audit_id}/refactors",
}


async def _noop_emitter(event: dict) -> None:  # pragma: no cover —— 占位
    pass


def _audit(source: Path, work: Path, **overrides: object) -> AuditReport:
    cfg = AuditConfig(
        source_path=str(source),
        work_root=str(work),
        out_dir=str(work / "reports"),
        enable_llm_review=False,
        **overrides,
    )
    return asyncio.run(run_audit(cfg, _noop_emitter))


# ---------------------------------------------------------------- C1 契约向后兼容
_LEGACY_REPORT_V03 = {
    "audit_id": "legacy-0001",
    "project_name": "legacy-app",
    "languages": {"python": 100.0},
    "loc": 42,
    "health_score": 61.9,
    "summary": {"critical": 1, "high": 0, "medium": 1, "low": 0},
    "issues": [
        {
            "id": "py-sqli-001",
            "category": "security",
            "severity": "critical",
            "title": "SQL 拼接注入",
            "file": "store.py",
            "line_start": 5,
            "line_end": 5,
            "code_snippet": "query = 'SELECT * FROM t WHERE n=' + name",
            "description": "字符串拼接构造 SQL",
            "evidence": ["concat at line 5"],
            "suggestion": "使用参数化查询",
            "confidence": 0.9,
            "source": "rule",
            "fix_status": "none",
            "patch_id": "",
        }
    ],
    "patches": [],
    "test_cases": [],
    # 注意：v1.7 新增的 architecture / refactor_proposals 有意缺席（v0.3 形态）
    "stats": {
        "duration_sec": 1.25,
        "llm_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_hits": 0,
        "cache_misses": 0,
        "files_total": 2,
        "loc_total": 42,
        "suppressed": 0,
    },
    "created_at": "2026-09-01T00:00:00",
    "schema_version": "1.0",
}


def test_c1_legacy_report_renders_on_current_code() -> None:
    report = AuditReport.from_dict(_LEGACY_REPORT_V03)
    assert report.audit_id == "legacy-0001"
    assert len(report.issues) == 1
    assert report.issues[0].severity.value == "critical"
    # v1.7 新增字段回落默认：不炸、不丢旧字段
    assert report.architecture is None
    assert report.refactor_proposals == []

    md = render_markdown(report)
    html = render_html(report)
    sarif = render_sarif(report)
    assert "SQL 拼接注入" in md
    assert "legacy-0001" in html
    assert sarif["runs"][0]["results"], "旧报告的 SARIF 导出不能为空"
    # 再序列化 → 再反序列化（往返不漂移）
    again = AuditReport.from_dict(report.to_dict())
    assert again.to_dict() == report.to_dict()


# ---------------------------------------------------------------- C2 特性开关 A/B 等价
def _normalized(report: AuditReport) -> dict:
    """灰度对拍口径：剥离时延类字段后的语义快照。"""
    d = report.to_dict()
    d.pop("audit_id", None)
    d.pop("created_at", None)
    d["stats"].pop("duration_sec", None)
    return d


def test_c2_rule_scan_workers_ab_equivalent(tmp_path: Path) -> None:
    base = _audit(MINI_APP, tmp_path / "w_serial", rule_scan_workers=1)
    par = _audit(MINI_APP, tmp_path / "w_par", rule_scan_workers=4)
    assert _normalized(base) == _normalized(par), "规则扫描并行度不得改变审计语义"


def test_c2_ingest_hardlink_ab_equivalent(tmp_path: Path) -> None:
    src = tmp_path / "proj"
    src.mkdir()
    (src / "main.py").write_text("x = 1\n", encoding="utf-8")
    (src / "store.py").write_text(
        "def q(c, n):\n    return c.execute('SELECT * FROM t WHERE n=' + n)\n",
        encoding="utf-8",
    )
    c_copy = ingest(src, tmp_path, "ab_copy", link_same_volume=False)
    c_link = ingest(src, tmp_path, "ab_link", link_same_volume=True)
    key = lambda ms: sorted((m.path, m.sha256, m.loc) for m in ms)  # noqa: E731
    assert key(c_copy.manifests) == key(c_link.manifests), "硬链接物化与复制物化的清单必须一致"


# ---------------------------------------------------------------- C3 API 路由面冻结
def test_c3_api_route_surface_frozen() -> None:
    from server import app as server_app

    client = TestClient(server_app.app)
    spec = client.get("/openapi.json").json()
    actual = set(spec["paths"].keys())
    api_actual = {p for p in actual if p.startswith("/api/")}
    assert api_actual == FROZEN_API_ROUTES, (
        f"API 路由面漂移：新增={sorted(api_actual - FROZEN_API_ROUTES)} "
        f"缺失={sorted(FROZEN_API_ROUTES - api_actual)}（契约变更须显式更新本清单）"
    )
