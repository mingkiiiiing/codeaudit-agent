"""W16 静态规则补缺：PY-PII-LOG 正反用例 + 红线反例。

正例覆盖：security_mixed/sec/pii_py.py 第 7 行原样语料（格式串 + 下标取参）、
f-string/% 形态、logging.getLogger(...).xxx(...) 链式、18 位身份证/11 位手机号
常量字面量（logger 与 print 两种写日志上下文）。
红线反例：clean 语料日志行（无 PII 词、无证件号字面量）、非日志上下文的
phone 取参（pii_py.py 第 11 行同型）、hotel/intel 等软边界词、注释区 PII 词。

追加（PY-PII-SQL，PII 明文入库）：pii_py.py 第 11 行原样语料（INSERT 含 phone
列名 + 绑定 user["phone"]）、绑定 PII 变量（SQL 文本无 PII 列名）、CREATE
TABLE 含 PII 列、UPDATE 写库语句；红线反例照抄 clean 语料 pkg/repo.py 的
参数化 SELECT（user_id/limit 非 PII）、SELECT 读取 PII 列（读非写）、无 PII
的 INSERT。
"""

from __future__ import annotations

from audit.detect.rules.pii_rules import PiiLogRule, PiiSqlRule, build_pii_rules


def _lines(rule, ctx):
    return [h.line_start for h in rule.check(ctx)]


class TestPiiLog:
    rule = PiiLogRule()

    def test_positive_corpus_login_line(self, make_ctx):
        # security_mixed/sec/pii_py.py 第 7 行原样语料
        source = (
            "import logging\n"
            "\n"
            "logger = logging.getLogger(__name__)\n"
            "\n"
            "def log_login(user):\n"
            '    logger.info("login phone=%s idcard=%s", user["phone"], user["idcard"])\n'
        )
        ctx = make_ctx(source)
        assert [h.line_start for h in self.rule.check(ctx)] == [6]

    def test_positive_getlogger_chain_fstring(self, make_ctx):
        ctx = make_ctx('logging.getLogger(__name__).info(f"user ssn={ssn}")\n')
        assert _lines(self.rule, ctx) == [1]

    def test_positive_percent_format(self, make_ctx):
        ctx = make_ctx('logger.warning("user tel=%s", tel_no)\n')
        assert _lines(self.rule, ctx) == [1]

    def test_positive_idcard_literal_in_logger(self, make_ctx):
        ctx = make_ctx('logger.info("idcard=%s", "11010119900307775X")\n')
        assert _lines(self.rule, ctx) == [1]

    def test_positive_mobile_literal_in_print(self, make_ctx):
        ctx = make_ctx('print("mobile 13800138000")\n')
        assert _lines(self.rule, ctx) == [1]

    def test_negative_clean_corpus_log_line(self, make_ctx):
        # 红线：clean 语料（pkg/orders.py）日志行 → 0 命中
        ctx = make_ctx(
            'logger.info("summarized orders total=%s count=%s", total, len(orders))\n'
        )
        assert self.rule.check(ctx) == []

    def test_negative_phone_without_log_context(self, make_ctx):
        # pii_py.py 第 11 行同型：phone 出现在 DB 参数化里，不构成写日志
        ctx = make_ctx(
            'cur.execute("INSERT INTO users(phone) VALUES (?)", (user["phone"],))\n'
        )
        assert self.rule.check(ctx) == []

    def test_negative_literal_without_log_context(self, make_ctx):
        ctx = make_ctx('data = "11010119900307775X"\n')
        assert self.rule.check(ctx) == []

    def test_negative_hotel_word_not_pii(self, make_ctx):
        # tel 软边界：hotel/intel 这类包含 tel 的英文词不命中
        ctx = make_ctx('logger.info("hotel=%s", hotel)\n')
        assert self.rule.check(ctx) == []

    def test_negative_pii_only_in_comment(self, make_ctx):
        ctx = make_ctx('logger.info("done")  # phone 校验通过\n')
        assert self.rule.check(ctx) == []

    def test_negative_longer_digit_run_not_idcard(self, make_ctx):
        # 19 位数字串（订单号/时间戳）不截取 18 位误报
        ctx = make_ctx('logger.info("order=%s", "1234567890123456789")\n')
        assert self.rule.check(ctx) == []


class TestPiiSql:
    rule = PiiSqlRule()

    def test_positive_corpus_insert_line(self, make_ctx):
        # security_mixed/sec/pii_py.py 第 11 行原样语料：INSERT 含 phone 列名
        # 且绑定参数含 user["phone"]，口径 a) 优先报列名
        ctx = make_ctx(
            'cur.execute("INSERT INTO users(phone) VALUES (?)", (user["phone"],))\n'
        )
        hits = self.rule.check(ctx)
        assert [h.line_start for h in hits] == [1]
        assert hits[0].meta["kind"] == "pii-column"
        assert hits[0].meta["pii_names"] == "phone"

    def test_positive_bound_pii_var_without_pii_column(self, make_ctx):
        # 口径 b)：SQL 文本无 PII 列名，但绑定参数含 PII 变量（软边界：user_tel）
        ctx = make_ctx(
            'cur.execute("INSERT INTO users(name, contact) VALUES (?, ?)", (name, user_tel))\n'
        )
        hits = self.rule.check(ctx)
        assert [h.line_start for h in hits] == [1]
        assert hits[0].meta["kind"] == "pii-param"
        assert hits[0].meta["pii_names"] == "tel"

    def test_positive_create_table_with_pii_column(self, make_ctx):
        ctx = make_ctx('cur.execute("CREATE TABLE users(phone TEXT)")\n')
        hits = self.rule.check(ctx)
        assert [h.line_start for h in hits] == [1]
        assert hits[0].meta["kind"] == "pii-column"

    def test_positive_update_statement(self, make_ctx):
        ctx = make_ctx(
            'cur.execute("UPDATE users SET phone = ? WHERE id = ?", (user["phone"], uid))\n'
        )
        assert [h.line_start for h in self.rule.check(ctx)] == [1]

    def test_negative_repo_select_by_id(self, make_ctx):
        # 红线：clean 语料（pkg/repo.py）参数化查询按主键查 → 0 命中
        ctx = make_ctx(
            'row = self._conn.execute("SELECT id, name FROM users WHERE id = ?", (user_id,)).fetchone()\n'
        )
        assert self.rule.check(ctx) == []

    def test_negative_repo_select_limit(self, make_ctx):
        # 红线：clean 语料（pkg/repo.py）LIMIT 参数化 → 0 命中
        ctx = make_ctx(
            'rows = self._conn.execute("SELECT id FROM users WHERE active = 1 LIMIT ?", (limit,)).fetchall()\n'
        )
        assert self.rule.check(ctx) == []

    def test_negative_select_reads_pii_column(self, make_ctx):
        # SELECT 读取 PII 列属"读"而非"明文入库"：列名口径仅在写库语句粗判
        # 通过后生效，且绑定参数区（uid）不含 PII 变量 → 不报
        ctx = make_ctx('cur.execute("SELECT phone FROM users WHERE id = ?", (uid,))\n')
        assert self.rule.check(ctx) == []

    def test_negative_insert_without_pii(self, make_ctx):
        ctx = make_ctx('cur.execute("INSERT INTO users(name) VALUES (?)", (name,))\n')
        assert self.rule.check(ctx) == []


def test_build_pii_rules():
    rules = build_pii_rules()
    assert [r.id for r in rules] == ["PY-PII-LOG", "PY-PII-SQL"]
    assert rules[0].category.value == "security" and rules[0].severity.value == "medium"
    assert rules[0].languages == ("python",)


def test_build_pii_rules_sql_meta():
    rules = {r.id: r for r in build_pii_rules()}
    rule = rules["PY-PII-SQL"]
    assert rule.category.value == "security" and rule.severity.value == "medium"
    assert rule.languages == ("python",)
    assert rule.bad_example.strip() and rule.good_example.strip()
