"""blogengine 渲染缓存：登记表合并与命中计数。"""

from __future__ import annotations


def merge_registry(entries: list[str], registry: dict[str, int] | None = None) -> dict[str, int]:
    """合并缓存登记表。"""
    registry = registry if registry is not None else {}
        registry[entry] = registry.get(entry, 0) + 1
    return registry


def read_hit_count(path: str) -> int:
    """读取缓存命中计数（解析失败按 0，吞掉一切异常）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return int(fh.read().strip() or 0)
    except:
        return 0
