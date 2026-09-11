"""bug 类规则正反用例（10 条）。"""

from __future__ import annotations

from audit.detect.rules.python import (
    AssertInProductionRule,
    BareExceptRule,
    EmptyExceptContextRule,
    EqNoneRule,
    ExceptPassRule,
    IsLiteralComparisonRule,
    MutableDefaultArgRule,
    OpenNoCloseRule,
    ShadowBuiltinRule,
    UnreachableCodeRule,
)


def _lines(rule, ctx):
    return [h.line_start for h in rule.check(ctx)]


class TestBareExcept:
    rule = BareExceptRule()

    def test_positive(self, make_ctx):
        ctx = make_ctx("try:\n    run()\nexcept:\n    pass\n")
        assert _lines(self.rule, ctx) == [3]

    def test_negative_typed_except(self, make_ctx):
        ctx = make_ctx("try:\n    run()\nexcept ValueError:\n    pass\n")
        assert self.rule.check(ctx) == []

    def test_negative_in_string(self, make_ctx):
        ctx = make_ctx('doc = """\nexcept:\n"""\n')
        assert self.rule.check(ctx) == []


class TestExceptPass:
    rule = ExceptPassRule()

    def test_positive_typed(self, make_ctx):
        ctx = make_ctx("try:\n    run()\nexcept ValueError:\n    pass\n")
        assert _lines(self.rule, ctx) == [3]

    def test_negative_logging_body(self, make_ctx):
        ctx = make_ctx(
            "import logging\n"
            "try:\n"
            "    run()\n"
            "except ValueError:\n"
            "    logging.exception('failed')\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_bare_except_reported_elsewhere(self, make_ctx):
        # 裸 except 由 PY-BARE-EXCEPT 负责，本规则不重复报告
        ctx = make_ctx("try:\n    run()\nexcept:\n    pass\n")
        assert self.rule.check(ctx) == []


class TestEmptyExceptContext:
    rule = EmptyExceptContextRule()

    def test_positive_ellipsis(self, make_ctx):
        ctx = make_ctx("try:\n    run()\nexcept Exception:\n    ...\n")
        assert _lines(self.rule, ctx) == [3]

    def test_positive_docstring_body(self, make_ctx):
        ctx = make_ctx('try:\n    run()\nexcept Exception:\n    """待实现"""\n')
        assert _lines(self.rule, ctx) == [3]

    def test_negative_real_body(self, make_ctx):
        ctx = make_ctx("try:\n    run()\nexcept Exception:\n    recover()\n")
        assert self.rule.check(ctx) == []


class TestMutableDefaultArg:
    rule = MutableDefaultArgRule()

    def test_positive_list(self, make_ctx):
        ctx = make_ctx("def accumulate(items, bucket=[]):\n    return bucket\n")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_dict_and_set(self, make_ctx):
        ctx = make_ctx("def f(a={}, b=set()):\n    return a, b\n")
        assert len(self.rule.check(ctx)) == 2

    def test_positive_multiline_def(self, make_ctx):
        ctx = make_ctx("def f(\n    a,\n    bucket=[],\n):\n    return bucket\n")
        assert _lines(self.rule, ctx) == [1]

    def test_mixed_none_and_mutable(self, make_ctx):
        # None 默认值不报，同函数里可变默认值仍要报
        ctx = make_ctx("def g(x=None, cfg={}):\n    return x\n")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_none_default_ok(self, make_ctx):
        ctx = make_ctx("def g(x=None):\n    return x\n")
        assert self.rule.check(ctx) == []

    def test_negative_plain_assignment(self, make_ctx):
        ctx = make_ctx("data = []\n")
        assert self.rule.check(ctx) == []


class TestEqNone:
    rule = EqNoneRule()

    def test_positive_eq(self, make_ctx):
        ctx = make_ctx("if a == None:\n    pass\n")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_ne(self, make_ctx):
        ctx = make_ctx("while b != None:\n    break\n")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_is_none(self, make_ctx):
        ctx = make_ctx("if a is None:\n    pass\n")
        assert self.rule.check(ctx) == []

    def test_negative_in_string(self, make_ctx):
        ctx = make_ctx("msg = 'a == None'\n")
        assert self.rule.check(ctx) == []


class TestOpenNoClose:
    rule = OpenNoCloseRule()

    def test_positive_leak(self, make_ctx):
        ctx = make_ctx(
            "def rd(p):\n"
            "    f = open(p)\n"
            "    return f.read()\n"
        )
        assert _lines(self.rule, ctx) == [2]

    def test_negative_explicit_close(self, make_ctx):
        ctx = make_ctx(
            "def rd(p):\n"
            "    f = open(p)\n"
            "    try:\n"
            "        return f.read()\n"
            "    finally:\n"
            "        f.close()\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_with_statement(self, make_ctx):
        ctx = make_ctx("def rd(p):\n    with open(p) as f:\n        return f.read()\n")
        assert self.rule.check(ctx) == []


class TestUnreachableCode:
    rule = UnreachableCodeRule()

    def test_positive_after_return(self, make_ctx):
        ctx = make_ctx("def f(x):\n    return x\n    print(x)\n")
        assert _lines(self.rule, ctx) == [3]

    def test_positive_after_raise(self, make_ctx):
        ctx = make_ctx("def f(x):\n    raise ValueError(x)\n    return x\n")
        assert _lines(self.rule, ctx) == [3]

    def test_negative_reachable_branch(self, make_ctx):
        ctx = make_ctx("def f(x):\n    if x:\n        return 1\n    return 2\n")
        assert self.rule.check(ctx) == []

    def test_negative_else_after_return(self, make_ctx):
        ctx = make_ctx("def f(x):\n    if x:\n        return 1\n    else:\n        return 2\n")
        assert self.rule.check(ctx) == []


class TestShadowBuiltin:
    rule = ShadowBuiltinRule()

    def test_positive_assignment(self, make_ctx):
        ctx = make_ctx("list = [1, 2]\nprint(list)\n")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_for_target(self, make_ctx):
        ctx = make_ctx("for str in items:\n    break\n")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_normal_name(self, make_ctx):
        ctx = make_ctx("items = [1, 2]\n")
        assert self.rule.check(ctx) == []

    def test_negative_equality_not_assignment(self, make_ctx):
        ctx = make_ctx("if dict == other:\n    pass\n")
        assert self.rule.check(ctx) == []


class TestAssertInProduction:
    rule = AssertInProductionRule()

    def test_positive_prod_file(self, make_ctx):
        ctx = make_ctx("def f(x):\n    assert x > 0\n    return x\n")
        assert _lines(self.rule, ctx) == [2]

    def test_negative_test_file(self, make_ctx):
        ctx = make_ctx("def test_f(x):\n    assert x > 0\n", rel_path="tests/test_a.py")
        assert self.rule.check(ctx) == []

    def test_negative_no_assert(self, make_ctx):
        ctx = make_ctx("def f(x):\n    if x <= 0:\n        raise ValueError('bad')\n")
        assert self.rule.check(ctx) == []


class TestIsLiteralComparison:
    rule = IsLiteralComparisonRule()

    def test_positive_string(self, make_ctx):
        ctx = make_ctx("if mode is 'fast':\n    run()\n")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_number(self, make_ctx):
        ctx = make_ctx("while n is not 0:\n    n -= 1\n")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_none_is_ok(self, make_ctx):
        ctx = make_ctx("if a is None:\n    pass\n")
        assert self.rule.check(ctx) == []
