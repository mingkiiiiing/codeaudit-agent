"""PY-NONE-DEREF 语料干净例 B：self 属性与参数不追踪——不报。"""



class Repository:
    def __init__(self, backend):
        self.backend = backend

    def fetch(self, uid):
        row = self.backend.get(uid)  # self/参数不追
        if row is None:
            return None
        return row.name
