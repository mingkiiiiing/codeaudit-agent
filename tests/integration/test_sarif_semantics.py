"""联调场景⑧（R2 必测场景 8）：SARIF 语义校验——真实审计产物 → report.sarif。

CLI 全流程（demo_proj，--format sarif）落盘 report.sarif，与同次审计的
report.json 逐条对账：
- 结构：version 2.1.0、$schema、driver 名/版本；
- ruleId 均可解析（driver.rules[] 内含，含 codeaudit/<category> 兜底机制）；
- level 映射：critical/high→error、medium→warning、low→note；
- partialFingerprints 与 audit.utils.issue_fingerprint 逐条一致（基线同源指纹）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import cli
from audit.models import AuditReport
from audit.report.sarif import severity_to_level
from audit.utils import issue_fingerprint

_FINGERPRINT_KEY = "codeauditFingerprint/v1"


def test_sarif_semantics_against_real_audit_products(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out_dir = tmp_path / "out"
    rc = cli.main(
        [
            "run", "tests/samples/demo_proj",
            "--no-llm", "--format", "sarif",
            "--work-root", str(tmp_path / "work"),
            "--out", str(out_dir),
        ]
    )
    assert rc == 0
    sarif_path = out_dir / "report.sarif"
    assert sarif_path.is_file()
    capsys.readouterr()

    report = AuditReport.from_dict(
        json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    )
    assert report.issues  # demo_proj 有真实缺陷
    sarif = json.loads(sarif_path.read_text(encoding="utf-8"))

    # ---- 结构
    assert sarif["version"] == "2.1.0"
    assert "sarif-2.1.0" in sarif["$schema"]
    run = sarif["runs"][0]
    driver = run["tool"]["driver"]
    assert driver["name"] == "codeaudit-agent"
    assert driver["version"]  # 版本单源 audit.__version__
    rule_ids = {rule["id"] for rule in driver["rules"]}
    assert len(rule_ids) == len(driver["rules"])  # rules 按 id 去重

    results = run["results"]
    assert len(results) == len(report.issues)

    # ---- 逐条对账（以指纹为键配对，不依赖顺序）
    by_fingerprint = {issue_fingerprint(issue): issue for issue in report.issues}
    seen: set[str] = set()
    for result in results:
        assert result["ruleId"] in rule_ids, f"ruleId 不可解析：{result['ruleId']}"
        assert result["level"] in {"error", "warning", "note"}
        location = result["locations"][0]["physicalLocation"]
        assert location["artifactLocation"]["uriBaseId"] == "%SRCROOT%"
        assert location["region"]["startLine"] >= 1

        fingerprint = result["partialFingerprints"][_FINGERPRINT_KEY]
        issue = by_fingerprint.get(fingerprint)
        assert issue is not None, f"指纹与 report.json 对不上：{fingerprint}"
        seen.add(fingerprint)
        # level 映射与 severity 一致
        assert result["level"] == severity_to_level(issue.severity)
        # message 承载标题
        assert issue.title in result["message"]["text"]

    assert seen == set(by_fingerprint)  # 每条问题恰好一条 result

    # ---- demo_proj 的产物同时覆盖三种 level（critical/high、medium、low 齐备）
    levels = {result["level"] for result in results}
    assert levels == {"error", "warning", "note"}

    # ---- 规则证据与 ruleId 对应（rule: 前缀证据 → 同名 ruleId）
    evidence_rules = {
        item[len("rule:") :]
        for issue in report.issues
        for item in issue.evidence
        if item.startswith("rule:")
    }
    assert evidence_rules <= rule_ids


@pytest.mark.parametrize(
    ("severity", "expected"),
    [("critical", "error"), ("high", "error"), ("medium", "warning"), ("low", "note")],
)
def test_severity_to_level_mapping(severity: str, expected: str) -> None:
    """severity → SARIF level 契约映射表。"""
    assert severity_to_level(severity) == expected
