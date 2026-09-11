"""performance 类规则正反用例（6 条）。"""

from __future__ import annotations

from audit.detect.rules.python import (
    DeepcopyInLoopRule,
    IoInLoopRule,
    ListMembershipRule,
    RepeatInvariantCallRule,
    RequestNoTimeoutRule,
    StrConcatInLoopRule,
)


def _lines(rule, ctx):
    return [h.line_start for h in rule.check(ctx)]


class TestListMembership:
    rule = ListMembershipRule()

    def test_positive(self, make_ctx):
        ctx = make_ctx(
            "def dedup(items):\n"
            "    seen = []\n"
            "    out = []\n"
            "    for i in items:\n"
            "        if i in seen:\n"
            "            continue\n"
            "        seen.append(i)\n"
            "        out.append(i)\n"
            "    return out\n"
        )
        assert _lines(self.rule, ctx) == [5]

    def test_negative_set_membership(self, make_ctx):
        ctx = make_ctx(
            "def dedup(items):\n"
            "    seen = set()\n"
            "    for i in items:\n"
            "        if i in seen:\n"
            "            continue\n"
            "        seen.add(i)\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_not_in_function(self, make_ctx):
        # 模块级（无函数作用域）不在启发式范围内
        ctx = make_ctx("seen = []\nif x in seen:\n    pass\n")
        assert self.rule.check(ctx) == []

    def test_negative_unrelated_var(self, make_ctx):
        ctx = make_ctx(
            "def f(items, index):\n"
            "    seen = []\n"
            "    if index in items:\n"
            "        pass\n"
        )
        assert self.rule.check(ctx) == []


class TestStrConcatInLoop:
    rule = StrConcatInLoopRule()

    def test_positive_self_concat(self, make_ctx):
        ctx = make_ctx(
            'def build(rows):\n'
            '    html = ""\n'
            "    for r in rows:\n"
            '        html = html + "<li>" + str(r) + "</li>"\n'
            "    return html\n"
        )
        assert _lines(self.rule, ctx) == [4]

    def test_positive_plus_eq(self, make_ctx):
        ctx = make_ctx(
            "def build(rows):\n"
            '    out = ""\n'
            "    for r in rows:\n"
            '        out += "<li>"\n'
            "    return out\n"
        )
        assert _lines(self.rule, ctx) == [4]

    def test_negative_int_accumulate(self, make_ctx):
        ctx = make_ctx(
            "def total(rows):\n"
            "    s = 0\n"
            "    for r in rows:\n"
            "        s = s + r\n"
            "    return s\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_outside_loop(self, make_ctx):
        ctx = make_ctx('def f(a, b):\n    s = ""\n    s = s + a + b\n    return s\n')
        assert self.rule.check(ctx) == []


class TestIoInLoop:
    rule = IoInLoopRule()

    def test_positive_open_in_loop(self, make_ctx):
        ctx = make_ctx(
            "def sizes(names):\n"
            "    out = []\n"
            "    for n in names:\n"
            "        f = open(n)\n"
            "        out.append(len(f.read()))\n"
            "        f.close()\n"
            "    return out\n"
        )
        assert _lines(self.rule, ctx) == [4]

    def test_positive_execute_in_loop(self, make_ctx):
        ctx = make_ctx(
            "def load(ids, conn):\n"
            "    for i in ids:\n"
            "        cur = conn.execute('SELECT * FROM t WHERE id=?', (i,))\n"
            "    return cur\n"
        )
        assert _lines(self.rule, ctx) == [3]

    def test_negative_io_outside_loop(self, make_ctx):
        ctx = make_ctx("def rd(p, conn):\n    f = open(p)\n    conn.execute('SELECT 1')\n    f.close()\n")
        assert self.rule.check(ctx) == []

    def test_negative_with_open_in_loop(self, make_ctx):
        # with open 已确保逐次关闭，属受控逐条 IO，本规则不报
        ctx = make_ctx(
            "def sizes(names):\n"
            "    out = []\n"
            "    for n in names:\n"
            "        with open(n) as f:\n"
            "            out.append(len(f.read()))\n"
            "    return out\n"
        )
        assert self.rule.check(ctx) == []


class TestRepeatInvariantCall:
    rule = RepeatInvariantCallRule()

    def test_positive_duplicate_call(self, make_ctx):
        ctx = make_ctx(
            "def f(cfg):\n"
            "    a = load_config(cfg)\n"
            "    b = load_config(cfg)\n"
            "    return a + b\n"
        )
        assert _lines(self.rule, ctx) == [3]

    def test_negative_distinct_args(self, make_ctx):
        ctx = make_ctx("def f(x, y):\n    a = load(x)\n    b = load(y)\n    return a + b\n")
        assert self.rule.check(ctx) == []

    def test_negative_outside_function(self, make_ctx):
        ctx = make_ctx("a = load_config(c)\nb = load_config(c)\n")
        assert self.rule.check(ctx) == []


class TestDeepcopyInLoop:
    rule = DeepcopyInLoopRule()

    def test_positive(self, make_ctx):
        ctx = make_ctx(
            "import copy\n"
            "def clone_all(items):\n"
            "    out = []\n"
            "    for x in items:\n"
            "        out.append(copy.deepcopy(x))\n"
            "    return out\n"
        )
        assert _lines(self.rule, ctx) == [5]

    def test_negative_outside_loop(self, make_ctx):
        ctx = make_ctx("import copy\ndef f(x):\n    return copy.deepcopy(x)\n")
        assert self.rule.check(ctx) == []


class TestRequestNoTimeout:
    rule = RequestNoTimeoutRule()

    def test_positive_urlopen(self, make_ctx):
        ctx = make_ctx(
            "import urllib.request\n"
            "def fetch(url):\n"
            "    resp = urllib.request.urlopen(url)\n"
            "    return resp.read()\n"
        )
        assert _lines(self.rule, ctx) == [3]

    def test_positive_requests_get(self, make_ctx):
        ctx = make_ctx("import requests\n\ndef fetch(url):\n    return requests.get(url).text\n")
        assert _lines(self.rule, ctx) == [4]

    def test_negative_with_timeout(self, make_ctx):
        ctx = make_ctx(
            "import urllib.request\n"
            "def fetch(url, timeout=10):\n"
            "    resp = urllib.request.urlopen(url, timeout=timeout)\n"
            "    return resp.read()\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_multiline_with_timeout(self, make_ctx):
        ctx = make_ctx(
            "import requests\n"
            "def fetch(url):\n"
            "    return requests.get(\n"
            "        url,\n"
            "        timeout=5,\n"
            "    ).text\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_dict_get(self, make_ctx):
        ctx = make_ctx("def f(d):\n    return d.get('k')\n")
        assert self.rule.check(ctx) == []
