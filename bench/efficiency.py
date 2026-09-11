"""效率对比实验（docs/04 §3.3）：人工初评 vs Agent 端到端 + 人工复核。

口径（docs/04 §3.3）::

    人工总耗时 T_human（初评）
    Agent 总耗时 T_agent_end2end + 人工复核耗时 T_review
    效率提升 = (T_human − (T_agent_end2end + T_review)) / T_human   → 目标 ≥ 70%

:func:`write_timing_template` 生成计时 CSV 模板（含两行示例），实验者按
项目 × 评审者 × 阶段逐条记录起止时间；:func:`load_timing_rows` 读回后可用
:func:`efficiency_from_rows` 按阶段聚合出 calc_efficiency 的输入。

阶段约定（phase 列取值）：``human-review``＝人工初评；``agent-audit``＝Agent
端到端审计；``agent-result-review``＝开发者复核 Agent 报告（剔除误报）。
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = [
    "PHASE_AGENT_AUDIT",
    "PHASE_AGENT_REVIEW",
    "PHASE_HUMAN_REVIEW",
    "TIMING_COLUMNS",
    "calc_efficiency",
    "efficiency_from_rows",
    "load_timing_rows",
    "write_timing_template",
]

TIMING_COLUMNS: tuple[str, ...] = ("project", "reviewer", "phase", "start", "end", "notes")

PHASE_HUMAN_REVIEW = "human-review"
PHASE_AGENT_AUDIT = "agent-audit"
PHASE_AGENT_REVIEW = "agent-result-review"

_TEMPLATE_EXAMPLE_ROWS: tuple[dict[str, str], ...] = (
    {
        "project": "demo_proj",
        "reviewer": "reviewer-A",
        "phase": PHASE_HUMAN_REVIEW,
        "start": "2026-09-11T10:00",
        "end": "2026-09-11T10:45",
        "notes": "示例：人工初评级评审（只找问题、不修复），请替换为真实记录",
    },
    {
        "project": "demo_proj",
        "reviewer": "reviewer-A",
        "phase": PHASE_AGENT_REVIEW,
        "start": "2026-09-11T11:00",
        "end": "2026-09-11T11:08",
        "notes": "示例：复核 Agent 报告剔除误报，请替换为真实记录",
    },
)


def calc_efficiency(
    human_minutes: float, agent_minutes: float, review_minutes: float
) -> dict[str, float | None]:
    """按 docs/04 §3.3 口径计算效率提升（分钟数任意同单位均可）。

    - ``agent_total_minutes = agent_minutes + review_minutes``；
    - ``improvement_ratio = (human − agent_total) / human``，``human <= 0`` 时为
      ``None``（分母非法，不抛错）；结果可为负（Agent 更慢时诚实记负值）。
    """
    human = float(human_minutes)
    agent = float(agent_minutes)
    review = float(review_minutes)
    agent_total = agent + review
    improvement = ((human - agent_total) / human) if human > 0 else None
    return {
        "human_minutes": human,
        "agent_minutes": agent,
        "review_minutes": review,
        "agent_total_minutes": agent_total,
        "saved_minutes": human - agent_total,
        "improvement_ratio": improvement,
        "target_ratio": 0.70,  # docs/04 §3.3 目标 ≥ 70%
    }


def write_timing_template(path: Path) -> Path:
    """生成计时 CSV 模板（表头 + 两行示例），返回写入路径；父目录自动创建。"""
    path = Path(path)
    if path.parent != Path(""):
        path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(TIMING_COLUMNS))
        writer.writeheader()
        for row in _TEMPLATE_EXAMPLE_ROWS:
            writer.writerow(row)
    return path


def load_timing_rows(path: Path) -> list[dict[str, str]]:
    """读回计时 CSV（DictReader），逐行剥掉空白；空文件返回空列表。"""
    path = Path(path)
    with path.open("r", encoding="utf-8", newline="") as fh:
        return [
            {k: (v or "").strip() for k, v in row.items() if k is not None}
            for row in csv.DictReader(fh)
        ]


def _minutes_between(start: str, end: str) -> float:
    """解析模板中的起止时间（ISO 格式，容忍仅到分钟），返回分钟数；非法记 0。"""
    try:
        seconds = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, seconds / 60.0)


def efficiency_from_rows(rows: list[dict[str, str]]) -> dict[str, Any]:
    """把计时记录按阶段聚合为效率对比结果。

    - ``human-review`` 行的时长求和 → T_human；
    - ``agent-audit`` + ``agent-result-review`` 行的时长分别求和 →
      T_agent_end2end / T_review，交给 :func:`calc_efficiency`；
    - 识别不到任何人工初评记录时 ``improvement_ratio`` 为 ``None``；
    - 附 ``rows_used``（有效行数）便于核对样本量（诚实性：小样本要写明）。
    """
    human = 0.0
    agent = 0.0
    review = 0.0
    used = 0
    for row in rows:
        phase = str(row.get("phase", ""))
        minutes = _minutes_between(str(row.get("start", "")), str(row.get("end", "")))
        if phase == PHASE_HUMAN_REVIEW:
            human += minutes
            used += 1
        elif phase == PHASE_AGENT_AUDIT:
            agent += minutes
            used += 1
        elif phase == PHASE_AGENT_REVIEW:
            review += minutes
            used += 1
    result = calc_efficiency(human, agent, review)
    return {"rows_used": used, **result}
