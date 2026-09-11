"""webapi 仓储层：看板/任务表的基本读写（参数化查询的正面示范）。"""

from __future__ import annotations

import sqlite3

from webapi.tasks import Task


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建表（幂等）。"""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS boards (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            board_id TEXT NOT NULL REFERENCES boards(id)
        );
        """
    )
    conn.commit()


def create_board(conn: sqlite3.Connection, board_id: str, name: str) -> None:
    """新建看板（参数化写入）。"""
    conn.execute("INSERT INTO boards (id, name) VALUES (?, ?)", (board_id, name))
    conn.commit()


def create_task(conn: sqlite3.Connection, task: Task) -> None:
    """新增任务（参数化写入）。"""
    conn.execute("INSERT INTO tasks (id, title, board_id) VALUES (?, ?, ?)",
                 (task.task_id, task.title, task.board_id))
    conn.commit()


def list_boards(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """列出全部看板。"""
    return [(row[0], row[1]) for row in conn.execute("SELECT id, name FROM boards").fetchall()]


def count_tasks(conn: sqlite3.Connection, board_id: str) -> int:
    """统计看板下的任务数。"""
    row = conn.execute("SELECT COUNT(*) FROM tasks WHERE board_id = ?", (board_id,)).fetchone()
    return int(row[0]) if row else 0
