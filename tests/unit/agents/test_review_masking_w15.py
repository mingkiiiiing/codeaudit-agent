"""W15-A1（docs/20 §4.2）：LLM 审查路径密钥脱敏回归。

W14-A1 只覆盖纯规则路径（Rule.mask_snippet），审计发现 A1：LLM 路径的 evidence
原样透传、snippet 直取不打码——密钥明文可经 LLM 审查结论进入报告。本文件用
FakeLLM 脚本化 payload（含密钥行）验证：
- code_snippet 套用规则路径同一打码语义（mask_string_literals 逐行）；
- evidence 逐条过 mask_secret_text（与 W14 mask 语义对齐，占位 ********）；
- 无字面量形态的普通 evidence 原样保留（打码不误伤可读性）。

sample_workspace 是 demo_proj 的 tmp 副本，此处直接改写 orders.py 为已知密钥行，
payload 行号指向密钥行（WorkspaceContext 每次读盘、无缓存）。
"""

from __future__ import annotations

import asyncio
import json

from audit.agents.review import issues_from_payloads, review_file
from audit.llm.base import FakeLLMClient

FILE = "app/services/orders.py"
SECRET_KEY = "sk-abcdef1234567890abcdef1234567890"
SECRET_PWD = "mysupersecretkey123456"

# 已知内容的文件：第 3/4 行为密钥行（payload 将指向第 3 行）
SECRET_FILE_LINES = [
    "import json",
    "",
    f'API_KEY = "{SECRET_KEY}"',
    f'DB_PASSWORD = "{SECRET_PWD}"',
    "def get_user(uid):",
    "    return None",
]


def _write_secret_file(workspace) -> None:
    (workspace.src_root / FILE).write_text("\n".join(SECRET_FILE_LINES), encoding="utf-8")


def _payload(**overrides) -> dict:
    payload = {
        "category": "bug",
        "severity": "high",
        "title": "模块级硬编码凭据",
        "file": FILE,
        "line_start": 3,
        "line_end": 3,
        "description": "API_KEY 疑似硬编码凭据，建议改用环境变量注入",
        "evidence": [
            f'第 3 行：API_KEY = "{SECRET_KEY}"',
            "orders.py:3 调用方未校验长度",  # 无字面量的普通证据，应原样保留
        ],
        "suggestion": "改用环境变量注入凭据",
        "confidence": 0.9,
    }
    payload.update(overrides)
    return payload


def test_payload_snippet_and_evidence_masked(sample_workspace):
    """映射层：snippet 套用规则路径打码、evidence 逐条过 mask_secret_text。"""
    _write_secret_file(sample_workspace)
    issues = issues_from_payloads([_payload()], sample_workspace)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.code_snippet == 'API_KEY = "********"'
    # 报告 Issue 全量字段无明文密钥
    for secret in (SECRET_KEY, SECRET_PWD):
        assert secret not in issue.code_snippet
        assert secret not in issue.evidence[0]
        assert secret not in issue.description
    # evidence 第一条被打码（占位 8 个 *），第二条普通文本原样保留
    assert "********" in issue.evidence[0]
    assert issue.evidence[1] == "orders.py:3 调用方未校验长度"


def test_review_json_path_masks_secrets(sample_workspace):
    """端到端：FakeLLM json 模式返回含密钥的 payload → review_file 产出已脱敏 Issue。"""
    _write_secret_file(sample_workspace)
    llm = FakeLLMClient([{"content": json.dumps({"issues": [_payload()]})}])
    issues = asyncio.run(review_file(sample_workspace, None, llm, FILE, hints=[]))
    assert len(issues) == 1
    issue = issues[0]
    assert issue.code_snippet == 'API_KEY = "********"'
    assert SECRET_KEY not in issue.evidence[0]
    assert "********" in issue.evidence[0]
    blob = json.dumps(
        {
            "snippet": issue.code_snippet,
            "evidence": issue.evidence,
            "description": issue.description,
        },
        ensure_ascii=False,
    )
    assert SECRET_KEY not in blob and SECRET_PWD not in blob


def test_multiline_snippet_masked_line_by_line(sample_workspace):
    """行区间覆盖密钥行与普通行：逐行打码、普通行不受扰（与规则路径一致）。"""
    _write_secret_file(sample_workspace)
    payload = _payload(line_start=3, line_end=4, evidence=[])
    issues = issues_from_payloads([payload], sample_workspace)
    assert len(issues) == 1
    assert issues[0].code_snippet == (
        'API_KEY = "********"\nDB_PASSWORD = "********"'
    )
