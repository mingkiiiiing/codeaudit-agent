"""Stage3 架构理解：启发式底座 + 可选 LLM 增强（map-reduce 的单机简化实现，T4）。"""

from __future__ import annotations

from audit.understand.architecture import build_architecture, find_entry_points

__all__ = ["build_architecture", "find_entry_points"]
