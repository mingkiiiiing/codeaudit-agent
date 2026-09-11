"""shopcore 领域模型：商品、购物车与订单。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class OrderStatus(Enum):
    """订单状态机。"""

    DRAFT = "draft"
    PAID = "paid"
    SHIPPED = "shipped"
    CANCELLED = "cancelled"


@dataclass
class Product:
    """在售商品。"""

    sku: str
    title: str
    unit_price: float
    stock: int = 0


@dataclass
class CartItem:
    """购物车条目。"""

    product: Product
    quantity: int

    @property
    def subtotal(self) -> float:
        """小计金额。"""
        return round(self.product.unit_price * self.quantity, 2)


@dataclass
class Order:
    """订单聚合根。"""

    order_id: str
    items: list[CartItem] = field(default_factory=list)
    status: OrderStatus = OrderStatus.DRAFT

    @property
    def total(self) -> float:
        """订单总金额。"""
        return round(sum(item.subtotal for item in self.items), 2)

    def add_item(self, item: CartItem) -> None:
        """追加一条购物车条目。"""
        self.items.append(item)
