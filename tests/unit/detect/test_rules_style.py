"""style 类规则正反用例（6 条）。"""

from __future__ import annotations

from audit.detect.rules.python import (
    DeepNestingRule,
    HardcodedUrlRule,
    LongFunctionRule,
    MagicNumberRule,
    PrintDebugRule,
    TodoFixmeCommentRule,
)


def _lines(rule, ctx):
    return [h.line_start for h in rule.check(ctx)]


class TestLongFunction:
    rule = LongFunctionRule()

    def test_positive_over_80_lines(self, make_ctx):
        body = "\n".join(f"    x{i} = {i}" for i in range(85))
        ctx = make_ctx(f"def big():\n{body}\n")
        hits = self.rule.check(ctx)
        assert len(hits) == 1
        assert hits[0].line_start == 1
        assert hits[0].line_end == 86

    def test_negative_short_function(self, make_ctx):
        ctx = make_ctx("def small():\n    return 1\n")
        assert self.rule.check(ctx) == []


class TestDeepNesting:
    rule = DeepNestingRule()

    def test_positive_four_levels(self, make_ctx):
        ctx = make_ctx(
            "def f(items):\n"
            "    for a in items:\n"
            "        if a:\n"
            "            for b in a:\n"
            "                if b:\n"
            "                    handle(b)\n"
        )
        # 第 5/6 行缩进 16/20 空格，达到 4 层；连续深行合并为一条命中
        hits = self.rule.check(ctx)
        assert len(hits) == 1
        assert (hits[0].line_start, hits[0].line_end) == (5, 6)

    def test_positive_two_separate_regions(self, make_ctx):
        ctx = make_ctx(
            "def f(items, flag):\n"
            "    for a in items:\n"
            "        if a:\n"
            "            for b in a:\n"
            "                if b:\n"
            "                    handle(b)\n"
            "    if flag:\n"
            "        for a in items:\n"
            "            if a:\n"
            "                for b in a:\n"
            "                    if b:\n"
            "                        handle(b)\n"
        )
        # 第 10 行缩进已 16 空格（4 层），第二区域为 10~12 行
        assert [(h.line_start, h.line_end) for h in self.rule.check(ctx)] == [(5, 6), (10, 12)]

    def test_negative_three_levels(self, make_ctx):
        ctx = make_ctx(
            "def f(items):\n"
            "    for a in items:\n"
            "        if a:\n"
            "            handle(a)\n"
        )
        assert self.rule.check(ctx) == []


class TestMagicNumber:
    rule = MagicNumberRule()

    def test_positive_comparison(self, make_ctx):
        ctx = make_ctx("def f(price):\n    if price > 10000:\n        return price\n    return 0\n")
        assert _lines(self.rule, ctx) == [2]

    def test_positive_float(self, make_ctx):
        ctx = make_ctx("x = 1_500_000\n")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_small_number(self, make_ctx):
        ctx = make_ctx("def f(price):\n    if price > 10:\n        return price\n    return 0\n")
        assert self.rule.check(ctx) == []

    def test_negative_number_in_string(self, make_ctx):
        ctx = make_ctx("hint = 'timeout 5000ms'\n")
        assert self.rule.check(ctx) == []


class TestTodoFixme:
    rule = TodoFixmeCommentRule()

    def test_positive_todo(self, make_ctx):
        ctx = make_ctx("def f():\n    pass  # TODO: 补充实现\n")
        assert _lines(self.rule, ctx) == [2]

    def test_positive_fixme_and_hack(self, make_ctx):
        ctx = make_ctx("# FIXME: 临时绕过\n# HACK: 勿动\nx = 1\n")
        assert _lines(self.rule, ctx) == [1, 2]

    def test_negative_lowercase(self, make_ctx):
        ctx = make_ctx("# todo: 小写不算标记\nx = 1\n")
        assert self.rule.check(ctx) == []

    def test_negative_word_contains_tag(self, make_ctx):
        ctx = make_ctx("# todolist 已整理\nx = 1\n")
        assert self.rule.check(ctx) == []


class TestHardcodedUrl:
    rule = HardcodedUrlRule()

    def test_positive_inline_url(self, make_ctx):
        ctx = make_ctx('def fetch():\n    return get("https://api.example.com/v1/users")\n')
        assert _lines(self.rule, ctx) == [2]

    def test_negative_upper_const(self, make_ctx):
        ctx = make_ctx('API_URL = "https://api.example.com/v1"\n\n\ndef fetch():\n    return get(API_URL)\n')
        assert self.rule.check(ctx) == []

    def test_negative_url_in_comment(self, make_ctx):
        ctx = make_ctx("# 文档见 https://example.com/docs\nx = 1\n")
        assert self.rule.check(ctx) == []


class TestPrintDebug:
    rule = PrintDebugRule()

    def test_positive(self, make_ctx):
        ctx = make_ctx("def f(x):\n    print('debug', x)\n    return x\n")
        assert _lines(self.rule, ctx) == [2]

    def test_negative_test_file(self, make_ctx):
        ctx = make_ctx("print('ok')\n", rel_path="tests/test_out.py")
        assert self.rule.check(ctx) == []

    def test_negative_logging(self, make_ctx):
        ctx = make_ctx("import logging\n\ndef f(x):\n    logging.info(x)\n")
        assert self.rule.check(ctx) == []

    def test_negative_pprint(self, make_ctx):
        ctx = make_ctx("from pprint import pprint\n\npprint(data)\n")
        assert self.rule.check(ctx) == []
