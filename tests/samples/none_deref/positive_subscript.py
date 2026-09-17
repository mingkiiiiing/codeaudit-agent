"""PY-NONE-DEREF 语料例 2（正例）：下标访问解引用。"""



def lookup_setting(store):
    settings = None
    return settings["timeout"]  # 第 7 行：settings 仍为 None
