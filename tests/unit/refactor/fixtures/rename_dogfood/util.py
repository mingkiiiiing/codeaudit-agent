"""工具函数与常量（rename dogfood 语料）。

故意埋入旧名陷阱：docstring/注释/子串标识符里的同名文本，用于验证
token 级替换不越界。语料口径与 audit/refactor/rename.py 的 MVP 约定一致
（无同名冲突、无动态反射）。
"""

MAX_RETRY = 3  # 注释陷阱：重命名 MAX_RETRY 后本注释应保留旧名
DEFAULT_TIMEOUT = 30

# 子串陷阱：MAX_RETRY_LIMIT 是独立符号，重命名 MAX_RETRY 时不得被误伤
MAX_RETRY_LIMIT = 10


def calc_total(items):
    """calc_total 汇总金额（docstring 陷阱：重命名后 docstring 应保留旧名）。"""
    total = 0
    for item in items:
        total += item
    return total


def format_label(name, width=10):
    """左对齐补空格。"""
    return name.ljust(width)


def build_query(base, **extra):
    """组装查询参数。"""
    params = dict(extra)
    params["q"] = base
    return params


def parse_int(raw, default=0):
    """安全转 int。"""
    try:
        return int(raw)
    except ValueError:
        return default


def coerce_qty(raw):
    """数量清洗（同文件调用 parse_int，构成同文件引用点）。"""
    return parse_int(raw, default=1)


def clamp_value(value, low, high):
    """区间截断。"""
    if value < low:
        return low
    if value > high:
        return high
    return value


def join_names(parts, sep=","):
    """拼接名称。"""
    return sep.join(parts)


def _shrink(text, limit=None):
    """内部工具：超长截断（引用 DEFAULT_TIMEOUT / clamp_value，构成同文件引用）。"""
    cap = clamp_value(limit if limit is not None else DEFAULT_TIMEOUT, 0, 1024)
    return text[:cap]
