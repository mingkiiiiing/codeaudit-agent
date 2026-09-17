"""W14-A2（M-1）单测：Review 迭代上限从 config.max_tool_iterations 读取。

覆盖面：
- make_tools_review_fn 工具取证路径：AgentLimits.max_iterations 随配置生效；
- review_file runtime 路径：缺省 limits 时同样从 config 读取；
- 兜底口径：config=None / 字段缺失 / 非数值 → REVIEW_MAX_ITERATIONS(12)；
  非正数钳到 1；
- AuditConfig 默认 12 时行为与历史硬编码完全一致（既有钉版断言 12 不变）。

Agent 循环用 monkeypatch SimpleAgentRuntime.run 捕获 limits（不真调 LLM）。
"""

from __future__ import annotations

from typing import Any

import pytest

from audit.agent.base import AgentResult
from audit.agent.runtime import SimpleAgentRuntime
from audit.agents.review import (
    REVIEW_MAX_ITERATIONS,
    _max_iterations_from_config,
    make_tools_review_fn,
    review_file,
)
from audit.config import AuditConfig
from audit.llm.base import FakeLLMClient
from audit.pipeline import PipelineContext

FILE = "app/services/orders.py"


@pytest.fixture
def _capture_runtime_run(monkeypatch):
    """捕获 runtime.run 收到的 limits；模拟正常 record_issues 收口（R1-26）。"""
    captured: dict[str, Any] = {}

    async def fake_run(self, system_prompt, messages, limits=None, on_event=None):
        captured["limits"] = limits
        await self.get_tool("record_issues").handler(issues=[])
        return AgentResult()

    monkeypatch.setattr(SimpleAgentRuntime, "run", fake_run)
    return captured


def _make_ctx(sample_workspace, fake_emitter, **config_overrides) -> PipelineContext:
    config = AuditConfig(source_path=str(sample_workspace.src_root), **config_overrides)
    return PipelineContext(
        config=config, workspace=sample_workspace, llm=FakeLLMClient(), emitter=fake_emitter
    )


# ---------------------------------------------------------------- 工具取证路径


async def test_tools_review_fn_reads_max_iterations_from_config(
    sample_workspace, fake_emitter, _capture_runtime_run
):
    """config.max_tool_iterations=5 → 工具路径 limits.max_iterations == 5。"""
    ctx = _make_ctx(sample_workspace, fake_emitter, max_tool_iterations=5)
    await make_tools_review_fn(ctx)(sample_workspace, FILE, [])
    assert _capture_runtime_run["limits"].max_iterations == 5


async def test_tools_review_fn_default_matches_history(
    sample_workspace, fake_emitter, _capture_runtime_run
):
    """默认配置（12）→ 与历史硬编码行为一致（既有钉版断言 12 继续成立）。"""
    ctx = _make_ctx(sample_workspace, fake_emitter)
    await make_tools_review_fn(ctx)(sample_workspace, FILE, [])
    assert _capture_runtime_run["limits"].max_iterations == 12


# ---------------------------------------------------------------- runtime 路径


async def test_review_file_runtime_reads_max_iterations_from_config(
    sample_workspace, _capture_runtime_run
):
    """review_file 传 config（runtime 路径，未显式给 limits）→ 上限随配置。"""
    llm = FakeLLMClient()
    runtime = SimpleAgentRuntime(llm)
    config = AuditConfig(source_path=str(sample_workspace.src_root), max_tool_iterations=7)
    await review_file(
        sample_workspace, None, llm, FILE, hints=[], runtime=runtime, config=config
    )
    assert _capture_runtime_run["limits"].max_iterations == 7


# ---------------------------------------------------------------- 兜底口径


def test_fallback_when_config_is_none():
    """config=None（review_file 兼容路径）→ 历史默认 12。"""
    assert _max_iterations_from_config(None) == 12


def test_fallback_when_field_missing_or_invalid():
    """字段缺失/非数值 → 回落 12（诚实兜底，不崩溃）。"""

    class _Empty:
        pass

    assert _max_iterations_from_config(_Empty()) == REVIEW_MAX_ITERATIONS

    class _Bad:
        max_tool_iterations = "abc"

    assert _max_iterations_from_config(_Bad()) == REVIEW_MAX_ITERATIONS


def test_non_positive_clamped_to_one():
    """非正数钳到 1（防止 AgentLimits 收到 0/负数造成未定义行为）。"""

    class _Zero:
        max_tool_iterations = 0

    class _Neg:
        max_tool_iterations = -3

    assert _max_iterations_from_config(_Zero()) == 1
    assert _max_iterations_from_config(_Neg()) == 1
