"""W22-A 工具循环消息预算测试：超预算折叠 tool 结果、幂等、默认关闭。

FakeLLMClient 脚本驱动多轮工具调用，工具 handler 返回超长 content，断言：
- 折叠后传给 llm.chat 的消息总字符有界；
- 消息条数与 tool_call/tool 配对完整（只缩短 content 不删消息）；
- 折叠标记幂等（已折叠消息不会被二次折叠）；
- message_budget_chars=0（默认）零行为变化。
"""

from __future__ import annotations

from audit.agent.base import STOP_COMPLETED, AgentLimits, ToolSpec
from audit.agent.runtime import (
    _FOLD_MARK,
    _convo_chars,
    _fold_oversized_tool_messages,
    SimpleAgentRuntime,
)
from audit.llm.base import FakeLLMClient


def _tool(spec_content: str) -> ToolSpec:
    async def handler() -> str:
        return spec_content

    return ToolSpec(name="read_big", description="返回大块内容", parameters={"type": "object", "properties": {}}, handler=handler)


def _loop_script(rounds: int) -> list[dict]:
    """rounds 轮工具调用后收尾 completed。"""
    script: list[dict] = [
        {"tool_calls": [{"id": f"c{i}", "name": "read_big", "arguments": {}}]}
        for i in range(rounds)
    ]
    script.append({"content": "done"})
    return script


def _convo_total_chars(messages: list[dict]) -> int:
    return sum(len(str(m.get("content") or "")) for m in messages)


async def test_budget_zero_keeps_old_behavior():
    """budget=0（默认）不折叠：超长工具结果原样保留在后续消息中。"""
    big = "x" * 5000
    llm = FakeLLMClient(_loop_script(2))
    rt = SimpleAgentRuntime(llm)
    rt.register_tool(_tool(big))
    result = await rt.run(
        "sys",
        [{"role": "user", "content": "go"}],
        limits=AgentLimits(message_budget_chars=0),
    )
    assert result.stop_reason == STOP_COMPLETED
    # 第二轮调用消息里包含完整工具结果（未折叠）
    assert _convo_total_chars(llm.calls[2]["messages"]) > 5000


async def test_oversized_tool_messages_folded_and_bounded():
    """超预算：最旧 tool 消息被折叠，消息总字符降到预算附近，配对结构完整。"""
    big = "y" * 4000
    llm = FakeLLMClient(_loop_script(3))
    rt = SimpleAgentRuntime(llm)
    rt.register_tool(_tool(big))
    budget = 6000
    result = await rt.run(
        "sys",
        [{"role": "user", "content": "go"}],
        limits=AgentLimits(message_budget_chars=budget),
    )
    assert result.stop_reason == STOP_COMPLETED
    assert result.tool_call_count == 3
    # 每一轮发给 llm.chat 的消息都满足预算（折叠发生在请求前）
    for call in llm.calls:
        assert _convo_total_chars(call["messages"]) <= budget + 4000  # 单条折叠余量
    # 配对完整性：assistant.tool_calls 的每个 id 都有对应 tool 消息
    final = llm.calls[-1]["messages"]
    tool_ids = [m["tool_call_id"] for m in final if m.get("role") == "tool"]
    call_ids = [tc["id"] for m in final if m.get("role") == "assistant" for tc in m.get("tool_calls", [])]
    assert sorted(tool_ids) == sorted(call_ids)
    # 至少一条 tool content 已折叠（带标记）
    assert any(str(m.get("content", "")).startswith("y" * 10) and _FOLD_MARK in str(m.get("content", "")) for m in final if m.get("role") == "tool")


async def test_fold_idempotent():
    """幂等：对已折叠的对话再跑折叠，不产生二次标记、字符数不增。"""
    convo: list[dict] = [
        {"role": "user", "content": "u" * 100},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "z" * 3000},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c2", "content": "z" * 3000},
    ]
    first = _fold_oversized_tool_messages(convo, 2000)
    assert first >= 1
    snapshot = [str(m.get("content")) for m in convo]
    again = _fold_oversized_tool_messages(convo, 2000)
    assert again == 0  # 已折叠，二次调用零动作
    assert [str(m.get("content")) for m in convo] == snapshot


async def test_short_tool_results_never_folded():
    """短于阈值的 tool 结果不折叠（折叠收益小于标记开销）。"""
    convo: list[dict] = [
        {"role": "user", "content": "u" * 100},
        {"role": "tool", "tool_call_id": "c1", "content": "short"},
    ]
    assert _fold_oversized_tool_messages(convo, 10) == 0
    assert convo[1]["content"] == "short"


async def test_user_and_system_messages_not_folded():
    """折叠只作用于 tool 消息：user/system 超预算也不动（保守停止条件）。"""
    convo: list[dict] = [
        {"role": "system", "content": "s" * 3000},
        {"role": "user", "content": "u" * 3000},
    ]
    assert _fold_oversized_tool_messages(convo, 100) == 0
    assert convo[0]["content"] == "s" * 3000
    assert convo[1]["content"] == "u" * 3000


async def test_convo_chars_counts_all_roles():
    convo = [
        {"role": "system", "content": "12345"},
        {"role": "user", "content": ""},
        {"role": "tool", "tool_call_id": "c", "content": "abcdef"},
    ]
    assert _convo_chars(convo) == 11
