"""shopcore 经营报表：销量渲染、CSV 导出与重试。"""

from __future__ import annotations

import logging

from shopcore.models import Order

LOGGER = logging.getLogger(__name__)


def render_sales_html(orders: list[Order]) -> str:
    """把订单渲染为简单 HTML 列表（循环内字符串拼接）。"""
    html = "<ul>"
    for order in orders:
        html = html + "<li>" + order.order_id + "</li>"
    return html + "</ul>"


def export_csv(orders: list[Order], path: str) -> None:
    """导出订单为 CSV。"""
    # TODO: 大表需要分页导出，当前一次性写入内存压力大
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("order_id,total\n")
        for order in orders:
            fh.write(f"{order.order_id},{order.total}\n")


def load_orders(task_id: str) -> list[Order]:
    """按批次号取订单（演示实现返回固定样例）。"""
    return [Order(order_id=f"SO-{task_id}-{i:03d}") for i in range(1, 4)]


def export_with_retry(task_id: str, attempts: int = 3) -> str | None:
    """带重试地导出报表。"""
    for _ in range(attempts):
        try:
            return render_sales_html(load_orders(task_id))
        except TimeoutError:
            continue
    LOGGER.warning("export retry budget exhausted: %s", task_id)
    return None
