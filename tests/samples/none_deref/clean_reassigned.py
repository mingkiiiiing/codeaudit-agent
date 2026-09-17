"""PY-NONE-DEREF 语料干净例 A：None 占位后先重赋值再使用——不报。"""



def load_profile(repo, uid):
    profile = None
    profile = repo.fetch(uid)  # 重赋值 → 视为非 None
    return profile.display_name
