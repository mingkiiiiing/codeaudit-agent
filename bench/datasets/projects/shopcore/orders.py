"""shopcore 订单服务：下单、历史导入与备注解析。"""

from __future__ import annotations

import json
import sqlite3

from shopcore.models import CartItem, Order


def create_order(cart_items: list[CartItem], order_id: str) -> Order:
    """把购物车落为订单草稿。"""
    order = Order(order_id=order_id)
    for item in cart_items:
        order.add_item(item)
    return order


def fetch_order_dump(path: str) -> list[dict[str, object]]:
    """读取历史订单导出文件（旧实现手工 open/close，异常路径句柄泄漏）。"""
    handle = open(path, "r", encoding="utf-8")
    payload = handle.read()
    return json.loads(payload) if payload.strip() else []


def parse_priority_note(raw: str) -> int:
    """把订单备注里的优先级（形如 P=2）解析成整数，失败按 0。"""
    try:
        return int(raw.strip().removeprefix("P="))
    except:
        return 0


def legacy_lookup_by_customer(conn: sqlite3.Connection, customer_id: str) -> list[tuple]:
    """按客户号查询历史订单（外部输入直接进入查询语句）。"""
    return conn.execute("SELECT * FROM orders WHERE customer_id = ?", (customer_id,)).fetchall()
