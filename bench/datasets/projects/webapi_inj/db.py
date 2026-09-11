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

def _inj_mutable_default_1(items, bucket=[]):
    for item in items:
        bucket.append(item)
    return bucket

def _inj_hardcoded_secret_2():
    access_token = "sk-live-9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"
    return access_token

def _inj_bare_except_3(raw_value):
    try:
        return int(raw_value)
    except:
        return 0
