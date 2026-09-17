"""SimpleAgentRuntime（T3）：最小可用的 ReAct 工具调用循环。

循环语义（对应任务书 B 与 docs/02 §3）：
1. system_prompt 进 messages 首条，追加调用方传入的 messages；
2. 调 llm.chat(messages, tools=注册工具的 to_openai_tool())；
3. 响应含 tool_calls：逐个执行（asyncio.wait_for 施加 limits.tool_timeout_sec 超时；
   handler 异常/超时以 {"error": ...} 文本回填不中断循环；返回 dict/list 自动
   json.dumps），结果以 {"role": "tool", "tool_call_id": ..., "content": ...} 回填后
   继续下一轮；
4. 无 tool_calls：结束，stop_reason="completed"；
5. 熔断：进入新一轮前，累计 tokens（每次 LLMResponse.usage 累加）超 token_budget →
   stop_reason="budget"；已完成迭代数达 max_iterations → stop_reason="max_iterations"；
6. llm.chat 抛异常 → stop_reason="error" 并记录 error 文本。

handler 的 kwargs 按工具 schema 的 properties 过滤后传入（多余键被丢弃）。
on_event 回调依次发射 iteration / tool_call / tool_result 事件（兼容同步回调）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, Iterable

from audit.agent.base import (
    STOP_BUDGET,
    STOP_COMPLETED,
    STOP_ERROR,
    STOP_MAX_ITERATIONS,
    AgentLimits,
    AgentResult,
    AgentRuntime,
    EventCallback,
    ToolSpec,
)
from audit.llm.base import LLMClient, LLMResponse, Message
from audit.utils import truncate

__all__ = ["SimpleAgentRuntime"]


# W22-A：折叠标记前缀（幂等——已折叠的消息不会被二次折叠）
_FOLD_MARK = "[folded:"
# W22-A：短于该长度的 tool 结果不值得折叠（折叠收益小于标记本身开销）
_FOLD_MIN_CHARS = 200
# W22-A：折叠时保留的原文前缀长度
_FOLD_KEEP_CHARS = 160


def _convo_chars(convo: list[Message]) -> int:
    """消息总字符数（content 口径；system/user/assistant/tool 一并计入）。"""
    return sum(len(str(m.get("content") or "")) for m in convo)


def _fold_oversized_tool_messages(convo: list[Message], budget: int) -> int:
    """W22-A 消息预算折叠：超预算时从最旧的 tool 消息折叠 content，返回折叠条数。

    - 只折叠 role=="tool" 的 content（保留前 _FOLD_KEEP_CHARS 字符 + 折叠标记），
      不删除消息——OpenAI 协议要求 tool 消息与 assistant.tool_calls 逐个配对，
      删除会破坏对话结构；assistant 消息的 tool_calls 字段永不触碰；
    - 折叠标记前缀幂等：已折叠过的消息跳过；
    - 保守停止条件：折叠完全部可折叠 tool 消息仍超预算时直接停止（不动其他
      角色——宁可超预算也不破坏用户/系统消息语义）。
    """
    if budget <= 0 or _convo_chars(convo) <= budget:
        return 0
    folded = 0
    for msg in convo:
        if _convo_chars(convo) <= budget:
            break
        if msg.get("role") != "tool":
            continue
        content = str(msg.get("content") or "")
        if len(content) < _FOLD_MIN_CHARS or content.startswith(_FOLD_MARK):
            continue
        keep = content[:_FOLD_KEEP_CHARS]
        msg["content"] = f"{keep}\n{_FOLD_MARK}{len(content) - len(keep)} chars folded to fit message budget]"
        folded += 1
    return folded


class SimpleAgentRuntime(AgentRuntime):
    """基于任意 LLMClient 的工具调用循环实现。"""

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm
        self._tools: dict[str, ToolSpec] = {}

    # ------------------------------------------------------------------ 工具注册

    def register_tool(self, spec: ToolSpec) -> None:
        """注册（或覆盖）一个工具。"""
        self._tools[spec.name] = spec

    def register_tools(self, specs: Iterable[ToolSpec]) -> None:
        for spec in specs:
            self.register_tool(spec)

    def get_tool(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools)

    # ------------------------------------------------------------------ 主循环

    async def run(
        self,
        system_prompt: str,
        messages: list[Message],
        limits: AgentLimits | None = None,
        on_event: EventCallback | None = None,
    ) -> AgentResult:
        limits = limits or AgentLimits()
        result = AgentResult()
        convo: list[Message] = []
        if system_prompt:
            convo.append({"role": "system", "content": system_prompt})
        convo.extend(list(messages))
        tools = [spec.to_openai_tool() for spec in self._tools.values()] or None
        total_tokens = 0

        while True:
            if total_tokens > limits.token_budget:
                result.stop_reason = STOP_BUDGET
                break
            if result.iterations >= limits.max_iterations:
                result.stop_reason = STOP_MAX_ITERATIONS
                break
            result.iterations += 1
            # W22-A：每轮请求前做消息预算折叠（0=不限，默认零行为变化）
            _fold_oversized_tool_messages(convo, limits.message_budget_chars)
            await self._emit(on_event, {"type": "iteration", "index": result.iterations})

            try:
                resp = await self._llm.chat(convo, tools=tools)
            except Exception as exc:  # LLM 最终失败（如 LLMError）
                result.stop_reason = STOP_ERROR
                result.error = f"{type(exc).__name__}: {exc}"
                break

            usage = resp.usage or {}
            result.prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
            result.completion_tokens += int(usage.get("completion_tokens", 0) or 0)
            total_tokens = result.prompt_tokens + result.completion_tokens

            if not resp.has_tool_calls:
                result.text = resp.content or ""
                result.stop_reason = STOP_COMPLETED
                break

            convo.append(self._assistant_message(resp))
            for tc in resp.tool_calls:
                result.tool_call_count += 1
                await self._emit(
                    on_event,
                    {"type": "tool_call", "name": tc.name, "arguments": tc.arguments, "tool_call_id": tc.id},
                )
                content, ok = await self._execute(tc, limits.tool_timeout_sec)
                result.trace.append({"type": "tool_call", "name": tc.name, "arguments": tc.arguments})
                result.trace.append({"type": "tool_result", "name": tc.name, "ok": ok, "content": truncate(content, 500)})
                await self._emit(on_event, {"type": "tool_result", "name": tc.name, "ok": ok})
                convo.append({"role": "tool", "tool_call_id": tc.id, "content": content})
        return result

    # ------------------------------------------------------------------ 内部工具

    async def _execute(self, tc: Any, timeout_sec: float) -> tuple[str, bool]:
        """执行单个工具调用，返回 (回填文本, 是否正常执行)。

        ok=False 仅表示执行层失败（未知工具/异常/超时）；工具自身以 {"error": ...}
        表达的业务失败仍算 ok=True（那是工具的正常失败语义）。
        """
        spec = self._tools.get(tc.name)
        if spec is None:
            return json.dumps({"error": f"未知工具: {tc.name}"}, ensure_ascii=False), False
        props = (spec.parameters or {}).get("properties") or {}
        kwargs = {k: v for k, v in (tc.arguments or {}).items() if k in props}
        try:
            out = await asyncio.wait_for(spec.handler(**kwargs), timeout=timeout_sec)
        except (asyncio.TimeoutError, TimeoutError):
            return (
                json.dumps({"error": f"工具 {tc.name} 执行超时（>{timeout_sec}s）"}, ensure_ascii=False),
                False,
            )
        except Exception as exc:  # handler 契约是不抛异常，这里兜底保证循环不崩
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), False
        if isinstance(out, (dict, list)):
            return json.dumps(out, ensure_ascii=False, default=str), True
        return ("" if out is None else str(out)), True

    @staticmethod
    def _assistant_message(resp: LLMResponse) -> Message:
        msg: Message = {"role": "assistant", "content": resp.content or ""}
        if resp.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)},
                }
                for tc in resp.tool_calls
            ]
        return msg

    @staticmethod
    async def _emit(cb: EventCallback | None, event: dict[str, Any]) -> None:
        if cb is None:
            return
        out = cb(event)
        if inspect.isawaitable(out):
            await out
