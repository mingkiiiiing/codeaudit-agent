"""数学/文本工具：可变默认参数、== None、循环内字符串拼接。"""


def accumulate(items, bucket=[]):  # line 4: 可变默认参数
    for i in items:
        bucket.append(i * 2)
    return bucket


def compare(a, b):
    if a == None:  # line 11: 应使用 is None
        return False
    return a == b


def build_page(rows):
    html = ""
    for r in rows:  # line 18: 循环内 += 拼接，应使用 join
        html = html + "<li>" + str(r) + "</li>"
    return html


def magic_discount(price):
    if price > 10000:
        return price * 0.85  # line 25: 魔法数字
    return price
