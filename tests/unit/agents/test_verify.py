"""T3 单测：Verify Agent（FakeLLMClient 脚本驱动，覆盖 verdict 三分支与降级）。"""

from __future__ import annotations

import json

from audit.agents import verify_issue
from audit.llm.base import FakeLLMClient
from audit.models import Category, Issue, IssueSource, Severity

FILE = "app/services/users.py"


def _issue() -> Issue:
    return Issue(
        id="ISS-0001",
        category=Category.BUG,
        severity=Severity.HIGH,
        title="get_user 返回值未判空",
        file=FILE,
        line_start=10,
        line_end=11,
        description="uid 不存在时返回 None",
        evidence=["orders.py:88 调用 get_user"],
        suggestion="增加判空",
        confidence=0.8,
        source=IssueSource.LLM,
    )


async def test_verify_confirmed(sample_workspace):
    llm = FakeLLMClient(
        [
            {
                "content": json.dumps(
                    {
                        "verdict": "confirmed",
                        "reason": "第 10 行确实未判空且无调用方防护",
                        "confidence": 0.95,
                        "severity": "critical",
                    }
                )
            }
        ]
    )
    out = await verify_issue(sample_workspace, None, llm, _issue())
    assert llm.calls[0]["json_mode"] is True
    assert out.severity == Severity.CRITICAL  # 应用响应的 severity
    assert out.confidence == 0.95
    assert any("[verify:confirmed]" in e and "未判空" in e for e in out.evidence)
    assert out.description == "uid 不存在时返回 None"  # 原描述不动
    # prompt 里带 issue JSON 与报告行源码上下文
    system_text = llm.calls[0]["messages"][0]["content"]
    assert "ISS-0001" in system_text
    assert "复核清单" in system_text
    user_text = llm.calls[0]["messages"][1]["content"]
    assert "10: " in user_text  # 带行号上下文


async def test_verify_confirmed_keeps_severity_when_invalid(sample_workspace):
    llm = FakeLLMClient(
        [{"content": json.dumps({"verdict": "confirmed", "reason": "属实", "confidence": 0.9, "severity": "极高"})}]
    )
    out = await verify_issue(sample_workspace, None, llm, _issue())
    assert out.severity == Severity.HIGH  # 非法 severity 保持原值
    assert out.confidence == 0.9


async def test_verify_false_positive(sample_workspace):
    llm = FakeLLMClient(
        [
            {
                "content": json.dumps(
                    {
                        "verdict": "false_positive",
                        "reason": "所有调用方均已判空，触发条件不可达",
                        "confidence": 0.99,
                        "severity": "low",  # 响应 severity 应被忽略（保持原值）
                    }
                )
            }
        ]
    )
    out = await verify_issue(sample_workspace, None, llm, _issue())
    assert out.confidence == 0.0  # 驳回 → confidence 置 0
    assert out.severity == Severity.HIGH  # severity 保持原值供误报率统计
    assert "[Verify 驳回]" in out.description
    assert "所有调用方均已判空" in out.description
    assert out.title == "get_user 返回值未判空"  # 其余字段不动
    assert not any("[verify:confirmed]" in e for e in out.evidence)


async def test_verify_uncertain_lowers_confidence(sample_workspace):
    llm = FakeLLMClient(
        [
            {
                "content": json.dumps(
                    {"verdict": "uncertain", "reason": "缺少调用方信息", "confidence": 0.3, "severity": "high"}
                )
            }
        ]
    )
    out = await verify_issue(sample_workspace, None, llm, _issue())
    assert out.confidence == 0.3  # min(0.8, 0.3)
    assert "[Verify:uncertain]" in out.description
    assert "缺少调用方信息" in out.description


async def test_verify_uncertain_without_confidence_caps_at_half(sample_workspace):
    llm = FakeLLMClient([{"content": json.dumps({"verdict": "uncertain", "reason": "信息不足"})}])
    out = await verify_issue(sample_workspace, None, llm, _issue())
    assert out.confidence == 0.5  # min(0.8, 0.5)


async def test_verify_parse_failure_degrades_to_uncertain(sample_workspace):
    llm = FakeLLMClient([{"content": "抱歉，我无法确认。这句话不是 JSON。"}])
    out = await verify_issue(sample_workspace, None, llm, _issue())
    assert out.confidence == 0.5  # min(0.8, 0.5)
    assert "[Verify 降级]" in out.description
    assert "解析失败" in out.description
    assert out.severity == Severity.HIGH  # 不动


async def test_verify_does_not_mutate_input(sample_workspace):
    llm = FakeLLMClient(
        [{"content": json.dumps({"verdict": "false_positive", "reason": "误报", "confidence": 0.9})}]
    )
    original = _issue()
    out = await verify_issue(sample_workspace, None, llm, original)
    assert out is not original
    assert original.confidence == 0.8  # 入参未被原地修改
    assert out.confidence == 0.0
