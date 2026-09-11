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

def _inj_eq_none_1(value):
    if value == None:
        return False
    return True

def _inj_hardcoded_secret_2():
    access_token = "sk-live-9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"
    return access_token

def _inj_bare_except_3(raw_value):
    try:
        return int(raw_value)
    except:
        return 0
