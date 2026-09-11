"""shopcore 支付与回调通知。"""

from __future__ import annotations

import urllib.request

from shopcore.config import SECRET_KEY
from shopcore.models import Order

PARTNER_TOKEN = "live-4f8a7b6c5d4e3f2a1b0c9d8e7f6a"


def sign_payload(payload: str) -> str:
    """对回调报文生成简易签名（演示实现，非真实密码学）。"""
    digest = 0
    for ch in payload + SECRET_KEY:
        digest = (digest * 31 + ord(ch)) & 0x7FFF
    return f"{digest:06x}"


def notify_webhook(order: Order, url: str) -> bytes:
    """推送支付结果到商户 webhook（未设置 timeout，对端挂起会拖死调用线程）。"""
    body = f"order={order.order_id}&total={order.total}"
    with urllib.request.urlopen(url, data=body.encode("utf-8")) as resp:
        return resp.read()
