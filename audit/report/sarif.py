"""SARIF 2.1.0 渲染器（W4-A1）：把 AuditReport 转为 GitHub Security 可消费的 SARIF。

对标 semgrep ``--sarif``（docs/09 §1 第 1 行）：产物交由
``github/codeql-action/upload-sarif@v3`` 上传，在 GitHub Security tab 原生展示；
本模块只负责产出合法的 SARIF 2.1.0 JSON，不做任何上传动作。

结构要点（SARIF 2.1.0）::

    {"$schema": ...sarif-2.1.0.json, "version": "2.1.0",
     "runs": [{"tool": {"driver": {name, version, informationUri, rules[]}},
               "results": [{ruleId, level, message, locations, partialFingerprints}]}]}

- ``results[].level`` 取值 error / warning / note，由 severity 四级映射而来；
- ``partialFingerprints`` 供 GitHub 跨次运行去重（指纹算法与基线一致，见
  :func:`audit.utils.issue_fingerprint`）；
- ``uriBaseId="%SRCROOT%"`` + posix 相对路径，上传端自行拼接仓库根。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from audit.models import AuditReport, Issue

_SARIF_SCHEMA_URI = "https://json.schemastore.org/sarif-2.1.0.json"
_SARIF_VERSION = "2.1.0"
_TOOL_NAME = "codeaudit-agent"
_INFORMATION_URI = "https://github.com/mingkiiiiing/codeaudit-agent"
_FINGERPRINT_KEY = "codeauditFingerprint/v1"
_URI_BASE_ID = "%SRCROOT%"

# severity -> SARIF level 映射表（契约 v1.4）：critical/high 视为 CI 必须处理的 error。
_SEVERITY_TO_LEVEL: dict[str, str] = {
    "critical": "error",
    "high": "error",
    "medium": "warning",
    "low": "note",
}

_EVIDENCE_RULE_PREFIX = "rule:"


def severity_to_level(severity: Any) -> str:
    """审计 severity → SARIF level：critical/high→error，medium→warning，low→note。

    接受 ``Severity`` 枚举或其字符串值；未知/缺失级别按最轻的 ``note`` 处理，
    保证渲染结果始终是 SARIF 合法取值。
    """
    key = getattr(severity, "value", severity)
    return _SEVERITY_TO_LEVEL.get(str(key), "note")


def _enum_value(value: Any) -> str:
    """枚举或字符串统一取字符串值（Issue 反序列化前后形态一致）。"""
    return str(getattr(value, "value", value))


def _rule_id_from_evidence(issue: Issue) -> str | None:
    """从 source 证据中提取规则 id（detect 引擎写入形如 ``rule:PY-XXX`` 的证据行）。

    无规则证据（纯 LLM 问题）返回 None，由调用方回退到通用类别规则。
    """
    for item in issue.evidence or []:
        text = str(item)
        if text.startswith(_EVIDENCE_RULE_PREFIX):
            rule_id = text[len(_EVIDENCE_RULE_PREFIX) :].strip()
            if rule_id:
                return rule_id
    return None


def _make_rule(rule_id: str, description: str, level: str) -> dict[str, Any]:
    """构造 SARIF reportingDescriptor（rules[] 元素）。"""
    return {
        "id": rule_id,
        "shortDescription": {"text": description or rule_id},
        "defaultConfiguration": {"level": level},
    }


def _load_driver_rules(known_ids: dict[str, dict[str, Any]]) -> None:
    """把内置规则注册表的元数据填入 known_ids（id → rule 对象），就地修改。"""
    from audit.detect.registry import DEFAULT_REGISTRY  # 延迟导入：避免渲染路径强依赖 tree-sitter

    for rule in DEFAULT_REGISTRY.all_rules:
        if rule.id not in known_ids:
            known_ids[rule.id] = _make_rule(rule.id, rule.description, severity_to_level(rule.severity))


def _region(issue: Issue) -> dict[str, int]:
    """SARIF region：行号 1-based 且必须 >=1（未指认行号的问题按第 1 行上报）。"""
    start = int(issue.line_start) if issue.line_start else 1
    end = int(issue.line_end) if issue.line_end else start
    return {"startLine": max(start, 1), "endLine": max(end, 1)}


def _result(issue: Issue) -> dict[str, Any]:
    """单条 Issue → SARIF result（契约 v1.4：ruleId 优先取证据规则，回退通用类别规则）。"""
    from audit.utils import issue_fingerprint

    rule_id = _rule_id_from_evidence(issue) or f"codeaudit/{_enum_value(issue.category)}"
    return {
        "ruleId": rule_id,
        "level": severity_to_level(issue.severity),
        "message": {"text": f"{issue.title}：{issue.description}"},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {
                        # 显式替换反斜杠：Linux 上 Path 不把 \ 当分隔符，
                        # as_posix() 不会转换（CI 曾因此挂掉）
                        "uri": issue.file.replace("\\", "/") if issue.file else "-",
                        "uriBaseId": _URI_BASE_ID,
                    },
                    "region": _region(issue),
                }
            }
        ],
        "partialFingerprints": {_FINGERPRINT_KEY: issue_fingerprint(issue)},
    }


def render_sarif(report: AuditReport) -> dict[str, Any]:
    """渲染 AuditReport 为 SARIF 2.1.0 dict。

    - ``runs[0].tool.driver``：工具名 / 版本（延迟读取 ``audit.__version__``，版本单源）/
      仓库地址 / rules[]；
    - rules[]：内置注册表全部规则 + LLM 产出中无法对应注册规则的问题所合成的
      通用规则 ``codeaudit/<category>``（以及证据里出现但不在注册表的规则 id 的兜底条目），
      按 id 去重、保持首次出现顺序。
    """
    import audit  # 延迟导入：版本单源，随包安装后始终可用

    known_rules: dict[str, dict[str, Any]] = {}
    _load_driver_rules(known_rules)
    for issue in report.issues:
        rule_id = _rule_id_from_evidence(issue) or f"codeaudit/{_enum_value(issue.category)}"
        if rule_id not in known_rules:
            if rule_id.startswith("codeaudit/"):
                category = rule_id.removeprefix("codeaudit/")
                known_rules[rule_id] = _make_rule(
                    rule_id, f"LLM 审查发现的问题（类别：{category}）", "warning"
                )
            else:  # 证据引用了未知规则 id：补兜底条目，保证 ruleId 均可解析
                known_rules[rule_id] = _make_rule(rule_id, f"审计规则 {rule_id}", "warning")

    return {
        "$schema": _SARIF_SCHEMA_URI,
        "version": _SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": _TOOL_NAME,
                        "version": audit.__version__,
                        "informationUri": _INFORMATION_URI,
                        "rules": list(known_rules.values()),
                    }
                },
                "results": [_result(issue) for issue in report.issues],
            }
        ],
    }


def write_sarif(report: AuditReport, out_path: Path) -> Path:
    """把 SARIF 报告落盘（UTF-8、ensure_ascii=False、缩进 2），返回写入路径。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(render_sarif(report), fh, ensure_ascii=False, indent=2)
    return out_path
