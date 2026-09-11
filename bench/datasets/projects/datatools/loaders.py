"""datatools 输出加载器：写 CSV 与入库。"""

from __future__ import annotations

import sqlite3


def write_csv_rows(path: str, rows: list[dict[str, object]]) -> None:
    """把行写为 CSV（首行为表头）。"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(",".join(rows[0].keys()) + "\n")
        for row in rows:
            fh.write(",".join(str(v) for v in row.values()) + "\n")


def dynamic_insert(conn: sqlite3.Connection, table: str, row: dict[str, object]) -> None:
    """按表名动态拼接 INSERT（表名来自外部配置，直接进入语句）。"""
    sql = "INSERT INTO " + table + " VALUES (?, ?)"
    conn.execute(sql, (row["id"], row["amount"]))


def load_fact_rows(conn: sqlite3.Connection, rows: list[dict[str, object]]) -> int:
    """循环逐条写入事实表（应改 executemany 批量）。"""
    written = 0
    for row in rows:
        cur = conn.cursor()
        cur.execute("INSERT INTO facts VALUES (?, ?)", (row["id"], row["amount"]))
        written += cur.rowcount
    conn.commit()
    return written
