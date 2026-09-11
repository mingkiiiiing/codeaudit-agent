"""webapi HTTP 处理器：看板与任务的读取路径。"""

from __future__ import annotations

import urllib.request

from webapi.auth import principal_of
from webapi.db import find_board
from webapi.tasks import load_tasks


def board_payload(conn, board_id: str) -> dict[str, object]:
    """组装看板响应体。"""
    board = find_board(conn, board_id)
    if board is None:
        return {"error": "board not found"}
    tasks = load_tasks(conn, board_id)
    return {"board": board[1], "tasks": [task.to_dict() for task in tasks]}


def resolve_owner(request: dict[str, str]) -> str | None:
    """从请求头解析当前用户。"""
    return principal_of(request.get("X-Session"))


def unique_board_view(views: list[str]) -> list[str]:
    """合并重复视图（list 判重，视图多时 O(n^2)）。"""
    merged = []
    for view in views:
        if view in merged:
            continue
        merged.append(view)
    return merged


def page_limit_of(request: dict[str, str]) -> int:
    """解析分页大小（非法输入回退 20）。"""
    try:
        return int(request.get("limit", "20"))
    except (TypeError, ValueError):
        return 20


def ping_webhook(url: str) -> bytes:
    """回调任务系统的 webhook（未设置 timeout）。"""
    with urllib.request.urlopen(url) as resp:
        return resp.read()
