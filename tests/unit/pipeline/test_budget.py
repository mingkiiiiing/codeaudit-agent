"""预算熔断（F8 / W12-A3）单测：编排层全局 token 预算闸门。

分工说明（避免与既有测试重复）：
- agent 级轮次熔断（SimpleAgentRuntime，stop_reason="budget"，严格 `>` 语义）
  由 tests/unit/agent/test_runtime.py::test_token_budget_fuse 覆盖，本文件不重复；
- tools 审查路径的 per-file 预算公式（min(200k, config.token_budget//10)）
  由 tests/unit/agents/test_tools_review.py::test_token_budget_formula 覆盖；
- 本文件覆盖编排层新增的全局闸门 audit.orchestrator.pipeline._BudgetGateLLM：
  精确熔断点（恰好用尽 / 单笔超出两个边界）、budget=0 与负数防御、熔断后
  阶段行为（fix/testgen 纯 LLM 阶段跳过、流水线不中断）、进度事件 warning
  （文案含"预算"）、done 事件降级标注与报告统计真实口径。

闸门熔断语义（与 runtime 的 `>` 差异见实现 docstring）：
调用发起前 spent >= budget 即拒绝（在途调用允许完成并计入，故最终消耗
至多超出预算"最后一笔"的量——这是预算闸在不预知 usage 前提下的合理口径）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from audit.config import AuditConfig
from audit.errors import AgentBudgetError
from audit.llm.base import FakeLLMClient, LLMResponse
from audit.orchestrator import pipeline as orch
from audit.orchestrator.pipeline import _BudgetGateLLM, run_audit


# ---------------------------------------------------------------------- 假客户端


class _PerCallTokensLLM(FakeLLMClient):
    """每次调用上报固定 tokens 的假客户端（无限脚本）：精确控制预算累计曲线。

    覆写 chat 绕过脚本回放，固定返回 usage=(prompt, completion) 与可解析的
    空审查结论（{"issues": []}），calls/usage_totals 计数与 FakeLLM 同口径。
    """

    def __init__(self, prompt_tokens: int = 100, completion_tokens: int = 20) -> None:
        super().__init__()
        self._p = prompt_tokens
        self._c = completion_tokens

    async def chat(self, messages, tools=None, json_mode=False, temperature=0.2):
        self.calls.append(
            {"messages": messages, "tools": tools, "json_mode": json_mode, "temperature": temperature}
        )
        self.prompt_tokens += self._p
        self.completion_tokens += self._c
        return LLMResponse(
            content='{"issues": []}',
            usage={"prompt_tokens": self._p, "completion_tokens": self._c},
        )


class _InterleavingLLM(_PerCallTokensLLM):
    """带真实挂起点的假客户端：chat 期间 await 一次，模拟并发在途调用交错。"""

    def __init__(self, prompt_tokens: int = 100, completion_tokens: int = 20) -> None:
        super().__init__(prompt_tokens, completion_tokens)
        self.in_flight = 0
        self.max_in_flight = 0

    async def chat(self, messages, tools=None, json_mode=False, temperature=0.2):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)  # 挂起点：让其他并发调用通过闸门的调用前检查
            return await super().chat(messages, tools, json_mode, temperature)
        finally:
            self.in_flight -= 1


def _budget_warnings(events: list[dict]) -> list[dict]:
    """提取文案含"预算"的 warning 事件。"""
    return [e for e in events if e.get("warning") and "预算" in str(e.get("message", ""))]


def _block_understand(monkeypatch) -> None:
    """屏蔽 understand 模块：隔离其 LLM 增强调用，保证调用次数确定。"""
    monkeypatch.setitem(sys.modules, "audit.understand.architecture", None)


def _make_proj(tmp_path: Path, n_files: int) -> Path:
    """构造 n_files 个微小 Python 文件的项目（每个文件触发一次 simple 审查调用）。"""
    src = tmp_path / "proj"
    src.mkdir()
    for i in range(n_files):
        (src / f"mod_{i}.py").write_text(f"x_{i} = {i}\n", encoding="utf-8")
    return src


# ---------------------------------------------------------------------- 闸门单元：熔断点边界


async def test_gate_exact_exhaustion_blocks_next_call(fake_emitter):
    """恰好用尽边界：budget=120、每笔恰 120 → 第 1 笔放行（0<120），第 2 笔被拒。"""
    inner = _PerCallTokensLLM(prompt_tokens=100, completion_tokens=20)
    gate = _BudgetGateLLM(inner, 120, fake_emitter, {})
    resp = await gate.chat([{"role": "user", "content": "hi"}])
    assert gate.spent_tokens == 120
    assert resp.content == '{"issues": []}'

    with pytest.raises(AgentBudgetError, match="预算"):
        await gate.chat([{"role": "user", "content": "again"}])

    assert len(inner.calls) == 1  # 底层只发起过 1 次调用
    assert gate.rejected_calls == 1
    warns = _budget_warnings(fake_emitter.events)
    assert len(warns) == 1  # 首次熔断仅告警一次
    assert warns[0]["stage"] == "init"
    assert warns[0]["used_tokens"] == 120
    assert warns[0]["token_budget"] == 120


async def test_gate_single_call_overshoot_allowed_then_fused(fake_emitter):
    """超过边界：budget=100、单笔 120 → 在途单笔允许完成（120>100），此后拒绝。"""
    inner = _PerCallTokensLLM(prompt_tokens=80, completion_tokens=40)
    gate = _BudgetGateLLM(inner, 100, fake_emitter, {})
    await gate.chat([{"role": "user", "content": "hi"}])
    assert gate.spent_tokens == 120  # 最终消耗可超出预算至多"最后一笔"

    with pytest.raises(AgentBudgetError):
        await gate.chat([{"role": "user", "content": "again"}])
    assert len(inner.calls) == 1
    assert _budget_warnings(fake_emitter.events)


async def test_gate_zero_budget_blocks_first_call(fake_emitter):
    """budget=0 防御：首次调用即拒绝，底层一次都不触。"""
    inner = _PerCallTokensLLM()
    gate = _BudgetGateLLM(inner, 0, fake_emitter, {})
    with pytest.raises(AgentBudgetError, match="预算"):
        await gate.chat([{"role": "user", "content": "hi"}])
    assert inner.calls == []
    assert gate.spent_tokens == 0
    assert len(_budget_warnings(fake_emitter.events)) == 1


async def test_gate_negative_budget_blocks_first_call(fake_emitter):
    """budget 为负数：与 0 同口径防御，首次调用即拒绝。"""
    inner = _PerCallTokensLLM()
    gate = _BudgetGateLLM(inner, -5, fake_emitter, {})
    with pytest.raises(AgentBudgetError):
        await gate.chat([{"role": "user", "content": "hi"}])
    assert inner.calls == []
    assert len(_budget_warnings(fake_emitter.events)) == 1


async def test_gate_in_flight_calls_complete_and_count_after_trip(fake_emitter):
    """在途语义：熔断后已发出的调用允许完成并计入，最终消耗=真实口径。

    budget=120、每笔 120：A/B 并发通过调用前检查（spent=0）→ A 完成触发熔断
    （spent=120）→ B 完成继续累计（spent=240）→ 之后的 C 被拒。闸门 spent 与
    底层 usage_totals 一致（诚实账目，在线实测的口径来源）。
    """
    inner = _InterleavingLLM(prompt_tokens=100, completion_tokens=20)
    gate = _BudgetGateLLM(inner, 120, fake_emitter, {})

    async def fire() -> None:
        try:
            await gate.chat([{"role": "user", "content": "hi"}])
        except AgentBudgetError:
            pass

    await asyncio.gather(fire(), fire())
    assert inner.max_in_flight == 2  # 两笔并发在途
    assert gate.tripped
    assert gate.spent_tokens == 240  # 熔断后 B 仍完成并计入（不可中止在途调用）
    assert gate.usage_totals()["prompt_tokens"] == 200
    assert gate.spent_tokens == gate.usage_totals()["prompt_tokens"] + gate.usage_totals()["completion_tokens"]

    with pytest.raises(AgentBudgetError):
        await gate.chat([{"role": "user", "content": "blocked"}])
    assert len(inner.calls) == 2  # 之后的调用不再触底


async def test_gate_transparent_with_headroom(fake_emitter):
    """预算充足时闸门透明：调用全部放行、无告警、usage_totals 委托真实口径。"""
    inner = _PerCallTokensLLM()
    gate = _BudgetGateLLM(inner, 10_000, fake_emitter, {})
    for _ in range(3):
        await gate.chat([{"role": "user", "content": "hi"}])
    assert gate.spent_tokens == 360
    assert len(inner.calls) == 3
    assert _budget_warnings(fake_emitter.events) == []
    assert gate.usage_totals() == inner.usage_totals()
    assert gate.usage_totals()["llm_calls"] == 3


# ---------------------------------------------------------------------- 流水线集成：熔断后行为


async def _run_audit_with_llm(tmp_path, fake_emitter, monkeypatch, llm, **config_kwargs):
    """注入假 LLM 跑完整 run_audit，返回 (report, events, captured_ctx)。"""
    import audit.detect.engine as engine_mod

    _block_understand(monkeypatch)
    monkeypatch.setattr(orch, "_make_llm", lambda config: (llm, None))
    captured: dict = {}
    real_run_detection = engine_mod.run_detection

    async def spy_run_detection(ctx, **kwargs):
        captured["ctx"] = ctx
        return await real_run_detection(ctx, **kwargs)

    monkeypatch.setattr(engine_mod, "run_detection", spy_run_detection)

    config = AuditConfig(
        source_path=str(config_kwargs.pop("source_path")),
        work_root=str(tmp_path / "work"),
        out_dir=str(tmp_path / "out"),
        api_key="test-key",  # 让 detect 装配 review_fn（LLM 已替换为假客户端，不触网）
        enable_verify=False,
        **config_kwargs,
    )
    report = await run_audit(config, fake_emitter)
    return report, fake_emitter.events, captured


async def test_pipeline_fuse_mid_detect_degrades_honestly(tmp_path, fake_emitter, monkeypatch):
    """4 文件、budget=350、每笔 120：3 笔放行（累计 360≥350 熔断），第 4 文件被拒。

    验证：warning 文案含"预算"；后续审查调用被拒（review_errors 诚实记录）；
    流水线不中断、报告正常产出且 done 事件带 degraded + budget_tripped；
    报告 stats 为真实口径（3 次调用 / 300 prompt / 60 completion）。
    """
    src = _make_proj(tmp_path, 4)
    llm = _PerCallTokensLLM(prompt_tokens=100, completion_tokens=20)
    report, events, captured = await _run_audit_with_llm(
        tmp_path, fake_emitter, monkeypatch, llm, source_path=src, token_budget=350
    )

    # 熔断触发：3 笔放行后闸门关闭，底层永远只发生 3 次调用
    assert len(llm.calls) == 3
    warns = _budget_warnings(events)
    assert len(warns) == 1 and "熔断" in warns[0]["message"]

    # 第 4 个文件审查被拒：诚实记入 review_errors（不静默当"无问题"）
    review_errors = captured["ctx"].extra.get("review_errors") or {}
    assert any("AgentBudgetError" in str(v) for v in review_errors.values())
    assert captured["ctx"].extra["budget_tripped"] is True

    # 流水线不中断：report 阶段照常产出，done 事件带降级标注与真实消耗
    assert any(e["stage"] == "report" and "报告已生成" in e["message"] for e in events)
    done = [e for e in events if e["stage"] == "done"][-1]
    assert done["degraded"] is True
    assert done["budget_tripped"] is True
    assert done["used_tokens"] == 360
    assert done["token_budget"] == 350

    # 报告 stats 真实口径（拒绝的调用不计入）
    assert report.stats.llm_calls == 3
    assert report.stats.prompt_tokens == 300
    assert report.stats.completion_tokens == 60
    assert (tmp_path / "out" / "report.json").exists()


async def test_pipeline_budget_tripped_skips_fix_and_testgen(tmp_path, fake_emitter, monkeypatch):
    """熔断后纯 LLM 阶段跳过：detect 一笔耗尽预算 → fix/testgen 发明确跳过事件。"""
    src = _make_proj(tmp_path, 1)
    llm = _PerCallTokensLLM(prompt_tokens=100, completion_tokens=20)
    report, events, _ = await _run_audit_with_llm(
        tmp_path,
        fake_emitter,
        monkeypatch,
        llm,
        source_path=src,
        token_budget=50,  # 单笔 120 直接耗尽（发起时 0<50 允许）
        do_fix=True,
        do_tests=True,
    )

    assert len(llm.calls) == 1  # detect 后再无任何 LLM 调用
    assert _budget_warnings(events)  # 预算耗尽即时告警
    fix_skip = [e for e in events if e["stage"] == "fix" and "预算" in e["message"]]
    testgen_skip = [e for e in events if e["stage"] == "testgen" and "预算" in e["message"]]
    assert fix_skip and "跳过" in fix_skip[0]["message"]
    assert testgen_skip and "跳过" in testgen_skip[0]["message"]
    # 跳过事件携带 budget_tripped 标志（供 Web/CLI 复核）
    assert fix_skip[0]["budget_tripped"] is True

    done = [e for e in events if e["stage"] == "done"][-1]
    assert done["degraded"] is True and done["budget_tripped"] is True
    assert report.stats.llm_calls == 1  # stats 真实：只有 detect 那一笔
    assert "报告已生成" in str([e["message"] for e in events if e["stage"] == "report"])


async def test_pipeline_zero_budget_blocks_all_llm_calls(tmp_path, fake_emitter, monkeypatch):
    """budget=0 全程防御：任何 LLM 调用都不触底，审查错误如实记录，报告照常产出。"""
    src = _make_proj(tmp_path, 2)
    llm = _PerCallTokensLLM()
    report, events, captured = await _run_audit_with_llm(
        tmp_path, fake_emitter, monkeypatch, llm, source_path=src, token_budget=0
    )

    assert llm.calls == []  # 底层零调用
    assert len(_budget_warnings(events)) == 1
    review_errors = captured["ctx"].extra.get("review_errors") or {}
    assert len(review_errors) == 2  # 两个文件的审查均被拒并如实记录
    done = [e for e in events if e["stage"] == "done"][-1]
    assert done["degraded"] is True and done["budget_tripped"] is True
    assert done["used_tokens"] == 0
    assert report.stats.llm_calls == 0
    assert any(e["stage"] == "report" and "报告已生成" in e["message"] for e in events)


async def test_pipeline_default_budget_no_fuse(tmp_path, fake_emitter, monkeypatch):
    """回归保护：默认大预算（2M）下闸门透明——无"预算"告警、done 无降级标注。"""
    src = _make_proj(tmp_path, 2)
    llm = _PerCallTokensLLM()
    report, events, _ = await _run_audit_with_llm(
        tmp_path, fake_emitter, monkeypatch, llm, source_path=src
    )

    assert len(llm.calls) == 2  # 两个文件各一次审查调用
    assert _budget_warnings(events) == []
    done = [e for e in events if e["stage"] == "done"][-1]
    assert "budget_tripped" not in done
    assert "degraded" not in done
    assert report.stats.llm_calls == 2


# ---------------------------------------------------------------------- 模块缺失兜底回归


async def test_gate_does_not_break_module_missing_flow(tmp_path, fake_emitter, monkeypatch):
    """T2/T3/T4 全缺失的降级形态不被闸门破坏（审计永不整体失败）。"""
    monkeypatch.setitem(sys.modules, "audit.ingest", None)
    monkeypatch.setitem(sys.modules, "audit.indexer", None)
    monkeypatch.setitem(sys.modules, "audit.understand.architecture", None)
    monkeypatch.setitem(sys.modules, "audit.detect.engine", None)
    src = tmp_path / "proj"
    src.mkdir()
    (src / "main.py").write_text("print('hi')\n", encoding="utf-8")
    config = AuditConfig(
        source_path=str(src),
        work_root=str(tmp_path / "work"),
        out_dir=str(tmp_path / "out"),
        token_budget=0,  # 极端配置下同样不崩
    )
    report = await run_audit(config, fake_emitter)
    assert report.issues == []
    assert any(e["stage"] == "done" for e in fake_emitter.events)
    # 未发生任何 LLM 调用 → 预算从未参与熔断 → 不应误发"预算"告警
    assert _budget_warnings(fake_emitter.events) == []
