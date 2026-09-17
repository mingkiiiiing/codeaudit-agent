"""PY-NONE-DEREF 语料例 5（正例）：嵌套块（try/except）内的下标解引用。"""



def parse_row(row, index):
    cache = None
    try:
        value = cache[index]  # 第 8 行：cache 仍为 None
    except LookupError:
        value = ""
    return value
