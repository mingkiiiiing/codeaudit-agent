"""PY-NONE-DEREF 语料例 1（正例）：属性访问解引用。

变量在同函数内被赋 None 后未重赋值即做属性访问——运行时 AttributeError。
"""



def render_template(user):
    profile = None
    return profile.display_name  # 第 10 行：profile 仍为 None
