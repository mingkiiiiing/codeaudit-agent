"""W19-A 圈复杂度数值化规则（PY-CYCLOMATIC-COMPLEXITY）单测。

口径：CC = 决策点数 + 1（if/elif/for/while/except/and/or/assert 各计 1，
with/else 不计，三元与推导式按其 if/for 计入）；CC > 阈值才报（默认阈值 10，
env CODEAUDIT_CC_THRESHOLD 覆盖）。

红线约定：clean_corpus（D:/acc_tmp/corpus/clean_corpus）的全部惯用写法对本规则
必须 0 命中——pkg/orders.py 与 pkg/repo.py 全文逐字拷入作红线反例；
W18 深度审计留下的 CC=9 标定样本（D:/acc_tmp/w18_cc/cc_sample.py）逐字拷入，
CC=9 ≤ 10 必须不报。
"""

from __future__ import annotations

import pytest

from audit.detect.rules.py_complexity import PyCyclomaticComplexityRule

RULE_ID = "PY-CYCLOMATIC-COMPLEXITY"


@pytest.fixture
def rule() -> PyCyclomaticComplexityRule:
    return PyCyclomaticComplexityRule()


@pytest.fixture
def default_threshold(monkeypatch):
    """恢复默认阈值口径（避免开发者机器上的环境变量影响断言）。"""
    monkeypatch.delenv("CODEAUDIT_CC_THRESHOLD", raising=False)


def _hits(rule, ctx):
    return rule.check(ctx)


def _if_chain(n_branches: int, indent: str = "") -> str:
    """构造 n 个串行分支（if 1 + elif n-1）的函数：决策点数 = n。"""
    body = [f"{indent}def chain(v):\n"]
    for i in range(n_branches):
        kw = "if" if i == 0 else "elif"
        body.append(f"{indent}    {kw} v == {i}:\n{indent}        return {i}\n")
    body.append(f"{indent}    return -1\n")
    return "".join(body)


# W18 深度审计的 CC=9 人工标定样本（D:/acc_tmp/w18_cc/cc_sample.py）逐字拷入
CC9_SAMPLE = (
    "def grade(score, bonus):\n"
    "    if score < 0 or score > 100:\n"
    "        return 0\n"
    "    elif score >= 90 and bonus:\n"
    "        return 10\n"
    "    elif score >= 80:\n"
    "        return 8\n"
    "    elif score >= 60:\n"
    "        return 6\n"
    "    total = 0\n"
    "    for b in bonus_list:\n"
    "        if b:\n"
    "            total += 1\n"
    "    return total + bonus\n"
    "bonus_list = []\n"
)

# 手算 CC=14：if 2 + or 2 + elif 1 + and 4 + for 1 + while 1 + except 2 = 13 决策点
CC14_FUNC = (
    "def classify(rows, flag, limit):\n"
    "    if flag is None or not rows or limit is None:\n"
    "        return 0\n"
    '    elif flag and limit and len(rows) > limit and flag != "skip":\n'
    "        return 1\n"
    "    total = 0\n"
    "    for r in rows:\n"
    "        if r % 2 == 0 and r > 0:\n"
    "            total += r\n"
    "    while total > 100:\n"
    "        total -= 100\n"
    "    try:\n"
    "        total = total // len(rows)\n"
    "    except ZeroDivisionError:\n"
    "        total = -1\n"
    "    except TypeError:\n"
    "        total = -2\n"
    "    return total\n"
)

# clean 语料红线反例：pkg/orders.py 全文逐字拷入
CLEAN_ORDERS = (
    "from __future__ import annotations\n"
    "\n"
    "import json\n"
    "import logging\n"
    "import subprocess\n"
    "from dataclasses import dataclass, field\n"
    "from pathlib import Path\n"
    "from urllib.request import urlopen\n"
    "\n"
    "logger = logging.getLogger(__name__)\n"
    "\n"
    "MAX_BATCH_SIZE = 100\n"
    'SERVICE_ENDPOINT = "https://api.example.com/v1"\n'
    "\n"
    "\n"
    "@dataclass\n"
    "class Order:\n"
    "    order_id: str\n"
    "    amount: int\n"
    "    tags: list[str] = field(default_factory=list)\n"
    "\n"
    "\n"
    "def load_orders(path: Path) -> list[Order]:\n"
    '    with path.open(encoding="utf-8") as fh:\n'
    "        payload = json.load(fh)\n"
    '    return [Order(order_id=str(item["id"]), amount=int(item["amount"])) for item in payload]\n'
    "\n"
    "\n"
    "def summarize(orders: list[Order]) -> dict[str, int]:\n"
    "    known = {o.order_id for o in orders}\n"
    "    total = sum(o.amount for o in orders if o.order_id in known)\n"
    '    logger.info("summarized orders total=%s count=%s", total, len(orders))\n'
    '    return {"total": total, "count": len(orders)}\n'
    "\n"
    "\n"
    "def run_sync(command: list[str]) -> str:\n"
    "    proc = subprocess.run(command, capture_output=True, text=True, encoding=\"utf-8\", check=True)\n"
    "    return proc.stdout.strip()\n"
    "\n"
    "\n"
    "def fetch_remote(url: str) -> bytes:\n"
    "    with urlopen(url, timeout=10) as resp:\n"
    "        return resp.read()\n"
    "\n"
    "\n"
    "def render_report(orders: list[Order]) -> str:\n"
    '    lines = [f"{o.order_id}: {o.amount}" for o in orders]\n'
    '    return "\\n".join(lines)\n'
)

# clean 语料红线反例：pkg/repo.py 全文逐字拷入
CLEAN_REPO = (
    "import logging\n"
    "\n"
    "logger = logging.getLogger(__name__)\n"
    "\n"
    "\n"
    "class Repository:\n"
    "    def __init__(self, conn):\n"
    "        self._conn = conn\n"
    "\n"
    "    def find_user(self, user_id: int):\n"
    '        row = self._conn.execute("SELECT id, name FROM users WHERE id = ?", (user_id,)).fetchone()\n'
    "        if row is None:\n"
    "            return None\n"
    '        return {"id": row[0], "name": row[1]}\n'
    "\n"
    "    def list_active(self, limit: int = 50):\n"
    '        rows = self._conn.execute("SELECT id FROM users WHERE active = 1 LIMIT ?", (limit,)).fetchall()\n'
    "        return [r[0] for r in rows]\n"
    "\n"
    "\n"
    "def validate_amount(amount: int) -> bool:\n"
    "    if amount <= 0:\n"
    '        logger.warning("invalid amount: %s", amount)\n'
    "        return False\n"
    "    return True\n"
    "\n"
    "\n"
    "def join_names(names: list[str]) -> str:\n"
    '    return ", ".join(names)\n'
)


class TestCountingStandard:
    """逐构造断言 CC 数值（调低阈值让命中发生，从而读出 meta 里的数值）。"""

    def test_sequential_if_chain(self, rule, make_ctx, monkeypatch):
        # if + elif×3 = 4 决策点 → CC = 5
        monkeypatch.setenv("CODEAUDIT_CC_THRESHOLD", "4")
        hits = _hits(rule, make_ctx(_if_chain(4)))
        assert len(hits) == 1
        assert hits[0].meta["cc"] == 5
        assert "分支 4" in hits[0].message

    def test_and_or_combination(self, rule, make_ctx, monkeypatch):
        # if 1 + and 4 + or 2 = 7 决策点 → CC = 8
        monkeypatch.setenv("CODEAUDIT_CC_THRESHOLD", "7")
        src = (
            "def complex_cond(a, b, c, d, e, f, g):\n"
            "    if (a and b) or (not c and d) or (e and not f and g):\n"
            "        return 1\n"
            "    return 0\n"
        )
        hits = _hits(rule, make_ctx(src))
        assert len(hits) == 1
        assert hits[0].meta["cc"] == 8
        assert hits[0].meta["boolean_ops"] == 6
        assert "布尔运算 6" in hits[0].message

    def test_for_plus_if(self, rule, make_ctx, monkeypatch):
        # for 1 + if 1 → CC = 3
        monkeypatch.setenv("CODEAUDIT_CC_THRESHOLD", "2")
        src = (
            "def count_even(xs):\n"
            "    total = 0\n"
            "    for x in xs:\n"
            "        if x % 2 == 0:\n"
            "            total += 1\n"
            "    return total\n"
        )
        hits = _hits(rule, make_ctx(src))
        assert len(hits) == 1
        assert hits[0].meta["cc"] == 3
        assert "分支 1、循环 1" in hits[0].message  # 分组顺序固定：分支在前

    def test_two_except_handlers(self, rule, make_ctx, monkeypatch):
        # except×2 → CC = 3
        monkeypatch.setenv("CODEAUDIT_CC_THRESHOLD", "2")
        src = (
            "def parse(text):\n"
            "    try:\n"
            "        return int(text)\n"
            "    except ValueError:\n"
            "        return 0\n"
            "    except TypeError:\n"
            "        return -1\n"
        )
        hits = _hits(rule, make_ctx(src))
        assert len(hits) == 1
        assert hits[0].meta["cc"] == 3
        assert "异常 2" in hits[0].message

    def test_ternary_counts_one(self, rule, make_ctx, monkeypatch):
        # 三元 if-else 的 if 计 1 → CC = 2；else 不计
        monkeypatch.setenv("CODEAUDIT_CC_THRESHOLD", "1")
        hits = _hits(rule, make_ctx('def pick(x):\n    return "a" if x else "b"\n'))
        assert len(hits) == 1
        assert hits[0].meta["cc"] == 2
        assert hits[0].meta["branches"] == 1

    def test_comprehension_for_if(self, rule, make_ctx, monkeypatch):
        # 推导式 for/if 各计 1 → CC = 3
        monkeypatch.setenv("CODEAUDIT_CC_THRESHOLD", "2")
        src = "def tops(xs):\n    return [x for x in xs if x > 0]\n"
        hits = _hits(rule, make_ctx(src))
        assert len(hits) == 1
        assert hits[0].meta["cc"] == 3
        assert hits[0].meta["loops"] == 1
        assert hits[0].meta["branches"] == 1

    def test_while_counts(self, rule, make_ctx, monkeypatch):
        monkeypatch.setenv("CODEAUDIT_CC_THRESHOLD", "1")
        src = "def shrink(n):\n    while n > 1:\n        n //= 2\n    return n\n"
        hits = _hits(rule, make_ctx(src))
        assert len(hits) == 1
        assert hits[0].meta["cc"] == 2

    def test_assert_counts(self, rule, make_ctx, monkeypatch):
        monkeypatch.setenv("CODEAUDIT_CC_THRESHOLD", "1")
        src = "def check(x):\n    assert x is not None\n    return x\n"
        hits = _hits(rule, make_ctx(src))
        assert len(hits) == 1
        assert hits[0].meta["cc"] == 2
        assert "断言 1" in hits[0].message

    def test_with_not_counted(self, rule, make_ctx, monkeypatch):
        # with 不计（非分支）：with + 8 个 if = 8 决策点 → CC = 9（若 with 被误计则为 10）
        monkeypatch.setenv("CODEAUDIT_CC_THRESHOLD", "8")
        body = "".join(f"    if v == {i}:\n        return {i}\n" for i in range(8))
        src = (
            "def read_head(path, v):\n"
            "    with open(path) as fh:\n"
            "        head = fh.read(10)\n"
            + body
            + "    return head\n"
        )
        hits = _hits(rule, make_ctx(src))
        assert len(hits) == 1
        assert hits[0].meta["cc"] == 9

    def test_string_and_comment_tokens_ignored(self, rule, make_ctx, monkeypatch):
        # 字符串/注释里的决策点 token 不计：真实 5 个 if → CC = 6（若误计则为 14）
        monkeypatch.setenv("CODEAUDIT_CC_THRESHOLD", "5")
        src = (
            "def noisy(x, y, z, a, b, c):\n"
            '    hint = "if elif for while and or except assert"\n'
            "    # if for while and or assert except elif\n"
            "    if x:\n"
            "        return 1\n"
            "    if y:\n"
            "        return 2\n"
            "    if z:\n"
            "        return 3\n"
            "    if a:\n"
            "        return 4\n"
            "    if b:\n"
            "        return 5\n"
            "    return 0\n"
        )
        hits = _hits(rule, make_ctx(src))
        assert len(hits) == 1
        assert hits[0].meta["cc"] == 6


class TestThresholdSemantics:
    """CC > 阈值才报；默认 10（env CODEAUDIT_CC_THRESHOLD 覆盖）。"""

    def test_cc10_not_reported(self, rule, make_ctx, default_threshold):
        # 9 分支 = 9 决策点 → CC = 10，阈值 10 不报（对齐 SonarQube 默认语义）
        assert _hits(rule, make_ctx(_if_chain(9))) == []

    def test_cc11_reported_on_def_line(self, rule, make_ctx, default_threshold):
        # 10 分支 → CC = 11，报 1 条，命中行 = def 行
        hits = _hits(rule, make_ctx(_if_chain(10)))
        assert len(hits) == 1
        assert hits[0].rule_id == RULE_ID
        assert hits[0].line_start == 1
        assert hits[0].line_end == 1

    def test_env_threshold_override(self, rule, make_ctx, monkeypatch):
        # 阈值降到 5 后，CC=9 的标定样本必须报
        monkeypatch.setenv("CODEAUDIT_CC_THRESHOLD", "5")
        hits = _hits(rule, make_ctx(CC9_SAMPLE))
        assert len(hits) == 1
        assert hits[0].meta["cc"] == 9
        assert hits[0].meta["threshold"] == 5

    def test_env_threshold_equal_not_reported(self, rule, make_ctx, monkeypatch):
        # 严格大于语义：阈值 9 时 CC=9 恰好等于阈值，仍不报
        monkeypatch.setenv("CODEAUDIT_CC_THRESHOLD", "9")
        assert _hits(rule, make_ctx(CC9_SAMPLE)) == []

    def test_env_threshold_invalid_falls_back(self, rule, make_ctx, monkeypatch):
        # 非法 env 值回退默认 10：CC=9 不报
        monkeypatch.setenv("CODEAUDIT_CC_THRESHOLD", "not-a-number")
        assert _hits(rule, make_ctx(CC9_SAMPLE)) == []


class TestCorpusRedline:
    """clean 语料红线：0 命中；W18 CC=9 标定样本不回填告警。"""

    def test_cc9_sample_not_reported(self, rule, make_ctx, default_threshold):
        assert _hits(rule, make_ctx(CC9_SAMPLE)) == []

    def test_clean_corpus_orders_zero_hits(self, rule, make_ctx, default_threshold):
        assert _hits(rule, make_ctx(CLEAN_ORDERS)) == []

    def test_clean_corpus_repo_zero_hits(self, rule, make_ctx, default_threshold):
        assert _hits(rule, make_ctx(CLEAN_REPO)) == []

    def test_cc14_reported_with_distribution(self, rule, make_ctx, default_threshold):
        hits = _hits(rule, make_ctx(CC14_FUNC))
        assert len(hits) == 1
        hit = hits[0]
        assert hit.line_start == 1  # 命中行 = def 行
        assert "圈复杂度 14（阈值 10）" in hit.message
        assert "分支 3、循环 2、布尔运算 6、异常 2" in hit.message
        assert hit.meta["cc"] == 14

    def test_determinism_same_input_same_value(self, rule, make_ctx, default_threshold):
        # 同输入两次运行，数值与全部字段一致（确定性硬要求）
        first = _hits(rule, make_ctx(CC14_FUNC))
        second = _hits(rule, make_ctx(CC14_FUNC))
        assert [
            (h.rule_id, h.line_start, h.line_end, h.message, tuple(sorted(h.meta.items())))
            for h in first
        ] == [
            (h.rule_id, h.line_start, h.line_end, h.message, tuple(sorted(h.meta.items())))
            for h in second
        ]


class TestScoping:
    """作用域口径：类方法同样计算；嵌套函数单独计、不累加进外层。"""

    def test_class_method_counted(self, rule, make_ctx, default_threshold):
        # 方法 10 分支 → CC = 11，命中行 = 方法 def 行（第 2 行）
        src = "class Router:\n" + _if_chain(10, indent="    ")
        hits = _hits(rule, make_ctx(src))
        assert len(hits) == 1
        assert hits[0].line_start == 2
        assert "函数 `chain`" in hits[0].message

    def test_nested_function_counted_separately(self, rule, make_ctx, monkeypatch):
        # 外层只有 1 个 if（CC=2），嵌套 inner 2 个 if（CC=3）；
        # 嵌套行不得累加进外层（否则外层 CC=4 也会被报）
        monkeypatch.setenv("CODEAUDIT_CC_THRESHOLD", "2")
        src = (
            "def outer(v):\n"
            "    def inner(w):\n"
            "        if w:\n"
            "            return 1\n"
            "        if w == 2:\n"
            "            return 2\n"
            "        return 0\n"
            "    if v:\n"
            "        return inner(v)\n"
            "    return 0\n"
        )
        hits = _hits(rule, make_ctx(src))
        assert [h.line_start for h in hits] == [2]  # 只有 inner 的 def 行
        assert hits[0].meta["cc"] == 3

    def test_at_most_one_hit_per_function(self, rule, make_ctx, default_threshold):
        # CC=14 的函数也只报 1 条
        assert len(_hits(rule, make_ctx(CC14_FUNC))) == 1
