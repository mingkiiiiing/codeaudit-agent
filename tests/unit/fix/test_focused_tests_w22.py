"""W22-F 受影响测试选择测试：选择器语义 + fix 阶段聚焦运行集成。

选择器（_affected_test_target）：issue 所在符号的直接 callers 中的测试文件；
fix 集成：注入假索引后 verified 路径走聚焦运行（focused_tests_runs 计数 +
事件文案标注聚焦目标），无索引场景保持整库（既有行为）。
"""

from __future__ import annotations

from pathlib import Path

from audit.llm.base import FakeLLMClient
from audit.models import Category, FixStatus, Issue, Severity, Symbol

import _fix_helpers as H
from audit.fix.stage import _affected_test_target, run_fix_stage


def _issue(file: str, line: int = 9) -> Issue:
    return Issue(
        id="ISS-0001",
        category=Category.BUG,
        severity=Severity.CRITICAL,
        title="裸 except",
        file=file,
        line_start=line,
        line_end=line,
        description="x",
        evidence=["rule_hit: demo"],
        confidence=0.9,
    )


class FakeIndex:
    """最小假索引：symbols_for_file + call_chain 按预设返回。"""

    def __init__(self, symbols: dict[str, list[Symbol]], chains: dict[str, list[str]]):
        self._symbols = symbols
        self._chains = chains
        self.chain_calls: list[str] = []

    def symbols_for_file(self, rel: str) -> list[Symbol]:
        return self._symbols.get(rel, [])

    def call_chain(self, symbol_name: str, direction: str = "callees", depth: int = 1) -> list[str]:
        self.chain_calls.append(symbol_name)
        return self._chains.get(symbol_name, [])


def _sym(name: str, start: int, end: int) -> Symbol:
    return Symbol(id=f"x::{name}", file="app.py", kind="function", name=name, line_start=start, line_end=end, signature="")


# ---------------------------------------------------------------- 选择器单测


async def test_selector_returns_first_test_file(tmp_path: Path, fake_emitter):
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY, "tests/test_app.py": H.TEST_APP_PY})
    ctx = H.make_ctx(ws, FakeLLMClient(), fake_emitter)
    ctx.index = FakeIndex(
        symbols={"app.py": [_sym("divide", 5, 20)]},
        chains={"divide": ["tests/test_app.py:12 test_divide -> app.py:9 divide", "other.py:1 x -> app.py:9 divide"]},
    )
    assert _affected_test_target(ctx, _issue("app.py")) == "tests/test_app.py"


async def test_selector_no_test_callers_returns_empty(tmp_path: Path, fake_emitter):
    """callers 全是非测试文件 → ""（整库回退）。"""
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    ctx = H.make_ctx(ws, FakeLLMClient(), fake_emitter)
    ctx.index = FakeIndex(
        symbols={"app.py": [_sym("divide", 5, 20)]},
        chains={"divide": ["main.py:3 run -> app.py:9 divide"]},
    )
    assert _affected_test_target(ctx, _issue("app.py")) == ""


async def test_selector_no_covering_symbol_returns_empty(tmp_path: Path, fake_emitter):
    """issue 行区间不落在任何符号内 → ""。"""
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    ctx = H.make_ctx(ws, FakeLLMClient(), fake_emitter)
    ctx.index = FakeIndex(symbols={"app.py": [_sym("divide", 5, 8)]}, chains={"divide": []})
    assert _affected_test_target(ctx, _issue("app.py", line=100)) == ""


async def test_selector_no_index_returns_empty(tmp_path: Path, fake_emitter):
    """索引缺失 → ""（诚实回退整库）。"""
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    ctx = H.make_ctx(ws, FakeLLMClient(), fake_emitter)
    assert ctx.index is None
    assert _affected_test_target(ctx, _issue("app.py")) == ""


async def test_selector_caches_per_file(tmp_path: Path, fake_emitter):
    """同文件多 Issue 复用缓存（call_chain 只反查一次）。"""
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY, "tests/test_app.py": H.TEST_APP_PY})
    ctx = H.make_ctx(ws, FakeLLMClient(), fake_emitter)
    index = FakeIndex(
        symbols={"app.py": [_sym("divide", 5, 20)]},
        chains={"divide": ["tests/test_app.py:12 test_divide -> app.py:9 divide"]},
    )
    ctx.index = index
    first = _affected_test_target(ctx, _issue("app.py"))
    second = _affected_test_target(ctx, _issue("app.py", line=15))
    assert first == second == "tests/test_app.py"
    assert index.chain_calls == ["divide"]  # 第二次命中缓存，未再反查


async def test_selector_chain_error_falls_back(tmp_path: Path, fake_emitter):
    """反查异常 → ""（不阻断修复闭环）。"""
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    ctx = H.make_ctx(ws, FakeLLMClient(), fake_emitter)

    class BrokenIndex(FakeIndex):
        def call_chain(self, symbol_name, direction="callees", depth=1):
            raise RuntimeError("db closed")

    ctx.index = BrokenIndex(symbols={"app.py": [_sym("divide", 5, 20)]}, chains={})
    assert _affected_test_target(ctx, _issue("app.py")) == ""


# ---------------------------------------------------------------- fix 阶段集成


async def test_fix_stage_focuses_affected_test_and_counts(tmp_path: Path, fake_emitter):
    """注入假索引后 verified 路径：聚焦 tests/test_app.py 运行、focused_tests_runs=1、事件标注。"""
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY, "tests/test_app.py": H.TEST_APP_PY})
    fake = FakeLLMClient([H.llm_json(H.BARE_EXCEPT_DIFF, "裸 except 改为精确捕获")])
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.index = FakeIndex(
        symbols={"app.py": [_sym("divide", 5, 20)]},
        chains={"divide": ["tests/test_app.py:12 test_divide -> app.py:9 divide"]},
    )
    issue = _issue("app.py")
    ctx.issues = [issue]

    await run_fix_stage(ctx)

    assert ctx.patches and ctx.patches[0].apply_status == "verified"
    assert issue.fix_status == FixStatus.VERIFIED
    assert ctx.extra["fix_stats"]["focused_tests_runs"] == 1
    events = [e for e in fake_emitter.events if e.get("stage") == "fix" and "聚焦" in str(e.get("message", ""))]
    assert events and "tests/test_app.py" in events[0]["message"]


async def test_fix_stage_without_index_stays_full_run(tmp_path: Path, fake_emitter):
    """无索引（默认场景）：整库运行、focused_tests_runs=0（W22-F 之前行为不变）。"""
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY, "tests/test_app.py": H.TEST_APP_PY})
    fake = FakeLLMClient([H.llm_json(H.BARE_EXCEPT_DIFF, "裸 except 改为精确捕获")])
    ctx = H.make_ctx(ws, fake, fake_emitter)
    issue = _issue("app.py")
    ctx.issues = [issue]

    await run_fix_stage(ctx)

    assert ctx.patches and ctx.patches[0].apply_status == "verified"
    assert ctx.extra["fix_stats"]["focused_tests_runs"] == 0
    events = [e for e in fake_emitter.events if e.get("stage") == "fix" and "整库" in str(e.get("message", ""))]
    assert events
