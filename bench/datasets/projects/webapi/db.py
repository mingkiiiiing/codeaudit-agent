"""webapi SQLite 访问层。"""

from __future__ import annotations

import sqlite3


def connect(path: str) -> sqlite3.Connection:
    """建立连接并打开外键约束。"""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def find_board(conn: sqlite3.Connection, board_id: str) -> tuple | None:
    """按 id 查询看板（外部输入直接进入查询语句）。"""
    row = conn.execute("SELECT * FROM boards WHERE id = '" + board_id + "'").fetchone()
    return row
