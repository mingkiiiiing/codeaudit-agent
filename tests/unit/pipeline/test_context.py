"""PipelineContext / EventEmitter 契约单测（W15-F 卡F：测试盲区补齐）。

契约结论固化（与审计 A7「静默吞噬簇」相关联）：
- PipelineContext.emit 本身【不吞噬异常】——emitter 抛出的异常原样向上传播
  （见 test_emitter_exception_propagates）。A7 的静默吞噬不发生在本契约文件，
  而是发生在调用侧：audit/understand/architecture.py:284-285 把 emit 包进
  try/except pass（进度事件 best-effort），其记账清单见
  tests/unit/architecture/test_architecture_logging.py。
- fixture 风格沿用 tests/conftest.py 的 pipeline_ctx / fake_emitter。
"""

from __future__ import annotations

import pytest

from audit.config import AuditConfig
from audit.llm.base import FakeLLMClient
from audit.models import AuditStats
from audit.pipeline import PipelineContext


class TestFieldDefaults:
    def test_mutable_fields_default_to_fresh_instances(self, pipeline_ctx):
        # 全部列表字段默认空、且不共享底层数组（default_factory 语义）
        assert pipeline_ctx.rule_hits == []
        assert pipeline_ctx.candidates == []
        assert pipeline_ctx.issues == []
        assert pipeline_ctx.refactor_proposals == []
        assert pipeline_ctx.patches == []
        assert pipeline_ctx.test_cases == []
        pipeline_ctx.issues.append("sentinel")
        assert pipeline_ctx.candidates == []
        assert pipeline_ctx.rule_hits == []

    def test_optional_fields_default_none(self, pipeline_ctx):
        assert pipeline_ctx.index is None
        assert pipeline_ctx.architecture is None

    def test_stats_and_extra_defaults(self, pipeline_ctx):
        assert isinstance(pipeline_ctx.stats, AuditStats)
        assert pipeline_ctx.stats.duration_sec == 0.0
        assert pipeline_ctx.extra == {}

    def test_instances_do_not_share_mutable_state(self, pipeline_ctx):
        other = PipelineContext(
            config=pipeline_ctx.config,
            workspace=pipeline_ctx.workspace,
            llm=FakeLLMClient(),
            emitter=pipeline_ctx.emitter,
        )
        other.issues.append("other")
        other.extra["k"] = "v"
        assert pipeline_ctx.issues == []
        assert pipeline_ctx.extra == {}
        assert other.stats is not pipeline_ctx.stats  # stats 也必须各自实例化

    def test_constructor_wiring_kept(self, pipeline_ctx, fake_emitter, sample_workspace):
        assert isinstance(pipeline_ctx.config, AuditConfig)
        assert pipeline_ctx.workspace is sample_workspace
        assert isinstance(pipeline_ctx.llm, FakeLLMClient)
        assert pipeline_ctx.emitter is fake_emitter


class TestEmit:
    async def test_minimal_event_shape(self, pipeline_ctx):
        await pipeline_ctx.emit("detect", "扫描中")
        assert pipeline_ctx.emitter.events == [
            {"type": "progress", "stage": "detect", "message": "扫描中"}
        ]

    async def test_current_total_included_only_when_given(self, pipeline_ctx):
        await pipeline_ctx.emit("understand", "架构理解", current=3, total=7)
        ev = pipeline_ctx.emitter.events[0]
        assert ev["current"] == 3
        assert ev["total"] == 7

        await pipeline_ctx.emit("report", "报告生成")
        ev2 = pipeline_ctx.emitter.events[1]
        assert "current" not in ev2
        assert "total" not in ev2

    async def test_extra_data_merged_into_event(self, pipeline_ctx):
        await pipeline_ctx.emit("fix", "应用补丁", patch_id="PATCH-0001", count=2)
        ev = pipeline_ctx.emitter.events[0]
        assert ev["patch_id"] == "PATCH-0001"
        assert ev["count"] == 2
        # 保留键不被 extra_data 挤掉
        assert ev["type"] == "progress"
        assert ev["stage"] == "fix"

    async def test_extra_data_may_override_reserved_keys(self, pipeline_ctx):
        # W15-F 现状固化：event.update(extra_data) 后写覆盖，调用方可覆盖 type/stage
        # 等保留键——这是当前契约的「调用方说了算」语义；若契约收紧为禁止覆盖，
        # 本测试应同步更新。
        await pipeline_ctx.emit("detect", "msg", type="custom")
        assert pipeline_ctx.emitter.events[0]["type"] == "custom"

    async def test_events_arrive_in_order(self, pipeline_ctx):
        for i in range(3):
            await pipeline_ctx.emit("ingest", f"step {i}", current=i, total=3)
        assert [e["message"] for e in pipeline_ctx.emitter.events] == [
            "step 0",
            "step 1",
            "step 2",
        ]
        assert all(e["type"] == "progress" for e in pipeline_ctx.emitter.events)


class TestEventEmitterContract:
    async def test_emitter_exception_propagates(self, pipeline_ctx):
        # 契约固化（审计 A7 关联结论）：PipelineContext.emit 不做 try/except，
        # emitter 的异常原样上抛、由调用阶段决定重试或降级——
        # 吞噬行为存在于 architecture.py:284-285 等调用侧，不在本契约。
        async def broken_emitter(event):
            raise RuntimeError("SSE 连接中断")

        pipeline_ctx.emitter = broken_emitter
        with pytest.raises(RuntimeError, match="SSE 连接中断"):
            await pipeline_ctx.emit("detect", "这条事件发不出去")

    async def test_non_awaitable_emitter_fails_loudly(self, pipeline_ctx):
        # 同上：契约不掩盖用法错误——emitter 返回非 Awaitable（同步函数返回 None）时
        # await 处立刻 TypeError，不会被转成静默失败
        def sync_emitter(event):
            return None

        pipeline_ctx.emitter = sync_emitter
        with pytest.raises(TypeError):
            await pipeline_ctx.emit("detect", "msg")
