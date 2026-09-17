"""W14-A2（M-2a）单测：testgen 阶段把 sandbox_backend 配置传入 SandboxExecutor。

覆盖面：
- 合法配置值（docker）原样透传给 SandboxExecutor(backend=...)；
- 非法配置值：executor 诚实降级 subprocess，阶段进度事件中如实标注 backend_note。

不真跑沙箱闭环：用 LLM 首轮 UNTESTABLE 判定短路（发生在 SandboxExecutor 构造
之后、沙箱执行之前），专测接线本身。SandboxExecutor 以 spy 子类替换捕获参数。
"""

from __future__ import annotations

from typing import Any

import pytest

import _testgen_helpers as TH
import audit.testgen.stage as testgen_stage
from audit.llm.base import FakeLLMClient
from audit.sandbox.executor import SandboxExecutor
from audit.testgen.stage import run_testgen_stage

MATHX = "app/utils/mathx.py"


class _SpyExecutor:
    """替换 testgen stage 模块命名空间中的 SandboxExecutor：捕获构造 kwargs。"""

    init_kwargs: list[dict[str, Any]] = []
    instances: list[Any] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self._inner = SandboxExecutor(*args, **kwargs)
        type(self).instances.append(self)
        type(self).init_kwargs.append(kwargs)

    def __getattr__(self, name: str) -> Any:  # 代理真实 executor
        return getattr(self._inner, name)


@pytest.fixture
def spy_executor(monkeypatch):
    """把 testgen stage 的 SandboxExecutor 换成 spy（用例间隔离）。"""
    _SpyExecutor.init_kwargs = []
    _SpyExecutor.instances = []
    monkeypatch.setattr(testgen_stage, "SandboxExecutor", _SpyExecutor)
    return _SpyExecutor


def _note_messages(emitter: Any) -> list[str]:
    return [
        str(e.get("message", ""))
        for e in emitter.events
        if e.get("stage") == "testgen" and "降级" in str(e.get("message", ""))
    ]


async def test_testgen_stage_passes_backend_config(sample_workspace, fake_emitter, spy_executor):
    """config.sandbox_backend="docker" → SandboxExecutor(backend="docker")。"""
    fake = FakeLLMClient([{"content": TH.UNTESTABLE_TEXT}])  # 首轮 UNTESTABLE → 跳过
    ctx = TH.make_ctx(sample_workspace, fake, fake_emitter, sandbox_backend="docker")
    ctx.extra["testgen_targets"] = [(MATHX, "compare")]
    await run_testgen_stage(ctx)
    assert spy_executor.init_kwargs and spy_executor.init_kwargs[0].get("backend") == "docker"


async def test_testgen_stage_invalid_backend_emits_note(
    sample_workspace, fake_emitter, spy_executor
):
    """非法配置值 → executor 降级 subprocess，进度事件如实标注。"""
    fake = FakeLLMClient([{"content": TH.UNTESTABLE_TEXT}])
    ctx = TH.make_ctx(sample_workspace, fake, fake_emitter, sandbox_backend="podman")
    ctx.extra["testgen_targets"] = [(MATHX, "compare")]
    await run_testgen_stage(ctx)
    assert spy_executor.instances[0].backend_requested == "subprocess"
    assert spy_executor.instances[0].backend_note
    notes = _note_messages(fake_emitter)
    assert notes and "podman" in notes[0]
