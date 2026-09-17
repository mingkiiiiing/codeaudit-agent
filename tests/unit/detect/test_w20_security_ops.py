"""W20-C 运维安全双形态规则（py_security_ops.py）正反用例。

正例覆盖：默认凭据四绑定形态（赋值 / 字典键值 / kwargs / ==/!= 比较、下标、
walrus），日志伪造的 % 参数化 / f-string 插值 / print / 多行三引号形态。

红线反例（误报高危点）：强随机密钥（PY-HARDCODED-SECRET 领地，弱口令字典
整词比对不碰）、os.environ 引用与回退默认值、${...}/<...> 模板占位、
changeme 兼作占位的双面语义（绑定名含占位语义才豁免）、空值、大字符串内嵌
假代码、注释；日志侧：无 \\n、纯静态 \\n、raw 字符串、全大写常量参数、
kwargs 形参名（sep=）、非日志调用的换行字符串，以及 clean 语料摘录。
"""

from __future__ import annotations

from audit.detect.rules.py_security_ops import (
    DefaultCredentialRule,
    LogForgeryRule,
    build_security_ops_rules,
)


def _lines(rule, ctx):
    return [h.line_start for h in rule.check(ctx)]


class TestDefaultCredential:
    rule = DefaultCredentialRule()

    def test_assign_admin_hit(self, make_ctx):
        ctx = make_ctx('password = "admin"\n')
        hits = self.rule.check(ctx)
        assert [h.line_start for h in hits] == [1]
        assert hits[0].severity.value == "high"
        assert hits[0].category.value == "security"
        assert hits[0].meta["var"] == "password"
        assert "出厂默认" in hits[0].message

    def test_env_style_upper_hit(self, make_ctx):
        # DB_PASS = "123456"：环境变量风格命名 + 弱数字口令
        ctx = make_ctx('DB_PASS = "123456"\n')
        assert [h.line_start for h in self.rule.check(ctx)] == [1]

    def test_case_insensitive_hit(self, make_ctx):
        # 全部小写比对：Password = "ADMIN" 同样命中
        ctx = make_ctx('Password = "ADMIN"\n')
        assert [h.line_start for h in self.rule.check(ctx)] == [1]

    def test_dict_key_value_hit(self, make_ctx):
        ctx = make_ctx('AUTH = {"api_key": "12345678"}\n')
        hits = self.rule.check(ctx)
        assert [h.line_start for h in hits] == [1]
        assert hits[0].meta["var"] == "api_key"

    def test_kwargs_hit(self, make_ctx):
        ctx = make_ctx('conn = connect(host, password="admin")\n')
        assert [h.line_start for h in self.rule.check(ctx)] == [1]

    def test_comparison_hit(self, make_ctx):
        ctx = make_ctx(
            'def grant(pwd):\n'
            '    if input_password == "root":\n'
            "        return True\n"
            "    return False\n"
        )
        assert [h.line_start for h in self.rule.check(ctx)] == [2]

    def test_subscript_hit(self, make_ctx):
        # 下标形态：掩码把下标串置空，需回原文补取键名
        ctx = make_ctx('cfg["access_key"] = "letmein"\n')
        assert [h.line_start for h in self.rule.check(ctx)] == [1]

    def test_walrus_hit(self, make_ctx):
        ctx = make_ctx('if (pwd := "qwerty"):\n    login(pwd)\n')
        assert [h.line_start for h in self.rule.check(ctx)] == [1]

    def test_strong_secret_no_hit(self, make_ctx):
        # 强随机值不在弱口令字典：PY-HARDCODED-SECRET 领地，两规则不重叠
        ctx = make_ctx(
            'DATABASE_PASSWORD = "S3cret-P4ssw0rd!2026-prod"\n'
            'API_KEY = "sk-9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c"\n'
        )
        assert self.rule.check(ctx) == []

    def test_env_ref_no_hit(self, make_ctx):
        # 环境变量引用豁免：os.environ / os.getenv 及回退默认值
        ctx = make_ctx(
            'password = os.environ["DB_PASSWORD"]\n'
            'pwd = os.environ.get("DB_PASSWORD")\n'
            'token = os.getenv("API_TOKEN", "admin")  # 回退值被调用表达式隔断\n'
        )
        assert self.rule.check(ctx) == []

    def test_placeholder_value_no_hit(self, make_ctx):
        # ${...} / <...> 模板占位豁免
        ctx = make_ctx(
            'password = "${DB_PASSWORD}"\n'
            'api_key = "<api-token>"\n'
            'secret = "{{DB_PASSWORD}}"\n'  # 非占位形态但不在字典
        )
        assert self.rule.check(ctx) == []

    def test_changeme_dual_semantics(self, make_ctx):
        # changeme 双面语义：普通凭据名照报；绑定名含占位语义则豁免
        ctx = make_ctx('password = "changeme"\n')
        assert [h.line_start for h in self.rule.check(ctx)] == [1]
        ctx2 = make_ctx('DEFAULT_PASSWORD = "changeme"\nPLACEHOLDER_TOKEN = "changeme"\n')
        assert self.rule.check(ctx2) == []

    def test_empty_value_no_hit(self, make_ctx):
        ctx = make_ctx('password = ""\nsecret = \'\'\n')
        assert self.rule.check(ctx) == []

    def test_embedded_string_and_comment_no_hit(self, make_ctx):
        # 大字符串内的假代码与注释不构成真实绑定
        ctx = make_ctx(
            "doc = \"default credentials like password = 'admin' are dangerous\"\n"
            '# password = "admin"\n'
        )
        assert self.rule.check(ctx) == []

    def test_nonbinding_value_no_hit(self, make_ctx):
        # 字典值命中但绑定名无凭据语义（username 不在语义表）
        ctx = make_ctx('opts = {"username": "admin", "retries": "3"}\n')
        assert self.rule.check(ctx) == []


class TestLogForgery:
    rule = LogForgeryRule()

    def test_logger_newline_var_hit(self, make_ctx):
        ctx = make_ctx(
            'logger = get_logger()\n'
            '\n'
            'def login(user):\n'
            '    logger.info("user %s logged in\\ntrace: x", user)\n'
        )
        hits = self.rule.check(ctx)
        assert [h.line_start for h in hits] == [4]
        assert hits[0].severity.value == "low"
        assert hits[0].category.value == "security"
        assert hits[0].meta["api"] == "logging"
        assert "log forging" in hits[0].message

    def test_no_newline_no_hit(self, make_ctx):
        ctx = make_ctx('logger.info("user %s logged in", user)\n')
        assert self.rule.check(ctx) == []

    def test_static_newline_no_hit(self, make_ctx):
        # 纯静态字符串日志（无变量拼接）保守不报
        ctx = make_ctx('logger.info("start batch\\nend batch")\nprint("done\\n")\n')
        assert self.rule.check(ctx) == []

    def test_print_hit(self, make_ctx):
        ctx = make_ctx('print("results for", user_input, "\\n")\n')
        hits = self.rule.check(ctx)
        assert [h.line_start for h in hits] == [1]
        assert hits[0].meta["api"] == "print"

    def test_raw_string_no_hit(self, make_ctx):
        # raw 字符串里 \n 是字面两字符，不构成换行注入
        ctx = make_ctx(r'logger.info(r"line1\nline2 %s", user)' + "\n")
        assert self.rule.check(ctx) == []

    def test_fstring_interp_hit(self, make_ctx):
        # f-string 插值：掩码后无残留标识符，需回原文查 {}
        ctx = make_ctx('logger.info(f"user {user} logged in\\n")\n')
        assert [h.line_start for h in self.rule.check(ctx)] == [1]

    def test_constant_only_no_hit(self, make_ctx):
        # 全大写常量 / True/False/None 不算非常量变量
        ctx = make_ctx(
            'logger.info("header\\n", TOTAL_LIMIT)\n'
            'logger.debug("flag\\n%s", True)\n'
        )
        assert self.rule.check(ctx) == []

    def test_kwarg_name_only_no_hit(self, make_ctx):
        # kwargs 形参名（sep=/file=）不是注入载体，纯静态输出不报
        ctx = make_ctx(
            'print("a\\nb", sep=", ")\n'
            'print("a\\nb", file=sys.stderr)\n'
        )
        assert self.rule.check(ctx) == []

    def test_multiline_triple_quote_hit(self, make_ctx):
        ctx = make_ctx(
            'logger.info("""login audit:\n'
            'user %s""", user)\n'
        )
        assert [h.line_start for h in self.rule.check(ctx)] == [1]

    def test_non_log_call_no_hit(self, make_ctx):
        # 非日志调用的换行字符串不涉本规则
        ctx = make_ctx(
            'msg = "a\\nb"\n'
            'data = "\\n".join(lines)\n'
        )
        assert self.rule.check(ctx) == []

    def test_format_call_hit(self, make_ctx):
        # .format 注入形态
        ctx = make_ctx('logger.info("user {} logged in\\n".format(name))\n')
        assert [h.line_start for h in self.rule.check(ctx)] == [1]


class TestCleanCorpusExcerpt:
    """clean 语料摘录（orders.py / repo.py / pii 风格日志行）→ 两条规则全 0 命中。"""

    def test_clean_orders_excerpt(self, make_ctx):
        ctx = make_ctx(
            "import json\n"
            "import logging\n"
            "\n"
            "logger = logging.getLogger(__name__)\n"
            "\n"
            "def summarize(orders):\n"
            '    logger.info("summarized orders total=%s count=%s", total, len(orders))\n'
            '    return "\\n".join(lines)\n'
        )
        assert all(r.check(ctx) == [] for r in build_security_ops_rules())

    def test_clean_repo_excerpt(self, make_ctx):
        ctx = make_ctx(
            "class Repository:\n"
            "    def find_user(self, user_id):\n"
            '        row = self._conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()\n'
            "        if row is None:\n"
            "            return None\n"
            '        logger.warning("invalid amount: %s", amount)\n'
            '        return {"id": row[0], "name": row[1]}\n'
        )
        assert all(r.check(ctx) == [] for r in build_security_ops_rules())

    def test_clean_pii_style_log(self, make_ctx):
        # security_mixed pii_py 原样形态（无 \n）：日志伪造规则不得新增命中
        ctx = make_ctx(
            'logger.info("login phone=%s idcard=%s", user["phone"], user["idcard"])\n'
        )
        assert all(r.check(ctx) == [] for r in build_security_ops_rules())


def test_build_security_ops_rules():
    rules = build_security_ops_rules()
    assert [r.id for r in rules] == ["PY-DEFAULT-CREDENTIAL", "PY-LOG-FORGERY"]
    ids = {r.id: r for r in rules}
    for rule in rules:
        assert rule.languages == ("python",)
        assert rule.category.value == "security"
        assert rule.bad_example.strip() and rule.good_example.strip()
    assert ids["PY-DEFAULT-CREDENTIAL"].severity.value == "high"
    assert ids["PY-LOG-FORGERY"].severity.value == "low"
