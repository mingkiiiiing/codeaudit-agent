"""W25 卡 B 单测：RouterClient 模型路由 + fallback（P0-7，全 mock 零网络）。

形态与 test_glm_client.py 一致：httpx.MockTransport 注入共享 http_client，
handler 按 payload["model"] 区分主/备模型的应答。
"""

from __future__ import annotations

import json

import httpx
import pytest

from audit.config import AuditConfig
from audit.errors import LLMError
from audit.llm.router import RouterClient

_PRIMARY = "glm-test"
_FALLBACK = "glm-fb"

_OK_BODY = {
    "choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 3},
}


def _body(content: str, model: str) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3},
        "model": model,
    }


def _config(**kw) -> AuditConfig:
    defaults: dict = {
        "api_key": "test-key",
        "base_url": "https://api.example.com/v4",
        "model": _PRIMARY,
        "concurrency": 8,
        "request_timeout": 5.0,
    }
    defaults.update(kw)
    return AuditConfig(**defaults)


def _router(handler, config: AuditConfig | None = None, **kw) -> RouterClient:
    cfg = config or _config()
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kw.setdefault("retry_base_delay", 0.001)
    return RouterClient(cfg, http_client=http, **kw)


def _read_payload(request: httpx.Request) -> dict:
    return json.loads(request.content.decode("utf-8"))


async def test_primary_429_fallback_success():
    """①主模型 429 重试耗尽 → 备模型成功返回，响应透传给调用方。"""
    attempts: dict[str, int] = {"primary": 0, "fallback": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        model = _read_payload(request)["model"]
        attempts["primary" if model == _PRIMARY else "fallback"] += 1
        if model == _PRIMARY:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, json=_body("from-fallback", _FALLBACK))

    router = _router(handler, _config(fallback_model=_FALLBACK), max_retries=1)
    resp = await router.chat([{"role": "user", "content": "hi"}])
    assert resp.content == "from-fallback"
    assert resp.model == _FALLBACK
    assert attempts["primary"] == 2  # 1 次原始 + 1 次重试（max_retries=1）耗尽后才切备
    assert attempts["fallback"] == 1  # 备模型一次机会即成功
    assert router.fallback_used == 1


async def test_both_fail_raises_last_llm_error():
    """双败：上抛最后（备模型）的 LLMError，主模型异常作为 __cause__ 保留。"""
    attempts: dict[str, int] = {"primary": 0, "fallback": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        model = _read_payload(request)["model"]
        attempts["primary" if model == _PRIMARY else "fallback"] += 1
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    router = _router(handler, _config(fallback_model=_FALLBACK), max_retries=1)
    with pytest.raises(LLMError, match="2 次尝试后仍失败") as exc_info:
        await router.chat([{"role": "user", "content": "hi"}])
    assert attempts == {"primary": 2, "fallback": 2}  # 备模型也有同等重试预算
    assert router.fallback_used == 1  # 切换确曾发生（含备模型也失败的调用）
    assert isinstance(exc_info.value.__cause__, LLMError)  # 主模型失败上下文不丢
    assert "GLM HTTP 429" in str(exc_info.value.__cause__)


async def test_no_fallback_primary_only_zero_change():
    """③fallback_model 为空：直接透传主 client、备路径零触发，行为与裸 GlmClient 一致。"""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert _read_payload(request)["model"] == _PRIMARY
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    router = _router(handler, _config(), max_retries=1)
    assert not router.fallback_enabled
    with pytest.raises(LLMError, match="GLM 调用在 2 次尝试后仍失败: GLM HTTP 429"):
        await router.chat([{"role": "user", "content": "hi"}])
    assert attempts == 2  # 只有主模型的请求，没有第二次备模型尝试
    usage = router.usage_totals()
    # 统计口径与裸 GlmClient 完全一致（键集合与计数）
    assert set(usage) == {"llm_calls", "prompt_tokens", "completion_tokens", "cache_hits", "cache_misses"}
    assert usage["llm_calls"] == 0  # 失败调用不计真实调用，与 GlmClient 同口径


async def test_no_fallback_success_passthrough():
    """③补：fallback 关闭且主模型成功——响应原样透传，无 fallback 计数。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_body("hello", _PRIMARY))

    router = _router(handler, _config(fallback_model=""))
    resp = await router.chat([{"role": "user", "content": "hi"}])
    assert resp.content == "hello"
    assert resp.model == _PRIMARY
    assert router.fallback_used == 0
    assert router.usage_totals()["llm_calls"] == 1


async def test_fallback_same_as_model_treated_as_disabled():
    """fallback_model 等于主模型：视为关闭，不构造备客户端、不触发备路径。"""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    router = _router(handler, _config(fallback_model=_PRIMARY), max_retries=1)
    assert not router.fallback_enabled
    with pytest.raises(LLMError):
        await router.chat([{"role": "user", "content": "hi"}])
    assert attempts == 2  # 无备模型重试
    assert router.fallback_used == 0


async def test_fallback_used_counter():
    """④计数：主模型失败切备模型的次数；主模型成功的调用不计数。"""
    primary_attempts = 0
    fallback_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal primary_attempts, fallback_attempts
        model = _read_payload(request)["model"]
        if model == _PRIMARY:
            primary_attempts += 1
            if primary_attempts <= 2:  # 第一次调用（2 次尝试）失败
                return httpx.Response(429, json={"error": {"message": "rate limited"}})
            return httpx.Response(200, json=_body("from-primary", _PRIMARY))
        fallback_attempts += 1
        return httpx.Response(200, json=_body("from-fallback", _FALLBACK))

    router = _router(handler, _config(fallback_model=_FALLBACK), max_retries=1)
    r1 = await router.chat([{"role": "user", "content": "call-1"}])
    assert r1.content == "from-fallback"
    assert router.fallback_used == 1
    r2 = await router.chat([{"role": "user", "content": "call-2"}])  # 主模型恢复，直连成功
    assert r2.content == "from-primary"
    assert router.fallback_used == 1  # 未再切换
    assert fallback_attempts == 1  # 第二次调用未触备路径
    assert primary_attempts == 3


async def test_stats_merged_across_clients_and_cache_keys_distinct():
    """⑤统计透出：主/备逐键求和（llm_calls/tokens/cache_*），pipeline 口径不变形。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if _read_payload(request)["model"] == _PRIMARY:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, json=_body("from-fallback", _FALLBACK))

    router = _router(handler, _config(fallback_model=_FALLBACK), max_retries=1)
    messages = [{"role": "user", "content": "same"}]
    await router.chat(messages)
    await router.chat(messages)  # 第二次：主仍失败切备，备模型命中自身缓存
    usage = router.usage_totals()
    assert usage["llm_calls"] == 1  # 仅备模型首次真实调用计入
    assert usage["prompt_tokens"] == 11
    assert usage["completion_tokens"] == 3
    # cache_misses：主 2 次 + 备 1 次；cache_hits：备第二次命中（缓存实例隔离且键含 model）
    assert usage["cache_misses"] == 3
    assert usage["cache_hits"] == 1
    assert router.fallback_used == 2


async def test_tools_json_mode_temperature_passthrough_on_fallback():
    """tools/json_mode/temperature 透传到备模型的实际 HTTP payload。"""
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(_read_payload(request))
        if payloads[-1]["model"] == _PRIMARY:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, json=_body("ok", _FALLBACK))

    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
    router = _router(handler, _config(fallback_model=_FALLBACK), max_retries=1)
    await router.chat(
        [{"role": "user", "content": "x"}], tools=tools, json_mode=True, temperature=0.7
    )
    fb_payload = payloads[-1]
    assert fb_payload["model"] == _FALLBACK
    assert fb_payload["tools"] == tools
    assert fb_payload["response_format"] == {"type": "json_object"}
    assert fb_payload["temperature"] == 0.7


async def test_aclose_ownership():
    """aclose：自建连接池时关闭（主备共享同一条）；外部注入时不动调用方的连接。"""
    cfg = _config(fallback_model=_FALLBACK)
    owned = RouterClient(cfg)
    assert owned._primary._http is owned._fallback._http  # 主备共享一条连接池
    await owned.aclose()
    assert owned._http.is_closed

    injected = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=_body("hello", _PRIMARY))
    ))
    router = RouterClient(cfg, http_client=injected)
    await router.aclose()
    assert not injected.is_closed  # 注入的连接由调用方负责关闭


async def test_config_error_not_routed_to_fallback():
    """ConfigError（缺 API Key）不触发 fallback：配置错误换模型同样会失败。"""
    router = RouterClient(_config(api_key="", fallback_model=_FALLBACK))
    from audit.errors import ConfigError

    with pytest.raises(ConfigError):
        await router.chat([{"role": "user", "content": "hi"}])
    assert router.fallback_used == 0
