"""Stage3 架构理解测试：启发式底座 + FakeLLM 增强/降级。"""

from __future__ import annotations

import json
import logging

import pytest

from audit.llm.base import FakeLLMClient
from audit.understand import build_architecture, find_entry_points


class TestHeuristicBase:
    async def test_no_llm_fallback(self, pipeline_ctx):
        # FakeLLMClient() 空脚本：耗尽后返回空 content，必须安全降级而非抛异常
        card = await build_architecture(pipeline_ctx)
        assert pipeline_ctx.architecture is card
        assert "python" in card.tech_stack
        assert card.text  # 人读摘要非空
        assert card.hotspots
        assert any("orders" in h for h in card.hotspots)
        # 入口识别：main.py（断言在入口清单与摘要文本两处）
        assert pipeline_ctx.extra["entry_points"] == ["main.py"]
        assert "main.py" in card.text
        # 模块职责表
        assert "app" in card.modules
        assert "个文件" in card.modules["app"]
        # FakeLLM 被调用且用 json_mode
        assert pipeline_ctx.llm.calls
        assert pipeline_ctx.llm.calls[0]["json_mode"] is True

    async def test_llm_disabled_runs_pure_heuristic(self, pipeline_ctx):
        pipeline_ctx.config.enable_llm_review = False
        card = await build_architecture(pipeline_ctx)
        assert "python" in card.tech_stack
        assert pipeline_ctx.llm.calls == []  # 未请求 LLM

    def test_find_entry_points_direct(self, pipeline_ctx):
        entries = find_entry_points(pipeline_ctx)
        assert entries == ["main.py"]

    async def test_modules_symbol_summary(self, pipeline_ctx):
        card = await build_architecture(pipeline_ctx)
        # 一级/二级目录各自成行；app/utils 应列出源码中的函数符号（无索引时的正则回退）
        assert "app" in card.modules and "app/utils" in card.modules
        assert "accumulate" in card.modules["app/utils"]
        assert "fetch_json" in card.modules["app/utils"]
        assert "get_order_summary" in card.modules["app/services"]


class TestLLMEnhancement:
    async def test_llm_result_merged(self, pipeline_ctx):
        payload = {
            "tech_stack": ["python", "flask", "sqlite"],
            "modules": {"app": "订单/用户业务模块"},
            "hotspots": ["app/services/orders.py"],
            "summary": "这是一个 Flask + SQLite 的订单管理系统，app 包承载全部业务。",
        }
        pipeline_ctx.llm = FakeLLMClient([{"content": json.dumps(payload, ensure_ascii=False)}])
        card = await build_architecture(pipeline_ctx)
        assert "flask" in card.tech_stack and "sqlite" in card.tech_stack
        assert "python" in card.tech_stack  # 启发式结果保留
        assert card.modules["app"] == "订单/用户业务模块"  # LLM 覆盖
        assert card.hotspots == ["app/services/orders.py"]
        assert card.text.startswith("这是一个 Flask")

    async def test_garbage_llm_output_degrades(self, pipeline_ctx):
        pipeline_ctx.llm = FakeLLMClient([{"content": "抱歉，我无法输出 JSON……"}])
        card = await build_architecture(pipeline_ctx)
        assert "python" in card.tech_stack
        assert card.text  # 启发式摘要兜底

    async def test_partial_llm_payload(self, pipeline_ctx):
        payload = {"summary": "只有摘要。"}
        pipeline_ctx.llm = FakeLLMClient([{"content": json.dumps(payload, ensure_ascii=False)}])
        card = await build_architecture(pipeline_ctx)
        assert card.text == "只有摘要。"
        assert "python" in card.tech_stack  # 其余字段回落启发式

    async def test_empty_response_logs_debug_not_warning(self, pipeline_ctx, caplog):
        """F5-R3：离线纯规则模式（FakeLLM 空脚本耗尽）空响应是常态而非故障——
        不打「增强失败」warning（此前误导用户以为出错），降级 debug 记账。"""
        with caplog.at_level(logging.DEBUG, logger="audit.understand.architecture"):
            card = await build_architecture(pipeline_ctx)
        assert "python" in card.tech_stack  # 启发式底座照常产出
        assert pipeline_ctx.llm.calls  # 请求已发出，空脚本耗尽返回空 content
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == []  # 不再误报「LLM 增强失败」
        assert any(
            r.levelno == logging.DEBUG and "空响应" in r.getMessage()
            for r in caplog.records
        )

    async def test_non_empty_garbage_still_warns(self, pipeline_ctx, caplog):
        """非空内容的解析失败是真实增强故障——W15 的 warning 可见性语义保持。"""
        pipeline_ctx.llm = FakeLLMClient([{"content": "抱歉，我无法输出 JSON……"}])
        with caplog.at_level(logging.DEBUG, logger="audit.understand.architecture"):
            card = await build_architecture(pipeline_ctx)
        assert "python" in card.tech_stack  # 降级为启发式底座
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("LLM 增强失败" in r.getMessage() for r in warnings)
