"""算法不变量测试（W9-B1）：确定性 / 健康分单调性 / 干净语料误报 / SARIF 结构 / 分页过滤不变量。

性质（property/metamorphic）口径：
- T1 确定性：同一输入两次独立审计（不同 work_root），问题清单逐字段一致、健康分一致；
- T2 健康分单调性：任意问题集合追加 critical 问题，健康分不升；空问题满分；值域 [0,100]；
- T3 干净语料误报：stdlib 风格纯净代码零 critical / high 告警（medium/low 记录不判死）；
- T4 SARIF 结构：level 映射合法、行号 ≥1、uri 非空、条数与 issues 一一对应；
- T5 统计自洽：summary 计数之和 == len(issues)，health_score 与 summary 交叉可复算；
- T6 分页/过滤不变量（端到端）：severity 过滤 total 与手工计数一致；按 offset 游走分页
  窗口，拼回的清单与全量一致（无重无漏）；非法过滤值 400。

全程离线：本文件 autouse fixture 消毒 GLM_* 环境变量（与 tests/integration/conftest 同口径）。
"""

from __future__ import annotations

import io
import random
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from audit.config import AuditConfig
from audit.models import AuditReport, Issue, Severity, count_by_severity, health_score
from audit.orchestrator.pipeline import run_audit
from audit.report.render import render_html, render_markdown
from audit.report.sarif import render_sarif

ROOT = Path(__file__).resolve().parents[2]
MINI_APP = ROOT / "demo" / "mini_app"


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("GLM_API_KEY", "GLM_BASE_URL", "GLM_MODEL"):
        monkeypatch.delenv(key, raising=False)


async def _noop_emitter(event: dict) -> None:  # pragma: no cover —— 仅作占位
    pass


def audit_offline(source: Path, work: Path) -> AuditReport:
    """离线（纯规则）跑一次完整流水线，返回报告。"""
    import asyncio

    cfg = AuditConfig(
        source_path=str(source),
        work_root=str(work),
        out_dir=str(work / "reports"),
        enable_llm_review=False,
    )
    return asyncio.run(run_audit(cfg, _noop_emitter))


# ---------------------------------------------------------------- T1 确定性
def test_t1_determinism_same_input_same_issues(tmp_path: Path) -> None:
    r1 = audit_offline(MINI_APP, tmp_path / "w1")
    r2 = audit_offline(MINI_APP, tmp_path / "w2")
    issues1 = [i.to_dict() for i in r1.issues]
    issues2 = [i.to_dict() for i in r2.issues]
    assert issues1 == issues2, "两次审计的问题清单（含顺序）必须逐字段一致"
    assert r1.health_score == r2.health_score
    assert r1.summary == r2.summary
    assert r1.languages == r2.languages
    assert r1.loc == r2.loc


# ---------------------------------------------------------------- T2 健康分单调性
def _rand_issue(rng: random.Random) -> Issue:
    sev = rng.choice(list(Severity))
    return Issue(
        id=f"rand-{rng.randint(0, 10**9)}",
        severity=sev,
        title="synthetic",
        file="mod.py",
        line_start=rng.randint(1, 500),
    )


def test_t2_health_score_monotone_under_added_critical() -> None:
    rng = random.Random(7)
    for _ in range(50):
        loc = rng.randint(10, 100_000)
        issues = [_rand_issue(rng) for _ in range(rng.randint(0, 40))]
        base = health_score(issues, loc)
        assert 0.0 <= base <= 100.0
        worse = issues + [
            Issue(id="extra", severity=Severity.CRITICAL, title="critical", file="x.py")
        ]
        assert health_score(worse, loc) <= base, "追加 critical 问题后健康分不允许上升"


def test_t2_health_score_perfect_when_clean_and_bounded() -> None:
    assert health_score([], 1000) == 100.0
    # 密度爆炸时下界为 0（不允许负分）
    floods = [Issue(id=str(i), severity=Severity.CRITICAL) for i in range(1000)]
    assert health_score(floods, 10) == 0.0


# ---------------------------------------------------------------- T3 干净语料误报
_CLEAN_SOURCES = {
    "pure.py": (
        '"""纯函数工具集（干净语料：不触碰任何规则模式）。"""\n'
        "\n"
        "def clamp(value: float, low: float, high: float) -> float:\n"
        '    """把 value 限制在 [low, high] 区间。"""\n'
        "    if low > high:\n"
        "        low, high = high, low\n"
        "    return min(max(value, low), high)\n"
        "\n"
        "\n"
        "def moving_average(values: list[float], window: int) -> list[float]:\n"
        '    """滑动窗口平均（窗口无效时原样返回）。"""\n'
        "    if window <= 0 or window > len(values):\n"
        "        return list(values)\n"
        "    return [\n"
        "        sum(values[i : i + window]) / window\n"
        "        for i in range(len(values) - window + 1)\n"
        "    ]\n"
    ),
    "types.py": (
        "from dataclasses import dataclass\n"
        "\n"
        "\n"
        "@dataclass(frozen=True)\n"
        "class Point:\n"
        "    x: float\n"
        "    y: float\n"
        "\n"
        "    def norm(self) -> float:\n"
        "        return (self.x**2 + self.y**2) ** 0.5\n"
    ),
    "iter_util.py": (
        "def dedupe_keep_order(items: list[str]) -> list[str]:\n"
        '    """保序去重。"""\n'
        "    seen: set[str] = set()\n"
        "    out: list[str] = []\n"
        "    for item in items:\n"
        "        if item not in seen:\n"
        "            seen.add(item)\n"
        "            out.append(item)\n"
        "    return out\n"
    ),
}


def test_t3_clean_corpus_no_critical_high_fp(tmp_path: Path) -> None:
    proj = tmp_path / "clean_proj"
    proj.mkdir()
    for name, text in _CLEAN_SOURCES.items():
        (proj / name).write_text(text, encoding="utf-8")
    report = audit_offline(proj, tmp_path / "work_t3")
    critical_high = [
        i.to_dict() for i in report.issues if i.severity in (Severity.CRITICAL, Severity.HIGH)
    ]
    assert not critical_high, f"干净语料出现 critical/high 误报：{critical_high}"


# ---------------------------------------------------------------- T4 SARIF 结构
def test_t4_sarif_structure(tmp_path: Path) -> None:
    report = audit_offline(MINI_APP, tmp_path / "w_sarif")
    sarif = render_sarif(report)
    assert sarif["version"] == "2.1.0"
    runs = sarif["runs"]
    assert len(runs) == 1
    results = runs[0]["results"]
    assert len(results) == len(report.issues), "SARIF 条数应与 issues 一一对应"
    for res in results:
        assert res["level"] in ("note", "warning", "error")
        loc = res["locations"][0]["physicalLocation"]
        assert loc["artifactLocation"]["uri"], "uri 不能为空"
        assert loc["region"]["startLine"] >= 1
    # 渲染器同报告不炸（契约 v1.x 报告双格式输出仍是当前代码路径）
    assert render_markdown(report)
    assert render_html(report)


# ---------------------------------------------------------------- T5 统计自洽
def test_t5_summary_consistency(tmp_path: Path) -> None:
    report = audit_offline(MINI_APP, tmp_path / "w_sum")
    manual = count_by_severity(report.issues)
    assert report.summary == manual
    assert sum(report.summary.values()) == len(report.issues)
    # 健康分可由 summary + loc 复算（同一公式）
    rebuilt_issues = [
        Issue(id=str(i), severity=Severity(s)) for s, n in report.summary.items() for i in range(n)
    ]
    assert health_score(rebuilt_issues, report.loc) == report.health_score


# ---------------------------------------------------------------- T6 分页/过滤不变量（端到端）
def _zip_dir(source: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(source.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(source).as_posix())
    return buf.getvalue()


def test_t6_filter_and_pagination_invariants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server import app as server_app

    monkeypatch.chdir(tmp_path)  # server 侧 work_root=".codeaudit" 相对 CWD，钉在 tmp
    client = TestClient(server_app.app)
    resp = client.post(
        "/api/audits/upload",
        files={"file": ("mini.zip", _zip_dir(MINI_APP), "application/zip")},
    )
    assert resp.status_code == 200
    audit_id = resp.json()["audit_id"]

    import time

    deadline = time.monotonic() + 120
    status = ""
    while time.monotonic() < deadline:
        status = client.get(f"/api/audits/{audit_id}").json().get("status", "")
        if status in ("done", "failed"):
            break
        time.sleep(0.2)
    assert status == "done", f"mini_app 审计未完成：{status}"

    full = client.get(f"/api/audits/{audit_id}/issues", params={"limit": "1000"}).json()
    total = full["total"]
    assert total == len(full["issues"]) > 0

    # 过滤不变量：每个 severity 的 total 与全量手工计数一致
    for sev in ("critical", "high", "medium", "low"):
        got = client.get(f"/api/audits/{audit_id}/issues", params={"severity": sev, "limit": "1000"})
        assert got.status_code == 200
        expect = sum(1 for i in full["issues"] if i["severity"] == sev)
        assert got.json()["total"] == expect, f"severity={sev} 过滤计数失真"

    # 分页不变量：offset 游走窗口拼回全量（无重无漏，顺序保持）
    window = 3
    rebuilt: list[dict] = []
    for off in range(0, total, window):
        page = client.get(
            f"/api/audits/{audit_id}/issues",
            params={"limit": str(window), "offset": str(off)},
        ).json()
        assert page["total"] == total
        rebuilt.extend(page["issues"])
    assert [i["id"] for i in rebuilt] == [i["id"] for i in full["issues"]], "分页窗口拼回与全量不一致"

    # 大小写/空白被服务端归一化（strip().lower() 后匹配即合法，属有意宽容）；
    # 归一化后仍非法的值必须 400
    for bad in ("fatal", "hack", "critical;drop", "%20"):
        resp = client.get(f"/api/audits/{audit_id}/issues", params={"severity": bad})
        assert resp.status_code == 400, f"severity={bad!r} 应 400，得到 {resp.status_code}"
    upper = client.get(f"/api/audits/{audit_id}/issues", params={"severity": "CRITICAL"})
    assert upper.status_code == 200 and upper.json()["total"] == sum(
        1 for i in full["issues"] if i["severity"] == "critical"
    )

    client.delete(f"/api/audits/{audit_id}")
