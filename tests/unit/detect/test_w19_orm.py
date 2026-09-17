"""W19-B ORM N+1 规则正反用例（PY-ORM-N-PLUS-ONE）。

直接构造 RuleContext（conftest 的 make_ctx），不依赖 registry 接线——接线由
集成人统一完成，本文件只验证规则自身行为。红线口径：`.execute()` 一律不报
（原生 SQL 归既有 IO-IN-LOOP 领地）；循环 header 行的同型链式只执行一次查询，
也不报。
"""

from __future__ import annotations

from audit.detect.rules.py_orm import OrmNPlusOneRule


def _lines(rule, ctx):
    return [h.line_start for h in rule.check(ctx)]


class TestOrmNPlusOne:
    rule = OrmNPlusOneRule()

    # ------------------------------------------------------------ 正例

    def test_positive_query_get_in_for(self, make_ctx):
        ctx = make_ctx(
            "def load(session, ids):\n"
            "    out = []\n"
            "    for uid in ids:\n"
            "        out.append(session.query(User).get(uid))\n"
            "    return out\n"
        )
        assert _lines(self.rule, ctx) == [4]

    def test_positive_query_first_in_for(self, make_ctx):
        # filter(...) 之后接 .first() 同样是链式立即执行
        ctx = make_ctx(
            "def firsts(session, ids):\n"
            "    rows = []\n"
            "    for uid in ids:\n"
            "        rows.append(session.query(User).filter(User.id == uid).first())\n"
            "    return rows\n"
        )
        assert _lines(self.rule, ctx) == [4]

    def test_positive_objects_get_in_for(self, make_ctx):
        # Django 形态：Model.objects.get(
        ctx = make_ctx(
            "def load(ids):\n"
            "    out = []\n"
            "    for uid in ids:\n"
            "        out.append(User.objects.get(pk=uid))\n"
            "    return out\n"
        )
        assert _lines(self.rule, ctx) == [4]

    def test_positive_query_all_in_while(self, make_ctx):
        ctx = make_ctx(
            "def drain(session):\n"
            "    while True:\n"
            "        rows = session.query(Task).all()\n"
            "        if not rows:\n"
            "            break\n"
        )
        assert _lines(self.rule, ctx) == [3]

    def test_positive_objects_get_in_while(self, make_ctx):
        ctx = make_ctx(
            "def poll(queue_ids):\n"
            "    while queue_ids:\n"
            "        job = Job.objects.get(id=queue_ids.pop())\n"
            "        run(job)\n"
        )
        assert _lines(self.rule, ctx) == [3]

    def test_positive_message_mentions_n_plus_one(self, make_ctx):
        ctx = make_ctx(
            "for uid in ids:\n"
            "    session.query(User).get(uid)\n"
        )
        hit = self.rule.check(ctx)[0]
        assert "N+1" in hit.message
        assert hit.meta["pattern"] == "session.query(...).get(/.first(/.all("

    # ------------------------------------------------------------ 反例

    def test_negative_query_outside_loop(self, make_ctx):
        # 对照：循环外同型调用只执行一次，不构成 N+1
        ctx = make_ctx(
            "def load(session, uid):\n"
            "    return session.query(User).get(uid)\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_execute_in_loop(self, make_ctx):
        # 红线口径：.execute() 是原生 SQL 游标，归既有 IO-IN-LOOP 领地，绝不报
        ctx = make_ctx(
            "def query_each(cur, ids):\n"
            "    rows = []\n"
            "    for i in ids:\n"
            "        rows.append(cur.execute('SELECT 1'))\n"
            "    return rows\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_query_in_loop_header(self, make_ctx):
        # header 行的 query(...).all() 只执行一次查询，报了就是误报
        ctx = make_ctx(
            "def all_users(session):\n"
            "    for u in session.query(User).all():\n"
            "        yield u\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_dict_get_in_loop(self, make_ctx):
        # 非 ORM 的 .get(（dict 取值）既无 .query( 也不是 .objects.get(，不报
        ctx = make_ctx(
            "def pick(cfg, keys):\n"
            "    out = []\n"
            "    for k in keys:\n"
            "        out.append(cfg.get(k))\n"
            "    return out\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_objects_filter_in_loop(self, make_ctx):
        # Django 的 .objects.filter( 返回惰性 queryset，循环内只构建不执行
        ctx = make_ctx(
            "def collect(ids):\n"
            "    out = []\n"
            "    for uid in ids:\n"
            "        out.append(User.objects.filter(id=uid))\n"
            "    return out\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_query_get_in_string(self, make_ctx):
        # 字符串与注释里的同形文本不报（掩码扫描）
        ctx = make_ctx(
            "def doc():\n"
            "    for uid in ids:\n"
            "        sql = 'session.query(User).get(uid)'\n"
        )
        assert self.rule.check(ctx) == []


if __name__ == "__main__":  # pragma: no cover
    import pytest

    pytest.main([__file__])
