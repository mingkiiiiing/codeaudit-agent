"""领域模型（rename dogfood 语料）。

含与 validation.py 同名的 Validator 类：W29 P0-2 作用域增强歧义语料——
validation.cross_check 中存在 models.Validator 属性链引用（接收者类型
不可静态判定），重命名 Validator 的计划整体拒绝（宁拒不改，报错含
位置明细）。
"""

TAX_RATE = 0.13  # 税率常量，service.py 跨文件引用


class UserRecord:
    """用户记录（docstring 陷阱：重命名 UserRecord 后 docstring 应保留旧名）。"""

    def __init__(self, uid, display):
        self.uid = uid
        self.display = display

    def to_display(self):
        """渲染展示名（service.py 以属性调用方式跨文件引用）。"""
        return f"[{self.uid}] {self.display}"


class OrderLine:
    """订单行。"""

    def __init__(self, sku, qty, price):
        self.sku = sku
        self.qty = qty
        self.price = price

    def net_amount(self):
        """净额 = 数量 * 单价（税率在 service 层叠加）。"""
        return self.qty * self.price


class Validator:
    """models 侧校验器：与 validation.Validator 同名不同文件（跨文件同名类语料）。"""

    def check(self, value):
        return value is not None
