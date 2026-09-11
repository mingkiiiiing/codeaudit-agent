"""订单服务：故意埋入空指针、SQL 拼接、资源泄漏、裸 except、低效成员判断。"""

import sqlite3

from app.services.users import get_user


def get_order_summary(order_id, conn):
    user = get_user(order_id)
    name = user["name"]  # line 10: get_user 可能返回 None，未判空
    query = "SELECT * FROM orders WHERE id = " + order_id  # line 11: SQL 拼接
    cur = conn.execute(query)
    return name, cur.fetchall()


def load_totals(paths):
    totals = []
    seen = []
    for p in paths:
        if p in seen:  # line 20: list 做 in 成员判断，应使用 set
            continue
        seen.append(p)
        f = open(p)  # line 23: 打开文件未关闭
        totals.append(len(f.read()))
    return totals


def safe_delete(path):
    try:
        import os

        os.remove(path)
    except:  # line 33: 裸 except 吞掉所有异常
        pass
    return True


def create_order(conn, user_id, amount):
    conn.execute(
        "INSERT INTO orders (user_id, amount) VALUES (?, ?)",
        (user_id, amount),
    )
    conn.commit()
