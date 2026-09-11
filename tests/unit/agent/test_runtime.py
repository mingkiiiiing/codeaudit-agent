"""T3 单测：SimpleAgentRuntime（FakeLLMClient 脚本驱动）。"""

from __future__ import annotations

import asyncio
import json

from audit.agent.base import (
    STOP_BUDGET,
    STOP_COMPLETED,
    STOP_ERROR,
    STOP_MAX_ITERATIONS,
    AgentLimits,
    ToolSpec,
)
from audit.agent.runtime import SimpleAgentRuntime
from audit.errors import LLMError
from audit.llm.base import FakeLLMClient, LLMClient, LLMResponse


def _spec(name: str, handler, props: dict | None = None) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} 工具",
        parameters={"type": "object", "properties": props or {}},
        handler=handler,
    )


async def test_tool_call_roundtrip_and_completion():
    llm = FakeLLMClient(
        [
            {"tool_calls": [{"id": "c1", "name": "get_loc", "arguments": {"path": "a.py"}}]},
            {"content": "完成", "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        ]
    )
    rt = SimpleAgentRuntime(llm)
    seen: dict = {}

    async def handler(path: str) -> dict:
        seen["path"] = path
        return {"loc": 42}

    rt.register_tool(
        _spec("get_loc", handler, props={"path": {"type": "string"}})
    )
    result = await rt.run("你是审计员", [{"role": "user", "content": "go"}])

    assert result.stop_reason == STOP_COMPLETED
    assert result.text == "完成"
    assert result.iterations == 2
    assert result.tool_call_count == 1
    assert seen["path"] == "a.py"
    # token 累加：脚本项1默认 100/20，项2显式 10/5
    assert result.prompt_tokens == 110
    assert result.completion_tokens == 25
    # system prompt 位于 messages 首条
    second_call_messages = llm.calls[1]["messages"]
    assert second_call_messages[0] == {"role": "system", "content": "你是审计员"}
    # assistant tool_calls 与 tool 结果均回填
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "c1"
    assert json.loads(tool_msgs[0]["content"]) == {"loc": 42}
    assistant_msgs = [m for m in second_call_messages if m.get("role") == "assistant"]
    assert assistant_msgs[0]["tool_calls"][0]["function"]["name"] == "get_loc"
    assert llm.calls[0]["tools"][0]["function"]["name"] == "get_loc"


async def test_kwargs_filtered_by_schema_properties():
    llm = FakeLLMClient(
        [
            {"tool_calls": [{"name": "t", "arguments": {"path": "a.py", "evil": "注入键"}}]},
            {"content": "done"},
        ]
    )
    rt = SimpleAgentRuntime(llm)
    received: dict = {}

    async def handler(path: str = "") -> str:
        received.update(path=path)
        return "ok"

    rt.register_tool(_spec("t", handler, props={"path": {"type": "string"}}))
    result = await rt.run("s", [{"role": "user", "content": "x"}])
    assert result.stop_reason == STOP_COMPLETED
    assert received == {"path": "a.py"}  # schema 之外的键被过滤


async def test_handler_exception_backfills_error_and_continues():
    llm = FakeLLMClient(
        [
            {"tool_calls": [{"name": "boom", "arguments": {}}]},
            {"content": "已收到错误并收尾"},
        ]
    )
    rt = SimpleAgentRuntime(llm)

    async def boom() -> str:
        raise RuntimeError("炸了")

    rt.register_tool(_spec("boom", boom))
    result = await rt.run("s", [{"role": "user", "content": "x"}])
    assert result.stop_reason == STOP_COMPLETED  # 不崩，正常收尾
    second_call_messages = llm.calls[1]["messages"]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    payload = json.loads(tool_msgs[0]["content"])
    assert "error" in payload and "炸了" in payload["error"]


async def test_max_iterations_fuse():
    script = [
        {"tool_calls": [{"name": "noop", "arguments": {}}]},
        {"tool_calls": [{"name": "noop", "arguments": {}}]},
        {"tool_calls": [{"name": "noop", "arguments": {}}]},
        {"tool_calls": [{"name": "noop", "arguments": {}}]},
        {"tool_calls": [{"name": "noop", "arguments": {}}]},
    ]
    llm = FakeLLMClient(script)
    rt = SimpleAgentRuntime(llm)

    async def noop() -> str:
        return "{}"

    rt.register_tool(_spec("noop", noop))
    result = await rt.run("s", [{"role": "user", "content": "x"}], limits=AgentLimits(max_iterations=3))
    assert result.stop_reason == STOP_MAX_ITERATIONS
    assert result.iterations == 3
    assert len(llm.calls) == 3
    assert result.tool_call_count == 3


async def test_token_budget_fuse():
    usage = {"prompt_tokens": 100, "completion_tokens": 20}
    script = [{"tool_calls": [{"name": "noop", "arguments": {}}], "usage": dict(usage)} for _ in range(5)]
    llm = FakeLLMClient(script)
    rt = SimpleAgentRuntime(llm)

    async def noop() -> str:
        return "{}"

    rt.register_tool(_spec("noop", noop))
    result = await rt.run(
        "s", [{"role": "user", "content": "x"}], limits=AgentLimits(max_iterations=10, token_budget=150)
    )
    assert result.stop_reason == STOP_BUDGET
    # 每轮 120 tokens：第 2 轮后累计 240 > 150 → 熔断
    assert len(llm.calls) == 2
    assert result.prompt_tokens == 200
    assert result.completion_tokens == 40


async def test_tool_timeout_backfills_error():
    llm = FakeLLMClient(
        [
            {"tool_calls": [{"name": "slow", "arguments": {}}]},
            {"content": "done"},
        ]
    )
    rt = SimpleAgentRuntime(llm)

    async def slow() -> str:
        await asyncio.sleep(2)
        return "never"

    rt.register_tool(_spec("slow", slow))
    result = await rt.run(
        "s", [{"role": "user", "content": "x"}], limits=AgentLimits(tool_timeout_sec=0.05)
    )
    assert result.stop_reason == STOP_COMPLETED
    second_call_messages = llm.calls[1]["messages"]
    tool_msgs = [m for m in second_call_messages if m.get("role") == "tool"]
    assert "超时" in tool_msgs[0]["content"]


async def test_unknown_tool_reports_error():
    llm = FakeLLMClient(
        [
            {"tool_calls": [{"name": "nope", "arguments": {}}]},
            {"content": "ok"},
        ]
    )
    rt = SimpleAgentRuntime(llm)
    result = await rt.run("s", [{"role": "user", "content": "x"}])
    assert result.stop_reason == STOP_COMPLETED
    tool_msgs = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert "未知工具" in tool_msgs[0]["content"]


async def test_events_emitted_in_order():
    llm = FakeLLMClient(
        [
            {"tool_calls": [{"id": "c9", "name": "t", "arguments": {}}]},
            {"content": "done"},
        ]
    )
    rt = SimpleAgentRuntime(llm)
    events: list[dict] = []

    async def on_event(event: dict) -> None:
        events.append(event)

    async def t() -> str:
        return "r"

    rt.register_tool(_spec("t", t))
    await rt.run("s", [{"role": "user", "content": "x"}], on_event=on_event)
    assert events[0]["type"] == "iteration" and events[0]["index"] == 1
    assert {"type": "tool_call", "name": "t"} == {k: events[1][k] for k in ("type", "name")}
    assert events[2]["type"] == "tool_result" and events[2]["ok"] is True
    assert events[-1] == {"type": "iteration", "index": 2}


async def test_no_tools_registered_passes_none():
    llm = FakeLLMClient([{"content": "直接回答"}])
    rt = SimpleAgentRuntime(llm)
    result = await rt.run("s", [{"role": "user", "content": "x"}])
    assert llm.calls[0]["tools"] is None
    assert result.stop_reason == STOP_COMPLETED
    assert result.text == "直接回答"


async def test_llm_failure_stops_with_error():
    class BoomLLM(LLMClient):
        async def chat(self, messages, tools=None, json_mode=False, temperature=0.2) -> LLMResponse:
            raise LLMError("api down")

        def usage_totals(self) -> dict:
            return {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    rt = SimpleAgentRuntime(BoomLLM())
    result = await rt.run("s", [{"role": "user", "content": "x"}])
    assert result.stop_reason == STOP_ERROR
    assert "api down" in result.error
    assert result.iterations == 1
