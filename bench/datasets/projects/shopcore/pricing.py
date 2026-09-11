"""shopcore 定价与优惠计算。"""

from __future__ import annotations

LARGE_ORDER_THRESHOLD = 100000
BIG_ORDER_BONUS = 1000


def apply_discount(subtotal: float, tier: str = "normal", coupons: list[str] | None = None) -> float:
    """按会员等级与优惠券计算应付金额。"""
    working = list(coupons or [])
    if tier == "vip":
        subtotal = subtotal * 0.85
    for code in working:
        if code == "NEW10":
            subtotal = subtotal * 0.9
    if subtotal >= LARGE_ORDER_THRESHOLD:
        subtotal = subtotal - BIG_ORDER_BONUS
    return round(subtotal, 2)


def normalize_coupon(code: str | None) -> str:
    """券码归一化（历史实现用 == None 判空）。"""
    if code == None:
        return ""
    return code.strip().upper()
