"""run_soak_long 排水等待（drain）口径单测（W29 卡 B）。

覆盖：
  - _drain_split 纯函数四象限：窗口内全终态不触发 / 窗口关时在途 drain 内收编（rate=1.0）/
    drain 截止仍未终态（计入 rate 拒绝）/ drain_sec=0 等价既有行为；
  - _resolve_drain_sec 优先级（CLI > env CODEAUDIT_SOAK_DRAIN_SEC > 默认 60）与关闭语义；
  - _task_lifecycle_drain 生命周期排水路径（替身客户端：收编 / 截止超时 / 上限内零事件 / drain 关闭零事件）；
  - _render_md 新增「排水归因」行且既有行格式零变化（兼容硬约束）。

测试用替身客户端接管 _do 的 httpx 调用，并把 AUDIT_TIMEOUT_SEC / TERMINAL_GRACE_SEC
monkeypatch 到亚秒级，避免真实 240s/5s 等待；轮询间隔复用真实 POLL_INTERVAL。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from bench.stress import run_soak_long
from bench.stress.run_soak import SoakStats, _terminal_rate
from bench.stress.run_soak_long import (
    DEFAULT_DRAIN_SEC,
    _drain_split,
    _render_md,
    _resolve_drain_sec,
    _task_lifecycle_drain,
)

WC = 1000.0  # 窗口关闭时刻（monotonic 秒，任意定值）


# ---------------------------------------------------------------- 替身
class _FakeResp:
    """最小响应替身：_task_lifecycle_drain 只读 status_code 与 json()。"""

    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    """最小 httpx 客户端替身：任务查询按 state 回调返回状态，其余请求返回 404。"""

    def __init__(self, state_fn: Any) -> None:
        self._state_fn = state_fn
        self.calls: list[str] = []

    async def request(self, method: str, url: str, **_kw: Any) -> _FakeResp:
        self.calls.append(f"{method} {url}")
        if method == "GET" and url.startswith("/api/audits/"):
            return _FakeResp(200, {"status": self._state_fn()})
        return _FakeResp(404)


# ---------------------------------------------------------------- _drain_split 纯函数
def test_drain_not_triggered_when_all_terminal_in_window() -> None:
    """窗口内全终态：无在途事件，drain 不触发，归因 (0, 0)。"""
    assert _drain_split([], DEFAULT_DRAIN_SEC, [], WC) == (0, 0)


def test_drain_rescues_inflight_and_rate_passes() -> None:
    """窗口关闭时 2 个在途、drain 内转终态：收编 2，终态率 100%。"""
    caps = [WC - 5.0, WC - 11.0]
    finishes = [WC + 30.0, WC + 59.5]
    assert _drain_split(caps, 60.0, finishes, WC) == (2, 0)
    stats = SoakStats()
    stats.admitted = 2
    stats.terminals["done"] = 2
    assert _terminal_rate(stats) == 1.0


def test_drain_deadline_still_running_rejected_by_rate() -> None:
    """drain 截止仍未终态：计入 still_running，终态率 < 1.0 照旧 FAIL。"""
    # 一个 drain 内收编 + 一个截止仍未终态
    assert _drain_split([WC - 5.0, WC - 11.0], 60.0, [WC + 30.0, None], WC) == (1, 1)
    # 完成时刻越过 drain 截止同样维持未终态（drain 只会改善不会恶化的上界）
    assert _drain_split([WC - 5.0], 60.0, [WC + 61.0], WC) == (0, 1)
    stats = SoakStats()
    stats.admitted = 2
    stats.terminals["done"] = 1
    stats.terminals["timeout"] = 1
    rate = _terminal_rate(stats)
    assert rate is not None and rate < 1.0


def test_drain_zero_equals_legacy_behavior() -> None:
    """drain_sec=0：零归因（不触发排水），终态率口径与既有完全一致（超时即拒绝）。"""
    assert _drain_split([WC - 5.0, WC - 11.0], 0.0, [WC + 30.0, None], WC) == (0, 0)
    assert _drain_split([WC - 5.0], -1.0, [WC + 1.0], WC) == (0, 0)  # 负值同关闭
    stats = SoakStats()
    stats.admitted = 2
    stats.terminals["done"] = 1
    stats.terminals["timeout"] = 1
    assert _terminal_rate(stats) == 0.5


# ---------------------------------------------------------------- 参数解析
def test_resolve_drain_sec_priority_and_off_switch() -> None:
    """CLI 优先于 env；env 覆盖默认；缺省 60；0/负值=关闭；非法 env 回退默认。"""
    env = {"CODEAUDIT_SOAK_DRAIN_SEC": "120"}
    assert _resolve_drain_sec(5.0, env) == 5.0  # CLI 优先
    assert _resolve_drain_sec(0.0, env) == 0.0  # 显式 0=关闭，同样优先
    assert _resolve_drain_sec(None, env) == 120.0  # env 覆盖默认
    assert _resolve_drain_sec(None, {}) == DEFAULT_DRAIN_SEC  # 缺省 60
    assert _resolve_drain_sec(-3.0, {}) == 0.0  # 负值按关闭
    assert _resolve_drain_sec(None, {"CODEAUDIT_SOAK_DRAIN_SEC": "abc"}) == DEFAULT_DRAIN_SEC  # 非法回退


# ---------------------------------------------------------------- 生命周期排水路径
def test_lifecycle_drain_rescues_slow_task(monkeypatch: Any) -> None:
    """超 240s 审计上限后 drain 内转终态：照常计 done，记录收编事件，保留窗口后仍 DELETE。"""
    monkeypatch.setattr(run_soak_long, "AUDIT_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(run_soak_long, "TERMINAL_GRACE_SEC", 0.0)
    stats = SoakStats()
    events: list[tuple[float, float | None]] = []
    polls = {"n": 0}

    def state() -> str:
        polls["n"] += 1
        return "done" if polls["n"] >= 3 else "running"

    window_close = time.monotonic()
    client = _FakeClient(state)
    asyncio.run(run_soak_long._task_lifecycle_drain(client, stats, "a1", window_close + 5.0, events))
    assert stats.terminals["done"] == 1
    assert stats.terminals["timeout"] == 0
    assert len(events) == 1
    cap, fin = events[0]
    assert fin is not None and fin > cap  # 越过原上限才转终态 → 属排水收编
    assert _drain_split([cap], 5.0, [fin], window_close) == (1, 0)
    assert client.calls[-1].startswith("DELETE")  # 清理口径不变


def test_lifecycle_drain_deadline_still_running(monkeypatch: Any) -> None:
    """drain 截止仍未终态：计 timeout（终态率拒绝），记录 (原上限, None) 事件。"""
    monkeypatch.setattr(run_soak_long, "AUDIT_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(run_soak_long, "TERMINAL_GRACE_SEC", 0.0)
    stats = SoakStats()
    events: list[tuple[float, float | None]] = []
    window_close = time.monotonic()
    asyncio.run(
        run_soak_long._task_lifecycle_drain(
            _FakeClient(lambda: "running"), stats, "a2", window_close + 0.4, events
        )
    )
    assert stats.terminals["timeout"] == 1
    assert len(events) == 1 and events[0][1] is None
    cap, fin = events[0]
    assert _drain_split([cap], 0.4, [fin], window_close) == (0, 1)


def test_lifecycle_fast_finish_records_no_event(monkeypatch: Any) -> None:
    """审计上限内转终态：零排水事件，与既有行为无差别。"""
    monkeypatch.setattr(run_soak_long, "AUDIT_TIMEOUT_SEC", 30.0)
    monkeypatch.setattr(run_soak_long, "TERMINAL_GRACE_SEC", 0.0)
    stats = SoakStats()
    events: list[tuple[float, float | None]] = []
    window_close = time.monotonic()
    asyncio.run(
        run_soak_long._task_lifecycle_drain(
            _FakeClient(lambda: "done"), stats, "a3", window_close + 60.0, events
        )
    )
    assert stats.terminals["done"] == 1
    assert events == []


def test_lifecycle_drain_off_no_event(monkeypatch: Any) -> None:
    """drain 关闭（drain_deadline=None）：不追加等待、不记事件，超时照旧——等价既有行为。"""
    monkeypatch.setattr(run_soak_long, "AUDIT_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(run_soak_long, "TERMINAL_GRACE_SEC", 0.0)
    stats = SoakStats()
    events: list[tuple[float, float | None]] = []
    asyncio.run(
        run_soak_long._task_lifecycle_drain(_FakeClient(lambda: "running"), stats, "a4", None, events)
    )
    assert stats.terminals["timeout"] == 1
    assert events == []


# ---------------------------------------------------------------- 报告渲染（兼容硬约束）
def _fake_env() -> dict[str, str]:
    return {
        "now": "2026-09-19 00:00:00",
        "host": "ut",
        "platform": "Windows 11",
        "cpu": "x86",
        "python": "3.11.0",
        "base": "http://127.0.0.1:8947",
        "quick": "1",
        "duration": "300",
        "task_period": "6",
    }


def _fake_stats() -> SoakStats:
    """非空采样：_render_md 的 RSS 诊断段要求至少一个 rss>0 样本（既有前提）。"""
    stats = SoakStats()
    stats.samples = [
        {"t": 0.0, "rss": 100.0, "active": 0.0, "total": 0.0, "cum_done": 0.0},
        {"t": 10.0, "rss": 101.0, "active": 1.0, "total": 2.0, "cum_done": 1.0},
    ]
    return stats


def _fake_verdict(**drain: Any) -> dict[str, Any]:
    verdict: dict[str, Any] = {
        "wall_load": 300.0,
        "wall_total": 301.0,
        "drain_wall": 1.0,
        "terminal_rate": 1.0,
        "rss_slope": (0.1, 20, 0.2),
        "rss_corr": None,
        "rss_first": 100.0,
        "rss_last": 102.0,
        "final_total": 3,
        "canary_checked": 1,
        "canary_bad": 0,
        "checks": [("接纳任务终态率 100%", "PASS", "(36+0)/36，超时未终态=0")],
        "overall": "PASS",
    }
    verdict.update(drain)
    return verdict


def test_render_md_adds_drain_row_and_keeps_legacy_rows() -> None:
    """drain 开启：新增排水归因行，既有摘要行格式零变化（抽查）。"""
    md = _render_md(
        _fake_env(), _fake_stats(), _fake_verdict(drain_sec=60.0, drain_rescued=2, drain_still_running=0)
    )
    assert "排水：60s 内收编 2 任务，仍 0 在途" in md
    assert "| 排水归因 | 排水：60s 内收编 2 任务，仍 0 在途 |" in md
    # 既有行零变化（抽查三行既有摘要行）
    assert "| 终态率 (done+failed)/admitted | 100.0% |" in md
    assert "| 任务 提交 / 接纳(admitted) | 0 / 0 |" in md
    assert "| 负载期实际时长 / 含排空总时长 | 300.0s / 301.0s（排空 1.0s） |" in md


def test_render_md_drain_off_note() -> None:
    """drain 关闭或旧形状 verdict（无 drain 键）：不崩且如实标注关闭（向后兼容）。"""
    md_off = _render_md(
        _fake_env(), _fake_stats(), _fake_verdict(drain_sec=0.0, drain_rescued=0, drain_still_running=0)
    )
    assert "| 排水归因 | 排水：关闭（--drain-sec=0，等价既有口径） |" in md_off
    md_legacy = _render_md(_fake_env(), _fake_stats(), _fake_verdict())
    assert "排水：关闭" in md_legacy
