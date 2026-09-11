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
