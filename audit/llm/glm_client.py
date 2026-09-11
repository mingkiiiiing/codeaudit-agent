"""GLM 客户端实现（T3）：OpenAI 兼容 /chat/completions 接入。

特性（对应 docs/02 §3.1 与任务书 A）：
- 并发限流：asyncio.Semaphore(config.concurrency)，只作用于真实 HTTP 调用（缓存命中不限流）；
- 指数退避重试：429 / 5xx / 网络错误（httpx.TransportError）最多重试 max_retries 次，
  每次失败延迟 retry_base_delay * 2^n（默认 0.5s、1s、2s），耗尽后抛 LLMError；
  其它 4xx 视为不可重试，立即抛 LLMError；
- 响应缓存：key = sha256(model + messages + tools + json_mode)，命中直接返回并计 cache_hit；
- token 计量：usage_totals() 累计 llm_calls / prompt_tokens / completion_tokens（附 cache_hits /
  cache_misses 供报告计算命中率），内部用 threading.Lock 保证线程安全；
- 缺 API Key 时抛 ConfigError。

测试注入：构造参数 http_client 可传入挂了 httpx.MockTransport 的客户端；retry_base_delay
可调小以加速重试路径；_sleep 属性可替换为假 sleep 记录退避序列（不碰真实时钟）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
from typing import Any, Awaitable, Callable

import httpx

from audit.config import AuditConfig
from audit.errors import ConfigError, LLMError
from audit.llm.base import LLMClient, LLMResponse, Message, ToolCall
from audit.utils import sha256_text

__all__ = ["GlmClient"]


class GlmClient(LLMClient):
    """GLM（OpenAI 兼容协议）异步客户端。"""

    def __init__(
        self,
        config: AuditConfig,
        http_client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        retry_base_delay: float = 0.5,
        cache_capacity: int = 4096,
    ) -> None:
        self._config = config
        self._max_retries = max(0, int(max_retries))
        self._retry_base_delay = max(0.0, float(retry_base_delay))
        self._cache_capacity = max(1, int(cache_capacity))
        self._owns_client = http_client is None
        self._http = http_client if http_client is not None else httpx.AsyncClient(timeout=config.request_timeout)
        self._semaphore = asyncio.Semaphore(max(1, int(config.concurrency)))
        # 可替换的 sleep（测试注入假时钟）
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        # 计数与缓存的简单锁（asyncio 单线程下本已安全，锁用于防御多线程复用）
        self._lock = threading.Lock()
        self._cache: dict[str, LLMResponse] = {}
        self._llm_calls = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._cache_hits = 0
        self._cache_misses = 0

    # ------------------------------------------------------------------ 对外 API

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """发起一次补全请求；命中缓存直接返回（计 cache_hit，不计费 token）。

        消融开关（契约 v1.3）：config.enable_llm_cache=False 时每次都走真实请求
        （不读不写缓存），cache_misses 照常逐次累计、cache_hits 恒不增长。
        """
        if not self._config.api_key:
            raise ConfigError("GLM API Key 未配置（GLM_API_KEY 或 AuditConfig.api_key）")
        use_cache = bool(getattr(self._config, "enable_llm_cache", True))
        key = self._cache_key(messages, tools, json_mode)
        cached: LLMResponse | None = None
        with self._lock:
            if use_cache:
                cached = self._cache.get(key)
            if cached is not None:
                self._cache_hits += 1
            else:
                self._cache_misses += 1
        if cached is not None:
            return dataclasses.replace(cached)
        resp = await self._request_with_retry(messages, tools, json_mode, temperature)
        if use_cache:
            with self._lock:
                self._cache[key] = resp
                if len(self._cache) > self._cache_capacity:  # 简单的先进先出淘汰
                    self._cache.pop(next(iter(self._cache)))
        return dataclasses.replace(resp)

    def usage_totals(self) -> dict[str, int]:
        """累计真实 LLM 调用次数与 token 用量；额外附 cache_hits/cache_misses。"""
        with self._lock:
            return {
                "llm_calls": self._llm_calls,
                "prompt_tokens": self._prompt_tokens,
                "completion_tokens": self._completion_tokens,
                "cache_hits": self._cache_hits,
                "cache_misses": self._cache_misses,
            }

    async def aclose(self) -> None:
        """关闭自建的 httpx 客户端（外部注入的由调用方负责关闭）。"""
        if self._owns_client:
            await self._http.aclose()

    # ------------------------------------------------------------------ 内部实现

    def _cache_key(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        json_mode: bool,
    ) -> str:
        """缓存键：sha256(model + messages + tools + json_mode)（JSON 序列化保证稳定）。"""
        payload = {
            "model": self._config.model,
            "messages": messages,
            "tools": tools,
            "json_mode": json_mode,
        }
        return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))

    async def _request_with_retry(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        json_mode: bool,
        temperature: float,
    ) -> LLMResponse:
        url = self._config.base_url.rstrip("/") + "/chat/completions"
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools  # OpenAI function calling 格式透传
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                async with self._semaphore:  # 限流只覆盖真实网络调用
                    resp = await self._http.post(
                        url, json=payload, headers=headers, timeout=self._config.request_timeout
                    )
            except httpx.TransportError as exc:  # 连接失败/超时等网络错误 → 可重试
                last_error = exc
            else:
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except ValueError as exc:
                        raise LLMError(f"GLM 响应不是合法 JSON: {exc}") from exc
                    parsed = self._parse_response(data)
                    with self._lock:
                        self._llm_calls += 1
                        self._prompt_tokens += int(parsed.usage.get("prompt_tokens", 0) or 0)
                        self._completion_tokens += int(parsed.usage.get("completion_tokens", 0) or 0)
                    return parsed
                detail = self._error_detail(resp)
                if resp.status_code == 429 or resp.status_code >= 500:  # 限频/服务端错误 → 可重试
                    last_error = LLMError(f"GLM HTTP {resp.status_code}: {detail}")
                else:  # 其它 4xx（鉴权/参数错误）不可重试
                    raise LLMError(f"GLM 请求被拒绝（HTTP {resp.status_code}，不可重试）: {detail}")
            if attempt < self._max_retries:
                await self._sleep(self._retry_base_delay * (2**attempt))
        raise LLMError(
            f"GLM 调用在 {self._max_retries + 1} 次尝试后仍失败: {last_error}"
        ) from last_error

    def _parse_response(self, data: dict[str, Any]) -> LLMResponse:
        choices = data.get("choices") or []
        message = (choices[0].get("message") if choices else None) or {}
        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments")
            if isinstance(raw_args, str) and raw_args.strip():
                try:
                    args: Any = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {}  # 参数解析失败交由 Agent 层自纠（工具会报缺参错误）
            else:
                args = raw_args or {}
            if not isinstance(args, dict):
                args = {}
            tool_calls.append(ToolCall(id=str(tc.get("id", "")), name=str(fn.get("name", "")), arguments=args))
        usage = data.get("usage") or {}
        return LLMResponse(
            content=message.get("content"),
            tool_calls=tool_calls,
            usage={
                "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
            },
            model=str(data.get("model", self._config.model)),
            raw=data,
        )

    @staticmethod
    def _error_detail(resp: httpx.Response) -> str:
        try:
            body = resp.json()
        except ValueError:
            return resp.text[:300]
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:300]
        return str(body)[:300]
