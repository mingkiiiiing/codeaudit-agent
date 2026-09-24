"""模型路由 + fallback 客户端（W25 卡 B，审计 P0-7：LLM 单点无 fallback）。

背景：第五轮审计确认 GLM 主模型 HTTP 429（套餐限流）时，GlmClient 内部指数退避
重试耗尽后抛 LLMError，全链路降级为规则通道，无备用模型兜底。RouterClient 在
编排层与 GlmClient 之间加一层薄路由：主模型失败（其自身重试已耗尽）后，用备用
模型再试一次；备用模型也失败则上抛最后的 LLMError（chain 原始异常保留完整上下文）。

设计口径：
- 触发条件：仅捕获 LLMError（主模型重试耗尽的终态）。ConfigError（如缺 API Key）
  等配置类错误原样上抛，不触发 fallback——配置错误换模型同样会失败；
- fallback 预算：每次 chat 至多一次备模型尝试（备模型构造时共享同一组 max_retries /
  retry_base_delay / cache_capacity，即备模型自身也有同等重试预算），不做多级 fallback；
- 缓存正确性：主/备各持独立 GlmClient 实例（不换 model 复用同一实例），GlmClient
  缓存键含 model（glm_client._cache_key），实例隔离 + 键含模型，双保险保证命中
  语义正确；失败响应不入缓存（GlmClient 仅缓存成功响应），主模型失败不会污染缓存；
- 统计口径：usage_totals() 在启用 fallback 时对主/备两个内部客户端的统计逐键求和
  （llm_calls/prompt_tokens/completion_tokens/cache_hits/cache_misses），保证 pipeline
  报告统计不变形（经备模型完成的调用其 token 必须可见）；关闭 fallback 时直接
  委托主客户端（与裸 GlmClient 逐字节同口径）；fallback_used 单独计数（线程安全，
  threading.Lock 与 GlmClient._lock 同款防御式用法），语义 =「主模型失败后切换
  备模型的次数」，含备模型也失败的调用——切换确曾发生；
- 参数透传：tools / json_mode / temperature 原样透给当前尝试的客户端；aclose()
  释放内部共享 httpx 连接池（自建时）并透传内部客户端的 aclose（注入连接时为
  no-op，所有权归调用方）。

MVP 边界（有意不做，避免过度设计）：
- 无健康探测 / 熔断记忆：每次调用都先试主模型，主模型限流恢复后自动回到主路径；
- 无粘性路由 / 权重 / 多备模型链：仅一主一备；
- 备模型与主模型共用同一 base_url / api_key（同一 OpenAI 兼容服务内换模型），
  跨服务商路由不在本卡范围；
- fallback 触发的事件标注已接编排层（W26 卡 B：run_audit finally 收口前按
  fallback_used>0 发 warning 事件）；细粒度观测入口仍为 RouterClient.fallback_used
  计数，调用方可自行消费。

测试注入：与 GlmClient 同风格——http_client 传挂了 httpx.MockTransport 的客户端
（主/备共享同一连接，handler 按 payload["model"] 区分路由）；retry_base_delay 调小
加速重试路径。
"""

from __future__ import annotations

import dataclasses
import threading
from typing import Any

import httpx

from audit.config import AuditConfig
from audit.errors import LLMError
from audit.llm.base import LLMClient, LLMResponse, Message
from audit.llm.glm_client import GlmClient

__all__ = ["RouterClient"]


class RouterClient(LLMClient):
    """一主一备的 GLM 异步路由客户端（OpenAI 兼容语义，契约不变）。"""

    def __init__(
        self,
        config: AuditConfig,
        http_client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        retry_base_delay: float = 0.5,
        cache_capacity: int = 4096,
    ) -> None:
        primary_model = config.model
        fallback_model = str(getattr(config, "fallback_model", "") or "").strip()
        # 空 / 与主模型相同 → 关闭 fallback：行为与直接使用 GlmClient 完全一致
        self._fallback_enabled = bool(fallback_model) and fallback_model != primary_model
        # 主/备共享一条连接池：无注入时由 RouterClient 自建并持有所有权
        self._owns_http = http_client is None
        self._http = http_client if http_client is not None else httpx.AsyncClient(
            timeout=config.request_timeout
        )
        common = {
            "http_client": self._http,
            "max_retries": max_retries,
            "retry_base_delay": retry_base_delay,
            "cache_capacity": cache_capacity,
        }
        self._primary = GlmClient(config, **common)
        self._fallback: GlmClient | None = None
        if self._fallback_enabled:
            # 独立实例（非换 model 复用）：缓存与统计天然隔离，键再含 model 双保险
            self._fallback = GlmClient(dataclasses.replace(config, model=fallback_model), **common)
        self._lock = threading.Lock()
        self._fallback_used = 0

    # ------------------------------------------------------------------ 对外 API

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """先走主模型；LLMError（重试耗尽）且备模型可用时用备模型再试一次。"""
        if not self._fallback_enabled or self._fallback is None:
            # 关闭 fallback：零额外逻辑，直接透传（与裸 GlmClient 行为一致）
            return await self._primary.chat(
                messages, tools=tools, json_mode=json_mode, temperature=temperature
            )
        try:
            return await self._primary.chat(
                messages, tools=tools, json_mode=json_mode, temperature=temperature
            )
        except LLMError as primary_exc:
            with self._lock:
                self._fallback_used += 1  # 切换即计数（含备模型也失败的调用）
            try:
                return await self._fallback.chat(
                    messages, tools=tools, json_mode=json_mode, temperature=temperature
                )
            except LLMError as fallback_exc:
                # 双败：上抛最后（备模型）异常，chain 原始异常保留主模型失败上下文
                raise fallback_exc from primary_exc

    def usage_totals(self) -> dict[str, int]:
        """透出内部客户端统计：关闭 fallback 直接委托主客户端；开启时逐键求和。"""
        totals = self._primary.usage_totals()
        if self._fallback is None:
            return totals
        for key, value in self._fallback.usage_totals().items():
            totals[key] = totals.get(key, 0) + value
        return totals

    @property
    def fallback_used(self) -> int:
        """主模型失败后切换备模型的累计次数（线程安全）。"""
        with self._lock:
            return self._fallback_used

    @property
    def fallback_enabled(self) -> bool:
        """是否配置了生效的备用模型（非空且不等于主模型）。"""
        return self._fallback_enabled

    async def aclose(self) -> None:
        """释放资源：透传内部客户端 aclose（注入连接时为 no-op），自建连接池在此关闭。"""
        for client in (self._primary, self._fallback):
            if client is not None:
                await client.aclose()
        if self._owns_http:
            await self._http.aclose()
