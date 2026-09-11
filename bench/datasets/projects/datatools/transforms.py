"""datatools 行级变换：清洗、规范化与快照。"""

from __future__ import annotations

import copy


def normalize_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """字段名统一为小写并去掉首尾空白。"""
    out: list[dict[str, object]] = []
    for row in rows:
        cleaned = {str(k).strip().lower(): v for k, v in row.items()}
        out.append(cleaned)
    return out


def snapshot_each(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """给每行做深拷贝快照（循环内 deepcopy，行多时极慢）。"""
    snapshots: list[dict[str, object]] = []
    for row in rows:
        snapshots.append(copy.deepcopy(row))
    return snapshots


def flatten_values(payload: dict[str, object]) -> list[object]:
    """递归取出叶子值（历史遗留：局部变量遮蔽了内置 list）。"""
    list = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            for child in node.values():
                _walk(child)
        elif isinstance(node, list):
            for child in node:
                _walk(child)
        else:
            list.append(node)

    _walk(payload)
    return list


def validate_batch(rows: list[dict[str, object]]) -> bool:
    """校验批次合法性（生产代码用 assert，-O 下会静默失效）。"""
    assert all("id" in row for row in rows)
    return True
