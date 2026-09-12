"""W4-A1 SARIF 渲染器自测：severity→level 映射、render_sarif 结构、write_sarif 落盘。

构造含 2 条不同 severity Issue 的 AuditReport（1 条带规则证据、1 条纯 LLM），
断言 SARIF 2.1.0 顶层结构、driver/rules/results 字段、partialFingerprints 与
issue_fingerprint 一致、uri 为 posix 相对路径。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import audit
from audit.models import AuditReport, Category, Issue, IssueSource, Severity
from audit.report.sarif import render_sarif, severity_to_level, write_sarif
from audit.utils import issue_fingerprint


def make_issue(severity: Severity, **kw: Any) -> Issue:
    """构造测试 Issue（字段与 detect 引擎真实产物形态一致）。"""
    base: dict[str, Any] = dict(
        id="ISS-0001",
        category=Category.SECURITY,
        severity=severity,
        title="SQL 拼接注入风险",
        file="app/services/orders.py",
        line_start=10,
        line_end=12,
        code_snippet="cursor.execute(f'select * from t where id={uid}')",
        description="用户输入未参数化直接拼入 SQL",
        evidence=["rule:PY-BARE-EXCEPT", "loc:app/services/orders.py:10-12"],
        suggestion="改用参数化查询",
        confidence=0.9,
        source=IssueSource.RULE_LLM,
    )
    base.update(kw)
    return Issue(**base)


def make_report(issues: list[Issue]) -> AuditReport:
    return AuditReport(
        audit_id="sariftst1",
        project_name="demo_proj",
        health_score=80.0,
        summary={"critical": 0, "high": 1, "medium": 0, "low": 1},
        issues=issues,
    )


# ---------------------------------------------------------------- severity_to_level


@pytest.mark.parametrize(
    ("severity", "expected"),
    [
        (Severity.CRITICAL, "error"),
        (Severity.HIGH, "error"),
        (Severity.MEDIUM, "warning"),
        (Severity.LOW, "note"),
        ("critical", "error"),
        ("high", "error"),
        ("medium", "warning"),
        ("low", "note"),
        ("unknown", "note"),  # 未知级别兜底为最轻的 note，保证 SARIF 合法
    ],
)
def test_severity_to_level_mapping(severity: Any, expected: str) -> None:
    """四级映射表驱动：critical/high→error，medium→warning，low→note。"""
    assert severity_to_level(severity) == expected


# ---------------------------------------------------------------- render_sarif 结构


@pytest.fixture
def rendered() -> dict[str, Any]:
    """两条 Issue：high + 规则证据；low + 纯 LLM（无证据，回退通用类别规则）。"""
    high = make_issue(Severity.HIGH)
    llm_low = make_issue(
        Severity.LOW,
        id="ISS-0002",
        category=Category.BUG,
        title="循环内重复查询",
        description="每次迭代都查询数据库",
        evidence=[],
        source=IssueSource.LLM,
    )
    return render_sarif(make_report([high, llm_low]))


def test_sarif_top_level_structure(rendered: dict[str, Any]) -> None:
    """顶层键：$schema 指向 2.1.0 schema、version=2.1.0、单 run。"""
    assert rendered["$schema"] == "https://json.schemastore.org/sarif-2.1.0.json"
    assert rendered["version"] == "2.1.0"
    assert len(rendered["runs"]) == 1


def test_sarif_driver_metadata(rendered: dict[str, Any]) -> None:
    """driver：工具名 / 版本单源（audit.__version__）/ 仓库 informationUri。"""
    driver = rendered["runs"][0]["tool"]["driver"]
    assert driver["name"] == "codeaudit-agent"
    assert driver["version"] == audit.__version__ == "0.3.0"
    assert driver["informationUri"] == "https://github.com/mingkiiiiing/codeaudit-agent"


def test_sarif_rules_from_registry_and_generic(rendered: dict[str, Any]) -> None:
    """rules[]：注册表规则齐全（含证据引用的 PY-BARE-EXCEPT），并为纯 LLM 问题合成通用规则。"""
    driver = rendered["runs"][0]["tool"]["driver"]
    rules = {r["id"]: r for r in driver["rules"]}
    # 内置注册表规则全部进入 rules[]
    assert "PY-BARE-EXCEPT" in rules
    assert len(driver["rules"]) >= 49
    for rule in driver["rules"]:
        assert rule["shortDescription"]["text"]
        assert rule["defaultConfiguration"]["level"] in {"error", "warning", "note"}
    # 通用类别规则（LLM 问题无法对应注册规则时合成）
    generic = rules["codeaudit/bug"]
    assert "LLM" in generic["shortDescription"]["text"]
    # 注册表规则的默认级别与 severity 映射一致
    from audit.detect.registry import DEFAULT_REGISTRY

    registry = {r.id: r for r in DEFAULT_REGISTRY.all_rules}
    assert rules["PY-BARE-EXCEPT"]["defaultConfiguration"]["level"] == severity_to_level(
        registry["PY-BARE-EXCEPT"].severity
    )


def test_sarif_results_fields(rendered: dict[str, Any]) -> None:
    """results[]：ruleId / level / message / location / partialFingerprints 逐字段断言。"""
    results = rendered["runs"][0]["results"]
    assert len(results) == 2
    by_rule = {r["ruleId"]: r for r in results}

    rule_hit = by_rule["PY-BARE-EXCEPT"]  # 证据里 rule:PY-BARE-EXCEPT 优先命中
    assert rule_hit["level"] == "error"  # high → error
    assert rule_hit["message"]["text"] == "SQL 拼接注入风险：用户输入未参数化直接拼入 SQL"
    loc = rule_hit["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"] == "app/services/orders.py"  # posix 相对路径
    assert loc["artifactLocation"]["uriBaseId"] == "%SRCROOT%"
    assert loc["region"] == {"startLine": 10, "endLine": 12}

    llm_hit = by_rule["codeaudit/bug"]  # 无规则证据 → 通用类别规则
    assert llm_hit["level"] == "note"  # low → note
    assert llm_hit["message"]["text"] == "循环内重复查询：每次迭代都查询数据库"


def test_sarif_partial_fingerprints_match_contract(rendered: dict[str, Any]) -> None:
    """partialFingerprints 与契约 issue_fingerprint 完全一致（GitHub 去重与基线同源）。"""
    report = make_report([make_issue(Severity.HIGH), make_issue(Severity.LOW, id="ISS-0002")])
    sarif = render_sarif(report)
    results = sarif["runs"][0]["results"]
    for issue, result in zip(report.issues, results, strict=True):
        assert result["partialFingerprints"]["codeauditFingerprint/v1"] == issue_fingerprint(issue)


def test_sarif_uri_backslash_normalized_to_posix() -> None:
    """Windows 反斜杠路径归一化为 posix 相对路径（SARIF uri 规范要求 / 分隔）。"""
    issue = make_issue(Severity.MEDIUM, file="app\\models\\user.py")
    sarif = render_sarif(make_report([issue]))
    uri = sarif["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
    assert uri == "app/models/user.py"
    assert "\\" not in uri


def test_sarif_zero_issue_report_has_empty_results() -> None:
    """零问题报告：results 为空数组、driver/rules 结构仍然完整（合法 SARIF）。"""
    sarif = render_sarif(AuditReport(audit_id="clean0001", project_name="clean"))
    assert sarif["runs"][0]["results"] == []
    assert sarif["runs"][0]["tool"]["driver"]["name"] == "codeaudit-agent"


# ---------------------------------------------------------------- write_sarif 落盘


def test_write_sarif_writes_loadable_json(tmp_path: Path) -> None:
    """write_sarif 落盘后可 json.load，schema/version 键正确，返回写入路径。"""
    out = tmp_path / "nested" / "report.sarif"
    report = make_report([make_issue(Severity.HIGH)])
    written = write_sarif(report, out)
    assert written == out and out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["$schema"] == "https://json.schemastore.org/sarif-2.1.0.json"
    assert data["version"] == "2.1.0"
    assert data["runs"][0]["tool"]["driver"]["name"] == "codeaudit-agent"
    assert len(data["runs"][0]["results"]) == 1
