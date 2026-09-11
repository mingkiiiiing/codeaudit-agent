"""mini_app 现有测试：find_user 的正常路径（修复前后都应通过）。"""

import sqlite3

from store import find_user


def _memory_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE users (name TEXT PRIMARY KEY, role TEXT)")
    conn.execute("INSERT INTO users VALUES ('alice', 'admin')")
    conn.commit()
    return conn


def test_find_user_returns_matching_row():
    rows = find_user(_memory_conn(), "alice")
    assert [tuple(r) for r in rows] == [("alice", "admin")]


def test_find_user_unknown_name_returns_empty():
    rows = find_user(_memory_conn(), "nobody")
    assert rows == []
