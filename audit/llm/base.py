"""LLM 基础契约（契约文件，勿改）：客户端协议、响应结构、可脚本回放的 FakeLLM。"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable

Message = dict[str, Any]  # OpenAI 风格消息：{"role": ..., "content": ..., ...}


@dataclass
class ToolCall:
    id: str = ""
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)  # prompt_tokens/completion_tokens
    model: str = ""
    raw: Any = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


def _normalize_script_item(item: "LLMResponse | dict[str, Any]", idx: int) -> LLMResponse:
    """允许用简写 dict 描述脚本项：{"content": ...} 或 {"tool_calls": [{"name":..., "arguments": {...}}]}。"""
    if isinstance(item, LLMResponse):
        return item
    data = dict(item)
    raw_calls = data.pop("tool_calls", [])
    calls: list[ToolCall] = []
    for i, c in enumerate(raw_calls):
        args = c.get("arguments", {})
        if isinstance(args, str):
            args = json.loads(args)
        calls.append(
            ToolCall(id=c.get("id", f"call_{idx}_{i}"), name=c["name"], arguments=args)
        )
    return LLMResponse(
        content=data.get("content"),
        tool_calls=calls,
        usage=data.get("usage", {"prompt_tokens": 100, "completion_tokens": 20}),
        model=data.get("model", "fake"),
    )


class LLMClient(ABC):
    """所有 LLM 后端的统一接口（OpenAI 兼容语义）。"""

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
        temperature: float = 0.2,
    ) -> LLMResponse:
        ...

    def usage_totals(self) -> dict[str, int]:
        return {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    async def aclose(self) -> None:  # noqa: B027 —— 契约 v1.5：默认空实现是有意设计
        """释放底层资源（HTTP 连接池等）；契约 v1.5 微增：默认空实现。

        持有真实连接的客户端（如 GlmClient）覆写本方法；编排层在审计结束
        （含异常）的 finally 中统一调用，无资源的实现（FakeLLM）无需关心。
        """


class FakeLLMClient(LLMClient):
    """脚本化假客户端：按顺序回放脚本项，用于全部离线测试。

    脚本耗尽后返回空 content 的响应（安全默认，避免测试死循环——
    Agent 循环会因"无工具调用且无内容"而正常收尾）。
    所有调用被记录到 self.calls 供断言。
    """

    def __init__(self, script: Iterable["LLMResponse | dict[str, Any]"] | None = None):
        self._script: list[LLMResponse] = [
            _normalize_script_item(item, i) for i, item in enumerate(script or [])
        ]
        self._cursor = 0
        self.calls: list[dict[str, Any]] = []
        self.prompt_tokens = 0
        self.completion_tokens = 0

    @property
    def exhausted(self) -> bool:
        return self._cursor >= len(self._script)

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
        temperature: float = 0.2,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "json_mode": json_mode,
                "temperature": temperature,
            }
        )
        if self._cursor >= len(self._script):
            return LLMResponse(content="")
        resp = self._script[self._cursor]
        self._cursor += 1
        self.prompt_tokens += resp.usage.get("prompt_tokens", 0)
        self.completion_tokens += resp.usage.get("completion_tokens", 0)
        return resp

    def usage_totals(self) -> dict[str, int]:
        return {
            "llm_calls": len(self.calls),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }
