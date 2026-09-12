"""security 类规则正反用例（5 条）。"""

from __future__ import annotations

from audit.detect.rules.python import (
    CommandInjectionRule,
    EvalExecRule,
    HardcodedSecretRule,
    SqlInjectionConcatRule,
    UnsafeDeserializeRule,
)


def _lines(rule, ctx):
    return [h.line_start for h in rule.check(ctx)]


class TestSqlInjection:
    rule = SqlInjectionConcatRule()

    def test_positive_plus_concat(self, make_ctx):
        ctx = make_ctx('query = "SELECT * FROM orders WHERE id = " + order_id\n')
        assert _lines(self.rule, ctx) == [1]

    def test_positive_fstring(self, make_ctx):
        ctx = make_ctx('query = f"SELECT * FROM orders WHERE id = {order_id}"\n')
        assert _lines(self.rule, ctx) == [1]

    def test_positive_percent(self, make_ctx):
        ctx = make_ctx('query = "SELECT * FROM users WHERE name = %s" % name\n')
        assert _lines(self.rule, ctx) == [1]

    def test_positive_format(self, make_ctx):
        ctx = make_ctx('query = "DELETE FROM users WHERE id = {}".format(uid)\n')
        assert _lines(self.rule, ctx) == [1]

    def test_negative_parameterized(self, make_ctx):
        ctx = make_ctx(
            'query = "SELECT * FROM orders WHERE id = ?"\n'
            "cur = conn.execute(query, (order_id,))\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_keyword_only_in_comment(self, make_ctx):
        ctx = make_ctx("# SELECT * FROM t 拼接风险见文档\nx = 1\n")
        assert self.rule.check(ctx) == []


class TestEvalExec:
    rule = EvalExecRule()

    def test_positive(self, make_ctx):
        ctx = make_ctx("def run(expr):\n    return eval(expr)\n")
        assert _lines(self.rule, ctx) == [2]

    def test_positive_exec(self, make_ctx):
        ctx = make_ctx("exec(user_code)\n")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_similar_name(self, make_ctx):
        ctx = make_ctx("score = evaluate(expr)\n")
        assert self.rule.check(ctx) == []

    def test_negative_in_string(self, make_ctx):
        ctx = make_ctx("help = '不要使用 eval(user_input)'\n")
        assert self.rule.check(ctx) == []


class TestCommandInjection:
    rule = CommandInjectionRule()

    def test_positive_concat(self, make_ctx):
        ctx = make_ctx('import os\n\ndef clean(path):\n    os.system("rm -rf " + path)\n')
        assert _lines(self.rule, ctx) == [4]

    def test_positive_variable(self, make_ctx):
        ctx = make_ctx("import os\n\nos.system(user_cmd)\n")
        assert _lines(self.rule, ctx) == [3]

    def test_positive_fstring(self, make_ctx):
        ctx = make_ctx('import os\n\nos.system(f"kill {pid}")\n')
        assert _lines(self.rule, ctx) == [3]

    def test_negative_constant_command(self, make_ctx):
        ctx = make_ctx('import os\n\nos.system("ls -l")\n')
        assert self.rule.check(ctx) == []


class TestHardcodedSecret:
    rule = HardcodedSecretRule()

    def test_positive_api_key(self, make_ctx):
        ctx = make_ctx('API_KEY = "sk-live-9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"\n')
        assert _lines(self.rule, ctx) == [1]

    def test_positive_password(self, make_ctx):
        ctx = make_ctx('DB_PASSWORD = "SuperSecretPass!42"\n')
        assert _lines(self.rule, ctx) == [1]

    def test_positive_camelcase_token(self, make_ctx):
        """R1-9：驼峰分词后精确匹配敏感词（dbPassword → db/password）。"""
        ctx = make_ctx('dbPassword = "T3st-P4ssw0rd-XYZ-9911"\n')
        assert _lines(self.rule, ctx) == [1]

    def test_positive_sk_prefix_even_without_name(self, make_ctx):
        ctx = make_ctx('third_party = "sk-proj-9f8a7b6c5d4e3f2a1b0c9d8e"\n')
        assert _lines(self.rule, ctx) == [1]

    def test_negative_env_lookup(self, make_ctx):
        ctx = make_ctx('API_KEY = os.environ["API_KEY"]\n')
        assert self.rule.check(ctx) == []

    def test_negative_short_value(self, make_ctx):
        ctx = make_ctx('token = "abc"\n')
        assert self.rule.check(ctx) == []

    def test_negative_unrelated_name(self, make_ctx):
        ctx = make_ctx('greeting = "hello cruel world example"\n')
        assert self.rule.check(ctx) == []

    def test_negative_substring_name_not_exact_token(self, make_ctx):
        """R1-9：敏感词只做分词后精确匹配——KEYWORD 不含独立 key token（子串误报类）。"""
        ctx = make_ctx('KEYWORD = "search-keyword-phrase-01"\n')
        assert self.rule.check(ctx) == []

    def test_negative_low_entropy_numeric_constant(self, make_ctx):
        """R1-9：名称含敏感词但值是低熵数字串（无随机性）不报。"""
        ctx = make_ctx('SECRET_THRESHOLD_BYTES = "00000000000000000"\n')
        assert self.rule.check(ctx) == []

    def test_negative_low_entropy_placeholder(self, make_ctx):
        """R1-9：低熵占位串（sk- 前缀但无随机性）不报。"""
        ctx = make_ctx('secret = "sk-aaaaaaaaaaaaaaaaaaaaaaaa"\n')
        assert self.rule.check(ctx) == []

    def test_negative_natural_language_text(self, make_ctx):
        """R1-9：字段名含敏感词但值是自然语言文案（非 ASCII）不报。"""
        ctx = make_ctx('password_error_text = "用户密码不能为空，请重新输入后再试"\n')
        assert self.rule.check(ctx) == []

    # ------------------------------------------------- R4-1：复数形态归一回归

    def test_positive_plural_api_keys(self, make_ctx):
        """R4-1：复数形态 API_KEYS → api/key 归一后命中（修复前 MISS）。"""
        ctx = make_ctx('API_KEYS = "Zk9#pQ2$vL8@mN4&xR7*Wd3!"\n')
        assert _lines(self.rule, ctx) == [1]

    def test_positive_plural_credentials(self, make_ctx):
        """R4-1：credentials → credential 归一后命中（修复前 MISS）。"""
        ctx = make_ctx('credentials = "9f8a7b6c5d4e3f2a1b0c9d8e"\n')
        assert _lines(self.rule, ctx) == [1]

    def test_positive_plural_camelcase_db_passwords(self, make_ctx):
        """R4-1：驼峰复数 dbPasswords → db/password 归一后命中（修复前 MISS）。"""
        ctx = make_ctx('dbPasswords = "T3st-P4ssw0rd-XYZ-9911"\n')
        assert _lines(self.rule, ctx) == [1]

    def test_negative_plural_name_with_plain_text_value(self, make_ctx):
        """R4-1：归一化只放宽名称侧，值侧闸门不降——普通文案值不命中。"""
        ctx = make_ctx('API_KEYS = "placeholder-value-here"\n')
        assert self.rule.check(ctx) == []

    # --------------------------------------- R4-7：password 家族放低闸门回归

    def test_positive_password_family_low_entropy(self, make_ctx):
        """R4-7：password 家族低熵自然词口令命中（旧高熵闸门漏报）。"""
        ctx = make_ctx('DB_PASSWORD = "mysupersecretkey"\n')
        assert _lines(self.rule, ctx) == [1]

    def test_negative_password_family_repeated_char_placeholder(self, make_ctx):
        """R4-7：放宽闸门仍要求最低随机性——单字符重复占位串不报。"""
        ctx = make_ctx('password = "aaaaaaaaaaaaaaaa"\n')
        assert self.rule.check(ctx) == []


class TestUnsafeDeserialize:
    rule = UnsafeDeserializeRule()

    def test_positive_pickle_loads(self, make_ctx):
        ctx = make_ctx("import pickle\n\ndata = pickle.loads(blob)\n")
        assert _lines(self.rule, ctx) == [3]

    def test_positive_yaml_load_no_loader(self, make_ctx):
        ctx = make_ctx("import yaml\n\ncfg = yaml.load(stream)\n")
        assert _lines(self.rule, ctx) == [3]

    def test_positive_yaml_unsafe_load(self, make_ctx):
        ctx = make_ctx("import yaml\n\ncfg = yaml.unsafe_load(stream)\n")
        assert _lines(self.rule, ctx) == [3]

    def test_negative_safe_loader(self, make_ctx):
        ctx = make_ctx("import yaml\n\ncfg = yaml.load(stream, Loader=yaml.SafeLoader)\n")
        assert self.rule.check(ctx) == []

    def test_negative_json_loads(self, make_ctx):
        ctx = make_ctx("import json\n\ndata = json.loads(text)\n")
        assert self.rule.check(ctx) == []
