"""shopcore 演示入口：组装各服务并跑通下单流程。"""

from __future__ import annotations

from shopcore.inventory import Inventory
from shopcore.models import CartItem, Product
from shopcore.orders import create_order
from shopcore.pricing import apply_discount


def build_catalog() -> Inventory:
    """装配演示商品目录。"""
    catalog = Inventory()
    catalog.register(Product(sku="SKU-001", title="机械键盘", unit_price=399.0, stock=20))
    catalog.register(Product(sku="SKU-002", title="无线鼠标", unit_price=129.0, stock=55))
    catalog.register(Product(sku="SKU-003", title="显示器支架", unit_price=199.0, stock=12))
    return catalog


def main() -> None:
    """跑通一次加购与计价。"""
    catalog = build_catalog()
    cart = [
        CartItem(product=catalog.get("SKU-001"), quantity=1),
        CartItem(product=catalog.get("SKU-002"), quantity=2),
    ]
    order = create_order(cart, order_id="SO-2026-0001")
    total = apply_discount(order.total, tier="vip")
    print(f"[demo] 订单 {order.order_id} 应付：{total}")


if __name__ == "__main__":
    main()

def _inj_eq_none_1(value):
    if value == None:
        return False
    return True

def _inj_bare_except_2(raw_value):
    try:
        return int(raw_value)
    except:
        return 0

def _inj_sql_concat_3(conn, user_id):
    query = "SELECT * FROM users WHERE id = " + user_id
    return conn.execute(query)
