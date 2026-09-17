"""W16 静态规则补缺：PY-NAMING-STYLE / PY-PINYIN-NAMING 正反用例 + 红线反例。

红线约定：clean_corpus（D:/acc_tmp/corpus/clean_corpus）的全部惯用写法对
新规则必须 0 命中——clean 语料片段直接拷入用例（summarize/render_report/
known/total 等正常词必须被白名单盖住）。
"""

from __future__ import annotations

from audit.detect.rules.py_naming import (
    PyNamingStyleRule,
    PyPinyinNamingRule,
    build_py_naming_rules,
)


def _lines(rule, ctx):
    return [h.line_start for h in rule.check(ctx)]


# clean 语料（pkg/orders.py / pkg/repo.py）摘录，逐字拷入作红线反例
CLEAN_PY_EXCERPT = (
    "def summarize(orders):\n"
    "    known = {o.order_id for o in orders}\n"
    "    total = sum(o.amount for o in orders if o.order_id in known)\n"
    "    logger.info('summarized orders total=%s count=%s', total, len(orders))\n"
    "    return {'total': total, 'count': len(orders)}\n"
    "\n"
    "def render_report(orders):\n"
    "    lines = [str(o) for o in orders]\n"
    "    return '\\n'.join(lines)\n"
    "\n"
    "def join_names(names):\n"
    "    return ', '.join(names)\n"
)


class TestPyNamingStyle:
    rule = PyNamingStyleRule()

    def test_positive_func_contains_upper(self, make_ctx):
        ctx = make_ctx("def GetUser(id):\n    return id\n")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_async_func_and_lower_class(self, make_ctx):
        ctx = make_ctx(
            "async def fetchUrl(url):\n"
            "    pass\n"
            "\n"
            "class order_service:\n"
            "    pass\n"
        )
        assert sorted(_lines(self.rule, ctx)) == [1, 4]

    def test_negative_snake_func_pascal_class(self, make_ctx):
        ctx = make_ctx(
            "def get_user(id):\n"
            "    return id\n"
            "\n"
            "class UserService:\n"
            "    pass\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_dunder_exempt(self, make_ctx):
        ctx = make_ctx("class Order:\n    def __init__(self):\n        pass\n")
        assert self.rule.check(ctx) == []

    def test_negative_test_file_exempt(self, make_ctx):
        ctx = make_ctx("def GetUser(id):\n    return id\n", rel_path="tests/unit/test_x.py")
        assert self.rule.check(ctx) == []

    def test_negative_clean_corpus_excerpt(self, make_ctx):
        # 红线：clean 语料的函数/类命名全部合规 → 0 命中
        ctx = make_ctx(CLEAN_PY_EXCERPT)
        assert self.rule.check(ctx) == []


class TestPyPinyinNaming:
    rule = PyPinyinNamingRule()

    def test_positive_def_pinyin(self, make_ctx):
        ctx = make_ctx("def jisuan_heji(shuju):\n    return shuju\n")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_assign_pinyin(self, make_ctx):
        ctx = make_ctx("jieguo = jisuan(shuju)\n")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_annotated_assign_pinyin(self, make_ctx):
        ctx = make_ctx("shuju: list = load()\n")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_english_words_whitelisted(self, make_ctx):
        # result/data/user/total 等英文词必须被白名单盖住
        ctx = make_ctx("total = sum(values)\nuser_name = load_data()\n")
        assert self.rule.check(ctx) == []

    def test_negative_single_syllable_token(self, make_ctx):
        # 单音节（jia/ma/shi）刻意不报：≥2 音节才命中（保守口径）
        ctx = make_ctx("def jia(a, b):\n    return a + b\n")
        assert self.rule.check(ctx) == []

    def test_negative_camel_pinyin_single_syllable_tokens(self, make_ctx):
        # 驼峰分词后每段均单音节（jieGuo → jie/guo）不报：整词口径
        ctx = make_ctx("jieGuo = 1\n")
        assert self.rule.check(ctx) == []

    def test_negative_test_file_exempt(self, make_ctx):
        ctx = make_ctx("jieguo = jisuan(shuju)\n", rel_path="tests/test_a.py")
        assert self.rule.check(ctx) == []

    def test_negative_clean_corpus_excerpt(self, make_ctx):
        # 红线：clean 语料全部标识符（summarize/known/total/render_report/…）0 命中
        ctx = make_ctx(CLEAN_PY_EXCERPT)
        assert self.rule.check(ctx) == []


def test_build_py_naming_rules():
    rules = build_py_naming_rules()
    assert [r.id for r in rules] == ["PY-NAMING-STYLE", "PY-PINYIN-NAMING"]
    assert all(r.category.value == "style" and r.severity.value == "low" for r in rules)
    assert all(r.languages == ("python",) for r in rules)
