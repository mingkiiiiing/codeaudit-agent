"""mini_app 演示入口：初始化演示库并查询用户。"""

import sqlite3

from store import find_user
from textutil import truncate_text


def open_db(path=":memory:"):
    """创建带 users 表的演示数据库连接。"""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS users (name TEXT PRIMARY KEY, role TEXT)")
    conn.commit()
    return conn


def main():
    conn = open_db()
    conn.execute("INSERT OR IGNORE INTO users VALUES ('alice', 'admin')")
    conn.commit()
    return find_user(conn, "alice")


if __name__ == "__main__":
    main()
