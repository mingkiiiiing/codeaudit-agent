"""Agent 运行时契约（契约文件，勿改）：工具注册、运行限制、运行结果。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from audit.llm.base import Message

# 工具处理器：kwargs 与工具 schema 的 properties 对齐；返回 str 或 dict（自动序列化）
ToolHandler = Callable[..., Awaitable[Any]]

# 运行事件回调：async def on_event(event: dict) -> None
# 事件形态：{"type": "tool_call", "name": ..., "arguments": ...}
#          {"type": "tool_result", "name": ..., "ok": bool}
#          {"type": "iteration", "index": int}
EventCallback = Callable[[dict[str, Any]], Awaitable[None]]

STOP_COMPLETED = "completed"
STOP_MAX_ITERATIONS = "max_iterations"
STOP_BUDGET = "budget"
STOP_ERROR = "error"


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema（OpenAI function parameters）
    handler: ToolHandler

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class AgentLimits:
    max_iterations: int = 25
    tool_timeout_sec: float = 60.0
    token_budget: int = 200_000


@dataclass
class AgentResult:
    text: str = ""
    iterations: int = 0
    tool_call_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    stop_reason: str = STOP_COMPLETED
    error: str = ""
    trace: list[dict[str, Any]] = field(default_factory=list)


class AgentRuntime(ABC):
    """工具调用循环的抽象基类（T3 提供具体实现）。"""

    @abstractmethod
    def register_tool(self, spec: ToolSpec) -> None:
        ...

    @abstractmethod
    async def run(
        self,
        system_prompt: str,
        messages: list[Message],
        limits: AgentLimits | None = None,
        on_event: EventCallback | None = None,
    ) -> AgentResult:
        ...
