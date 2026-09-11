"""datatools 输入读取器：CSV 与 JSONL。"""

from __future__ import annotations

import json


def read_csv_rows(path: str) -> list[dict[str, str]]:
    """读取 CSV（逗号分隔，演示实现不处理引号转义）。"""
    rows: list[dict[str, str]] = []
    handle = open(path, "r", encoding="utf-8")
    header = handle.readline().strip().split(",")
    for line in handle:
        cells = line.strip().split(",")
        rows.append(dict(zip(header, cells)))
    return rows


def read_jsonl(path: str) -> list[dict[str, object]]:
    """逐行读取 JSONL（坏行被静默吞掉，故障不可见）。"""
    rows: list[dict[str, object]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def peek_header(path: str) -> list[str]:
    """只读表头（供字段映射预览）。"""
    with open(path, "r", encoding="utf-8") as fh:
        return fh.readline().strip().split(",")
