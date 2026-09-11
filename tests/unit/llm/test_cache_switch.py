"""Wave 3 消融开关接线测试：enable_llm_cache（契约 v1.3，docs/08 §3）。

False 时 GlmClient.chat() 每次都真实调用（不走缓存读写），cache_misses 照常
逐次累计、cache_hits 恒不增长；默认 True 行为与现状一致（第二次命中缓存）。
全部基于 httpx.MockTransport，零网络、零真实 LLM。
"""

from __future__ import annotations

import httpx

from audit.config import AuditConfig
from audit.llm import GlmClient

_OK_BODY = {
    "choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 3},
    "model": "glm-test",
}


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


def _client(handler, config: AuditConfig | None = None) -> GlmClient:
    cfg = config or _config()
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GlmClient(cfg, http_client=http)


async def test_cache_disabled_hits_transport_every_time():
    """False：同一请求两次都打到 transport（cache_misses=2、cache_hits=0）。"""
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(200, json=_OK_BODY)

    client = _client(handler, _config(enable_llm_cache=False))
    messages = [{"role": "user", "content": "same"}]
    r1 = await client.chat(messages)
    r2 = await client.chat(messages)
    assert count == 2  # 每次都真实调用
    assert r1.content == "hello" and r2.content == "hello"
    usage = client.usage_totals()
    assert usage["llm_calls"] == 2
    assert usage["cache_misses"] == 2  # miss 照常逐次累计
    assert usage["cache_hits"] == 0  # 永不命中
    assert usage["prompt_tokens"] == 22  # token 逐次计费


async def test_cache_disabled_does_not_populate_cache_store():
    """False：请求完成后不写缓存——恢复 True 语义可由新实例验证互不影响。"""
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(200, json=_OK_BODY)

    client = _client(handler, _config(enable_llm_cache=False))
    messages = [{"role": "user", "content": "same"}]
    await client.chat(messages)
    await client.chat(messages)
    assert client._cache == {}  # 缓存存储保持为空


async def test_cache_enabled_second_call_hits_cache():
    """默认 True：第二次同请求命中缓存（不发请求），计数与现状一致。"""
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(200, json=_OK_BODY)

    client = _client(handler)
    messages = [{"role": "user", "content": "same"}]
    await client.chat(messages)
    r2 = await client.chat(messages)
    assert count == 1  # 第二次命中缓存
    assert r2.content == "hello"
    usage = client.usage_totals()
    assert usage["llm_calls"] == 1
    assert usage["cache_hits"] == 1
    assert usage["cache_misses"] == 1
    assert usage["prompt_tokens"] == 11
