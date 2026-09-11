"""bench.efficiency 单元测试（docs/04 §3.3）：效率公式手算对账 + 计时模板生成。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bench.efficiency import (
    PHASE_AGENT_AUDIT,
    PHASE_AGENT_REVIEW,
    PHASE_HUMAN_REVIEW,
    TIMING_COLUMNS,
    calc_efficiency,
    efficiency_from_rows,
    load_timing_rows,
    write_timing_template,
)


# ---------------------------------------------------------------- calc_efficiency


def test_calc_efficiency_formula_hand_checked() -> None:
    """(100 − (20 + 10)) / 100 = 0.70，与 docs/04 §3.3 公式逐项手算一致。"""
    result = calc_efficiency(human_minutes=100.0, agent_minutes=20.0, review_minutes=10.0)
    assert result["agent_total_minutes"] == 30.0
    assert result["saved_minutes"] == 70.0
    assert result["improvement_ratio"] == pytest.approx(0.70)
    assert result["target_ratio"] == 0.70


def test_calc_efficiency_edges() -> None:
    # 人工分母为 0：不抛错，improvement 为 None
    assert calc_efficiency(0, 10, 5)["improvement_ratio"] is None
    # Agent 更慢：诚实记负值
    negative = calc_efficiency(50, 40, 30)
    assert negative["improvement_ratio"] == pytest.approx((50 - 70) / 50)


# ---------------------------------------------------------------- 计时模板


def test_write_timing_template_file(tmp_path: Path) -> None:
    """模板含表头 + 两行示例；读回后行数与列序一致。"""
    path = write_timing_template(tmp_path / "sub" / "timing.csv")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "project,reviewer,phase,start,end,notes"
    assert len(lines) == 3  # 表头 + 2 行示例
    rows = load_timing_rows(path)
    assert len(rows) == 2
    assert tuple(rows[0].keys()) == TIMING_COLUMNS
    assert rows[0]["phase"] == PHASE_HUMAN_REVIEW
    assert rows[1]["phase"] == PHASE_AGENT_REVIEW


def test_efficiency_from_rows_aggregates_by_phase() -> None:
    """60 分钟人工初评 vs 10 分钟审计 + 10 分钟复核 → 提升 (60−20)/60。"""
    rows = [
        {
            "project": "p",
            "reviewer": "r",
            "phase": PHASE_HUMAN_REVIEW,
            "start": "2026-09-11T10:00",
            "end": "2026-09-11T11:00",
            "notes": "",
        },
        {
            "project": "p",
            "reviewer": "r",
            "phase": PHASE_AGENT_AUDIT,
            "start": "2026-09-11T11:00",
            "end": "2026-09-11T11:10",
            "notes": "",
        },
        {
            "project": "p",
            "reviewer": "r",
            "phase": PHASE_AGENT_REVIEW,
            "start": "2026-09-11T11:10",
            "end": "2026-09-11T11:20",
            "notes": "",
        },
    ]
    result = efficiency_from_rows(rows)
    assert result["rows_used"] == 3
    assert result["human_minutes"] == pytest.approx(60.0)
    assert result["agent_minutes"] == pytest.approx(10.0)
    assert result["review_minutes"] == pytest.approx(10.0)
    assert result["improvement_ratio"] == pytest.approx(40.0 / 60.0)


def test_efficiency_from_rows_no_human_data(tmp_path: Path) -> None:
    """无人工初评记录（只有模板示例行外数据缺失场景）时 improvement 为 None。"""
    result = efficiency_from_rows([])
    assert result["rows_used"] == 0
    assert result["improvement_ratio"] is None
