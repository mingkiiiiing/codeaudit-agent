"""重构方案生成器（W7-A2，契约 v1.7）：赛题要求 5「自动生成重构方案」。

分层设计（docs/12 §4-2）：
- heuristics：确定性底座，零 LLM，从既有索引与规则命中聚合出结构化方案；
- llm：可选增强层，对 top N（≤5）启发式方案深化 rationale/steps，失败安全降级；
- stage：run_refactor_stage 阶段入口（detect 之后、fix 之前），聚合 → 增强 → 写 ctx。
"""

from __future__ import annotations

from audit.refactor.rename import DefSite, FilePatch, RenamePlan, RenameResult, ReplacePoint, apply_rename, plan_rename
from audit.refactor.stage import run_refactor_stage

__all__ = [
    "DefSite",
    "FilePatch",
    "RenamePlan",
    "RenameResult",
    "ReplacePoint",
    "apply_rename",
    "plan_rename",
    "run_refactor_stage",
]
