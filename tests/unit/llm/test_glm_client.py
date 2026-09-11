"""T3 单测：GlmClient（全 mock，httpx.MockTransport，无真实网络）。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from audit.config import AuditConfig
from audit.errors import ConfigError, LLMError
from audit.llm import GlmClient


def _config(**kw) -> AuditConfig:
    defaults: dict = {
        "api_key": "test-key",
        "base_url": "https://api.example.com/v4",
        "model": "glm-test",
        "concurrency": 8,
        "request_timeout": 5.0,
    }
    defaults.update(kw)
    return AuditConfig(**defaults)


_OK_BODY = {
    "choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 3},
    "model": "glm-test",
}


def _client(handler, config: AuditConfig | None = None, **kw) -> GlmClient:
    cfg = config or _config()
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GlmClient(cfg, http_client=http, **kw)


def _read_payload(request: httpx.Request) -> dict:
    return json.loads(request.content.decode("utf-8"))


async def test_chat_success_parses_response():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["payload"] = _read_payload(request)
        return httpx.Response(200, json=_OK_BODY)

    client = _client(handler)
    resp = await client.chat([{"role": "user", "content": "hi"}])
    assert resp.content == "hello"
    assert not resp.has_tool_calls
    assert resp.model == "glm-test"
    assert seen["url"].endswith("/v4/chat/completions")
    assert seen["auth"] == "Bearer test-key"
    assert seen["payload"]["model"] == "glm-test"
    usage = client.usage_totals()
    assert usage["llm_calls"] == 1
    assert usage["prompt_tokens"] == 11
    assert usage["completion_tokens"] == 3


async def test_tools_and_json_mode_passthrough():
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(_read_payload(request))
        return httpx.Response(200, json=_OK_BODY)

    client = _client(handler)
    await client.chat([{"role": "user", "content": "a"}], tools=tools, json_mode=True)
    assert payloads[0]["tools"] == tools
    assert payloads[0]["response_format"] == {"type": "json_object"}

    await client.chat([{"role": "user", "content": "b"}])
    assert "tools" not in payloads[1]
    assert "response_format" not in payloads[1]


async def test_tool_calls_arguments_parsed():
    body = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": "a.py", "start_line": 2}),
                            },
                        },
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {"name": "bad", "arguments": "{not json"},
                        },
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    resp = await _client(handler).chat([{"role": "user", "content": "x"}])
    assert resp.has_tool_calls
    assert resp.tool_calls[0].id == "call_1"
    assert resp.tool_calls[0].name == "read_file"
    assert resp.tool_calls[0].arguments == {"path": "a.py", "start_line": 2}
    # 非法 arguments JSON：容错为空 dict，交由 Agent 层自纠
    assert resp.tool_calls[1].arguments == {}


async def test_cache_hit_counts_and_skips_http():
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(200, json=_OK_BODY)

    client = _client(handler)
    messages = [{"role": "user", "content": "same"}]
    r1 = await client.chat(messages)
    r2 = await client.chat(messages)
    assert count == 1  # 第二次命中缓存，不再发请求
    assert r2.content == r1.content
    usage = client.usage_totals()
    assert usage["llm_calls"] == 1  # 缓存命中不计真实调用
    assert usage["cache_hits"] == 1
    assert usage["cache_misses"] == 1
    assert usage["prompt_tokens"] == 11  # token 只计真实调用


async def test_retry_on_500_then_success():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(500, json={"error": {"message": "boom"}})
        return httpx.Response(200, json=_OK_BODY)

    client = _client(handler, retry_base_delay=0.001)
    resp = await client.chat([{"role": "user", "content": "hi"}])
    assert resp.content == "hello"
    assert attempts == 2
    assert client.usage_totals()["llm_calls"] == 1


async def test_retry_backoff_sequence_and_exhaustion(monkeypatch):
    delays: list[float] = []
    attempts = 0

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    client = _client(handler)
    client._sleep = fake_sleep  # 注入假时钟，记录退避序列
    with pytest.raises(LLMError):
        await client.chat([{"role": "user", "content": "hi"}])
    assert attempts == 4  # 1 次原始调用 + 3 次重试
    assert delays == [0.5, 1.0, 2.0]  # 0.5 * 2^n


async def test_non_retryable_401_fails_fast():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    client = _client(handler)
    with pytest.raises(LLMError, match="401"):
        await client.chat([{"role": "user", "content": "hi"}])
    assert attempts == 1  # 不可重试状态码：一次即抛


async def test_network_error_retried_then_llm_error():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("connection refused")

    client = _client(handler, max_retries=1, retry_base_delay=0.001)
    with pytest.raises(LLMError, match="connection refused"):
        await client.chat([{"role": "user", "content": "hi"}])
    assert attempts == 2


async def test_missing_api_key_raises_config_error():
    client = GlmClient(_config(api_key=""))
    with pytest.raises(ConfigError):
        await client.chat([{"role": "user", "content": "hi"}])


async def test_semaphore_limits_concurrency():
    active = 0
    max_active = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return httpx.Response(200, json=_OK_BODY)

    client = _client(handler, _config(concurrency=2))
    await asyncio.gather(
        *[client.chat([{"role": "user", "content": f"m{i}"}]) for i in range(5)]
    )
    assert max_active <= 2
    assert client.usage_totals()["llm_calls"] == 5


async def test_usage_accumulates_across_calls():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_OK_BODY)

    client = _client(handler)
    await client.chat([{"role": "user", "content": "one"}])
    await client.chat([{"role": "user", "content": "two"}])
    usage = client.usage_totals()
    assert usage["llm_calls"] == 2
    assert usage["prompt_tokens"] == 22
    assert usage["completion_tokens"] == 6
