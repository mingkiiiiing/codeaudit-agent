"""T3 单测：Review Agent（FakeLLMClient 脚本驱动）。"""

from __future__ import annotations

import json

from audit.agent.base import ToolSpec
from audit.agent.runtime import SimpleAgentRuntime
from audit.agents import REVIEW_TOOL_NAMES, build_review_prompt, review_file
from audit.agents.review import issues_from_payloads
from audit.llm.base import FakeLLMClient
from audit.models import Issue, IssueSource

FILE = "app/services/orders.py"


def _valid_issue(**overrides) -> dict:
    payload = {
        "category": "bug",
        "severity": "high",
        "title": "get_user 返回值未判空即访问属性",
        "file": FILE,
        "line_start": 1,
        "line_end": 2,
        "description": "uid 不存在时 get_user 返回 None，后续访问属性会崩",
        "evidence": ["orders.py:1 调用 get_user"],
        "suggestion": "增加判空分支",
        "confidence": 0.9,
    }
    payload.update(overrides)
    return payload


async def test_review_json_mode_path(sample_workspace):
    llm = FakeLLMClient([{"content": json.dumps({"issues": [_valid_issue()]})}])
    issues = await review_file(
        sample_workspace, None, llm, FILE, hints=["R-001 bug users.py:9 可空返回未判空"]
    )
    assert llm.calls[0]["json_mode"] is True
    assert len(issues) == 1
    issue = issues[0]
    assert issue.source == IssueSource.LLM
    assert issue.id == ""  # id 由集成层分配
    assert issue.file == FILE
    assert issue.line_start == 1 and issue.line_end == 2
    assert issue.code_snippet  # 附带代码片段
    assert issue.confidence == 0.9
    # prompt 组包：hint 注入 + 带行号源码 + 架构摘要
    system_text = llm.calls[0]["messages"][0]["content"]
    assert "R-001" in system_text
    assert "1: " in system_text
    assert "<architecture_card>" in system_text
    assert "[rule_hints]" in system_text


async def test_review_json_mode_empty_issues(sample_workspace):
    llm = FakeLLMClient([{"content": json.dumps({"issues": []})}])
    issues = await review_file(sample_workspace, None, llm, FILE, hints=[])
    assert issues == []


async def test_review_json_mode_drops_out_of_range_lines(sample_workspace):
    """简化路径的行号终检：越界/幻觉条目丢弃，合法条目保留。"""
    total = sample_workspace.line_count(FILE)
    llm = FakeLLMClient(
        [
            {
                "content": json.dumps(
                    {
                        "issues": [
                            _valid_issue(),
                            _valid_issue(title="越界", line_start=total + 1, line_end=total + 1),
                            _valid_issue(title="零行", line_start=0, line_end=0),
                            _valid_issue(title="幻觉文件", file="no/such.py"),
                        ]
                    }
                )
            }
        ]
    )
    issues = await review_file(sample_workspace, None, llm, FILE, hints=[])
    assert len(issues) == 1
    assert issues[0].line_start == 1
    assert issues[0].line_end == min(2, total)


async def test_review_json_mode_unparsable_returns_empty(sample_workspace):
    llm = FakeLLMClient([{"content": "完全不是 JSON 的回答"}])
    issues = await review_file(sample_workspace, None, llm, FILE, hints=None)
    assert issues == []


async def test_review_runtime_path_tool_rejects_invalid_batch(sample_workspace):
    """工具层行号硬校验：批量里任一条非法 → 整批拒绝（不落任何问题）。"""
    llm = FakeLLMClient(
        [
            {
                "tool_calls": [
                    {
                        "name": "record_issues",
                        "arguments": {
                            "issues": [
                                _valid_issue(),
                                _valid_issue(title="越界行", line_start=99999, line_end=99999),
                                _valid_issue(title="零行号", line_start=0, line_end=0),
                                _valid_issue(title="幻觉文件", file="ghost/ghost.py"),
                            ]
                        },
                    }
                ]
            },
            {"content": "审查完成"},
        ]
    )
    runtime = SimpleAgentRuntime(llm)
    issues = await review_file(sample_workspace, None, llm, FILE, hints=[], runtime=runtime)
    assert issues == []  # 整批被工具层拒绝
    # 审查工具集恰好是 docs/03 §3.1 列出的 7 个
    tool_names = {t["function"]["name"] for t in llm.calls[0]["tools"]}
    assert tool_names == set(REVIEW_TOOL_NAMES)
    # record_issues 的错误以 role=tool 消息回填，Agent 可读到失败原因
    tool_msgs = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert "校验失败" in tool_msgs[0]["content"]


async def test_review_runtime_path_valid_batch_drops_out_of_range(sample_workspace):
    """工具层放行的合法批次进入收集；Review 层再做行号终检（双保险）。"""
    llm = FakeLLMClient(
        [
            {
                "tool_calls": [
                    {
                        "name": "record_issues",
                        "arguments": {
                            "issues": [
                                _valid_issue(),
                                _valid_issue(title="另一条"),
                            ]
                        },
                    }
                ]
            },
            {"content": "done"},
        ]
    )
    runtime = SimpleAgentRuntime(llm)
    issues = await review_file(sample_workspace, None, llm, FILE, hints=[], runtime=runtime)
    assert len(issues) == 2
    assert all(i.source == IssueSource.LLM for i in issues)
    assert all(i.id == "" for i in issues)  # id 留给集成层


async def test_review_runtime_path_records_issues_via_handler(sample_workspace):
    """工具层校验通过时，载荷经包装处理器进入收集列表并转为 Issue。"""
    llm = FakeLLMClient(
        [
            {"tool_calls": [{"name": "record_issues", "arguments": {"issues": [_valid_issue()]}}]},
            {"content": "done"},
        ]
    )
    runtime = SimpleAgentRuntime(llm)
    issues = await review_file(sample_workspace, None, llm, FILE, hints=[], runtime=runtime)
    assert len(issues) == 1
    assert issues[0].source == IssueSource.LLM


def test_issues_from_payloads_clamps_line_end(sample_workspace):
    total = sample_workspace.line_count(FILE)
    payloads = [
        _valid_issue(line_end=10 ** 6),  # line_end 越界 → 收敛到文件末行
        _valid_issue(line_start=total, line_end=total),
    ]
    issues = issues_from_payloads(payloads, sample_workspace)
    assert len(issues) == 2
    assert issues[0].line_end == total
    assert issues[1].line_start == total


def test_issues_from_payloads_invalid_category_falls_back(sample_workspace):
    issues = issues_from_payloads([_valid_issue(category="logic")], sample_workspace)
    assert len(issues) == 1
    assert issues[0].category.value == "bug"  # 非法枚举回退 bug


def test_build_review_prompt_contains_sections():
    prompt = build_review_prompt(
        architecture_summary="FastAPI 单体",
        file_path="a.py",
        loc=42,
        symbols_text="- function f (1-2)",
        source_text="1: x = 1",
        hints_text="无",
    )
    for marker in ("<architecture_card>", "[file_symbols]", "[file_source]", "[rule_hints]", "record_issues"):
        assert marker in prompt
    assert "a.py（42 行）" in prompt


async def test_review_respects_pre_registered_tools(sample_workspace):
    """runtime 已注册的工具被尊重；record_issues 被包装以收集结果。"""
    llm = FakeLLMClient(
        [
            {"tool_calls": [{"name": "record_issues", "arguments": {"issues": [_valid_issue()]}}]},
            {"content": "done"},
        ]
    )
    runtime = SimpleAgentRuntime(llm)
    marker_calls: list = []

    async def custom_read_file(path: str, start_line: int = 0, end_line: int = 0):
        marker_calls.append(path)
        return {"content": "custom"}

    runtime.register_tool(
        ToolSpec(
            name="read_file",
            description="custom",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            handler=custom_read_file,
        )
    )
    await review_file(sample_workspace, None, llm, FILE, hints=[], runtime=runtime)
    assert runtime.has_tool("read_file")
    assert runtime.get_tool("read_file").description == "custom"  # 未被覆盖
    assert runtime.has_tool("record_issues")
