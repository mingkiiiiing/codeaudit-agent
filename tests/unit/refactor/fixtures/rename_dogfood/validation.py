"""校验模块（rename dogfood 语料）。

与 models.py 同名的 Validator 类：W29 P0-2 作用域增强歧义语料——
cross_check 中的 models.Validator 属性链引用（接收者类型不可静态判定）
作用域归属不明，重命名 Validator 整体拒绝（宁拒不改，报错含位置明细）。
同时跨文件引用 service.apply_discount / OrderService.checkout，覆盖
"被调用方在另一个文件定义"的引用更新路径。
"""

from service import OrderService, apply_discount


class Validator:
    """validation 侧校验器：与 models.Validator 同名不同文件（跨文件同名类语料）。"""

    def check(self, value):
        return bool(value)


def validate_amount(amount):
    """金额校验：跨文件调用 apply_discount。"""
    ok = Validator().check(amount) and amount > 0
    return apply_discount(amount, 10) if ok else 0


def cross_check(amount):
    """双校验器交叉验证：本模块与 models 侧 Validator 结论必须一致。

    W29 P0-2 歧义语料：models.Validator 是属性链引用（接收者类型不可
    静态判定），使重命名 Validator 的计划整体拒绝（宁拒不改）。
    """
    import models

    return Validator().check(amount) == models.Validator().check(amount)


def run_once(owner):
    """驱动样例：跨文件调用 OrderService.checkout（方法属性调用）。"""
    svc = OrderService()
    svc.add("SKU-1", 2, 3.5)
    return svc.checkout(owner)
