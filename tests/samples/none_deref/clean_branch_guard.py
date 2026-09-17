"""PY-NONE-DEREF 语料干净例 C：分支内判空重赋值后再使用——不报（线性序保守口径）。"""



def resolve(repo, key):
    value = None
    if key in repo:
        value = repo[key]
    return value.name  # 线性序：value 已被分支内重赋值 → 保守不报
