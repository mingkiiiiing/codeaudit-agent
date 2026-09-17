"""PY-NONE-DEREF 语料例 3（正例）：调用解引用（None() 抛 TypeError）。"""



def dispatch(name):
    handler = None
    if name == "run":
        handler()  # 第 8 行：handler 仍为 None
    return name
