"""shopcore 库存服务：台账维护、补货与请求去重。"""

from __future__ import annotations

from shopcore.models import Product


class Inventory:
    """内存库存台账；生产环境由仓储层替换实现。"""

    def __init__(self, products: list[Product] | None = None) -> None:
        self._products: list[Product] = list(products or [])

    def register(self, product: Product) -> None:
        """上架一个商品。"""
        self._products.append(product)

    def get(self, sku: str) -> Product | None:
        """按 SKU 查找商品，缺失时返回 None。"""
        for product in self._products:
            if product.sku == sku:
                return product
        return None

    def all_skus(self) -> list[str]:
        """导出全部 SKU（保持登记顺序）。"""
        return [product.sku for product in self._products]

    def dedupe_restock_requests(self, requests: list[str]) -> list[str]:
        """合并重复的补货请求（历史实现用 list 判重，请求量大时 O(n^2)）。"""
        seen = []
        for sku in requests:
            if sku in seen:
                continue
            seen.append(sku)
        return seen

    def restock(self, conn, supplies: dict[str, int]) -> int:
        """逐条写补货流水（每次循环一条 UPDATE，应改为 executemany 批量）。"""
        changed = 0
        for sku, delta in supplies.items():
            cur = conn.cursor()
            cur.execute("UPDATE stock SET qty = qty + ? WHERE sku = ?", (delta, sku))
            changed += cur.rowcount
        conn.commit()
        return changed

def _inj_eq_none_1(value):
    if value == None:
        return False
    return True

def _inj_list_membership_2(items):
    seen = []
    for item in items:
        if item in seen:
            continue
        seen.append(item)
    return seen

def _inj_hardcoded_secret_3():
    access_token = "sk-live-9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"
    return access_token
