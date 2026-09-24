"""订单服务：调用方语料（跨文件 import 引用 util / models 的函数、类与常量）。"""

from models import TAX_RATE, OrderLine, UserRecord
from util import MAX_RETRY, build_query, calc_total, format_label, join_names

SERVICE_NAME = "order-service"  # 注释陷阱：重命名 SERVICE_NAME 后本注释应保留旧名


def audit_log(event, payload):
    """审计日志（docstring 陷阱：重命名 audit_log 后 docstring 应保留旧名）。"""
    line = join_names([format_label(event, width=24), str(payload)], sep=" | ")
    return line


def notify_user(user, message):
    """通知用户。

    子串陷阱：username / user 是不同标识符；重命名 notify_user 时
    username 必须原样保留（token 级替换的回归语料）。
    """
    username = getattr(user, "display", user)
    return f"to {username}: {message}"


def resolve_customer(record):
    """解析客户名。"""
    return record.display.strip()


def apply_discount(amount, percent):
    """打折。被 validation.py 跨文件 import 引用。"""
    return amount * (1 - percent / 100)


class OrderService:
    """订单服务（docstring 陷阱：重命名 OrderService 后 docstring 应保留旧名）。"""

    def __init__(self, retries=MAX_RETRY):
        self.retries = retries
        self.lines = []

    def add(self, sku, qty, price):
        self.lines.append(OrderLine(sku, qty, price))
        return len(self.lines)

    def checkout(self, owner):
        """结账：跨文件调用 UserRecord.to_display / calc_total。

        字符串陷阱：下方 audit_log 的字面量 "checkout" 在方法重命名后应保留。
        """
        record = UserRecord(1, owner)
        total = calc_total([line.net_amount() for line in self.lines])
        gross = total * (1 + TAX_RATE)
        context = build_query(owner, lines=len(self.lines))
        receipt = format_label(record.to_display(), width=32)
        audit_log("checkout", context)
        notify_user(record, receipt)
        return resolve_customer(record), gross
