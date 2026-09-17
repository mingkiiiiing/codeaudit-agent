"""W14-A2（M-2a）单测：fix 阶段把 sandbox_backend 配置传入 SandboxExecutor。

覆盖面：
- 合法配置值（subprocess/docker）原样透传给 SandboxExecutor(backend=...)；
- 非法配置值：executor 诚实降级 subprocess，阶段进度事件中如实标注 backend_note。

不真跑沙箱闭环：用 LLM 输出不可解析（generate_patch 失败 → failed）短路——
发生在 SandboxExecutor 构造之后、沙箱执行之前，专测接线本身。
SandboxExecutor 以 spy 子类替换捕获构造参数。
"""

from __future__ import annotations

from typing import Any

import pytest

import _fix_helpers as H
import audit.fix.stage as fix_stage
from audit.fix.stage import run_fix_stage
from audit.llm.base import FakeLLMClient
from audit.models import Category, Issue, Severity
from audit.sandbox.executor import SandboxExecutor


class _SpyExecutor:
    """替换 fix stage 模块命名空间中的 SandboxExecutor：捕获构造 kwargs。"""

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
    """把 fix stage 的 SandboxExecutor 换成 spy（用例间隔离）。"""
    _SpyExecutor.init_kwargs = []
    _SpyExecutor.instances = []
    monkeypatch.setattr(fix_stage, "SandboxExecutor", _SpyExecutor)
    return _SpyExecutor


def _issue() -> Issue:
    return Issue(
        id="ISS-0001",
        category=Category.BUG,
        severity=Severity.CRITICAL,
        title="裸 except",
        file="app.py",
        line_start=9,
        line_end=9,
        code_snippet="",
        description="裸 except",
        evidence=["rule_hit: demo"],
        confidence=0.9,
    )


def _note_messages(emitter: Any) -> list[str]:
    return [
        str(e.get("message", ""))
        for e in emitter.events
        if e.get("stage") == "fix" and "降级" in str(e.get("message", ""))
    ]


async def test_fix_stage_passes_backend_config(tmp_path, fake_emitter, spy_executor):
    """config.sandbox_backend="docker" → SandboxExecutor(backend="docker")。"""
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    fake = FakeLLMClient([{"content": "not-json"}])  # 不可解析 → failed（不触沙箱执行）
    ctx = H.make_ctx(ws, fake, fake_emitter, sandbox_backend="docker")
    ctx.issues = [_issue()]
    await run_fix_stage(ctx)
    assert spy_executor.init_kwargs and spy_executor.init_kwargs[0].get("backend") == "docker"


async def test_fix_stage_default_backend_subprocess(tmp_path, fake_emitter, spy_executor):
    """未配置时传 "subprocess"（与既有无参构造行为完全一致）。"""
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    fake = FakeLLMClient([{"content": "not-json"}])
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.issues = [_issue()]
    await run_fix_stage(ctx)
    assert spy_executor.init_kwargs[0].get("backend") == "subprocess"


async def test_fix_stage_invalid_backend_emits_note(tmp_path, fake_emitter, spy_executor):
    """非法配置值 → executor 降级 subprocess，进度事件如实标注。"""
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    fake = FakeLLMClient([{"content": "not-json"}])
    ctx = H.make_ctx(ws, fake, fake_emitter, sandbox_backend="podman")
    ctx.issues = [_issue()]
    await run_fix_stage(ctx)
    assert spy_executor.instances[0].backend_requested == "subprocess"
    assert spy_executor.instances[0].backend_note
    notes = _note_messages(fake_emitter)
    assert notes and "podman" in notes[0]
