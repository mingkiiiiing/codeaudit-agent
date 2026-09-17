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


class TestCommandInjectionW19C:
    """W19-C 命令注入规则增强：subprocess.Popen / os.execv* / 列表 sh -c 中转三形态。"""

    rule = CommandInjectionRule()

    # ------------------------------------------------ 形态 a：Popen shell=True / 动态首参

    def test_positive_popen_shell_true_var(self, make_ctx):
        ctx = make_ctx("import subprocess\n\nsubprocess.Popen(cmd, shell=True)\n")
        assert _lines(self.rule, ctx) == [3]

    def test_positive_popen_shell_true_literal(self, make_ctx):
        """shell=True 时字符串参数会进入 shell 解释：首参为字面量也报。"""
        ctx = make_ctx('import subprocess\n\nsubprocess.Popen("ls -l", shell=True)\n')
        assert _lines(self.rule, ctx) == [3]

    def test_positive_popen_concat(self, make_ctx):
        ctx = make_ctx('import subprocess\n\nsubprocess.Popen("ping " + host)\n')
        assert _lines(self.rule, ctx) == [3]

    def test_positive_popen_fstring(self, make_ctx):
        ctx = make_ctx('import subprocess\n\nsubprocess.Popen(f"kill {pid}")\n')
        assert _lines(self.rule, ctx) == [3]

    def test_negative_popen_literal_no_shell(self, make_ctx):
        ctx = make_ctx('import subprocess\n\nsubprocess.Popen("ls -l")\n')
        assert self.rule.check(ctx) == []

    # ------------------------------------------------ 形态 b：os.execv* 调 shell

    def test_positive_execv_sh_c(self, make_ctx):
        ctx = make_ctx('import os\n\nos.execv("/bin/sh", ["sh", "-c", cmd])\n')
        assert _lines(self.rule, ctx) == [3]

    def test_positive_execvp_bash_c(self, make_ctx):
        ctx = make_ctx('import os\n\nos.execvp("bash", ["bash", "-c", script])\n')
        assert _lines(self.rule, ctx) == [3]

    def test_negative_execv_fixed_payload(self, make_ctx):
        """execv 走 sh -c 但负载为纯字面量（无变量残留）不报。"""
        ctx = make_ctx('import os\n\nos.execv("/bin/sh", ["sh", "-c", "echo fixed"])\n')
        assert self.rule.check(ctx) == []

    # ------------------------------------------------ 形态 c：run/call/check_output 列表 -c 中转

    def test_positive_run_sh_c_list(self, make_ctx):
        ctx = make_ctx('import subprocess\n\nsubprocess.run(["sh", "-c", cmd])\n')
        assert _lines(self.rule, ctx) == [3]

    def test_positive_run_sh_c_list_with_check(self, make_ctx):
        """带 check=True 不影响注入判定：-c 中转的动态命令仍等价命令注入。"""
        ctx = make_ctx('import subprocess\n\nsubprocess.run(["sh", "-c", cmd], check=True)\n')
        assert _lines(self.rule, ctx) == [3]

    def test_positive_check_output_bash_c_list(self, make_ctx):
        ctx = make_ctx('import subprocess\n\nsubprocess.check_output(["bash", "-c", tgt])\n')
        assert _lines(self.rule, ctx) == [3]

    def test_negative_run_plain_list(self, make_ctx):
        """run 无 shell=True 时列表参数本身安全：纯列表（ls -l）不报。"""
        ctx = make_ctx('import subprocess\n\nsubprocess.run(["ls", "-l"])\n')
        assert self.rule.check(ctx) == []

    def test_negative_run_fixed_payload_sh_c(self, make_ctx):
        """列表走 sh -c 但负载为纯字面量（无变量/拼接）不报。"""
        ctx = make_ctx('import subprocess\n\nsubprocess.run(["sh", "-c", "echo fixed"])\n')
        assert self.rule.check(ctx) == []

    def test_negative_run_var_no_shell(self, make_ctx):
        """变量串参数且无 shell=True：保持既有语义（归 PY-SUBPROCESS-WITHOUT-CHECK 管辖）不报。"""
        ctx = make_ctx("import subprocess\n\nsubprocess.run(cmd, check=True)\n")
        assert self.rule.check(ctx) == []

    def test_negative_run_list_var_no_shell(self, make_ctx):
        """变量数组参数 + shell=False：数组形式不进 shell，不报。"""
        ctx = make_ctx("import subprocess\n\nsubprocess.run(args, shell=False, check=True)\n")
        assert self.rule.check(ctx) == []

    # ------------------------------------------------ 误报红线：clean 语料 orders.py 片段

    def test_negative_clean_corpus_orders_snippet(self, make_ctx):
        """clean_corpus/pkg/orders.py 的 run(command, ..., check=True) 列表参数不得报。"""
        ctx = make_ctx(
            "import subprocess\n\n"
            "def run_sync(command: list[str]) -> str:\n"
            "    proc = subprocess.run(\n"
            "        command, capture_output=True, text=True, encoding=\"utf-8\", check=True\n"
            "    )\n"
            "    return proc.stdout.strip()\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_run_list_with_env_kwarg_fp(self, make_ctx):
        """列表内无 -c 中转时，kwargs 里的变量（env=...）不得触发列表中转判定。"""
        ctx = make_ctx(
            'import subprocess\n\nsubprocess.run(["sh", "script.sh"], env={"A": b})\n'
        )
        assert self.rule.check(ctx) == []


class TestSqlInjectionW20D:
    """W20-D SQL 注入二级传播：SQL 常量模板（tmpl/PREFIX 式）的跨行直接拼接使用。"""

    rule = SqlInjectionConcatRule()

    def test_positive_const_template_format(self, make_ctx):
        """q3 场景：函数内 tmpl 常量模板 + 下行 .format → 归因使用行命中。"""
        ctx = make_ctx(
            "def q3(cur, name):\n"
            "    tmpl = \"SELECT * FROM u WHERE name = '{}'\"\n"
            "    sql = tmpl.format(name)\n"
            "    cur.execute(sql)\n"
        )
        assert _lines(self.rule, ctx) == [3]

    def test_positive_module_prefix_plus(self, make_ctx):
        """q5 场景：模块级 PREFIX 常量 + 函数内 execute(PREFIX + x) → 归因使用行。"""
        ctx = make_ctx(
            'PREFIX = "SELECT * FROM t WHERE flag = "\n'
            "\n"
            "def q5(cur, x):\n"
            "    cur.execute(PREFIX + x)\n"
        )
        assert _lines(self.rule, ctx) == [4]

    def test_positive_const_plus_before(self, make_ctx):
        """``… + CONST`` 形态（常量在加号右侧）同样命中使用行。"""
        ctx = make_ctx(
            'HEAD = "SELECT * FROM t WHERE flag = "\n'
            "query = other + HEAD\n"
        )
        assert _lines(self.rule, ctx) == [2]

    def test_positive_const_fstring_interpolation(self, make_ctx):
        """f-string 插值 {CONST} 形态命中使用行（掩码行看不到插值，须查原始跨度）。"""
        ctx = make_ctx(
            'BASE = "SELECT * FROM t WHERE flag = "\n'
            'q = f"{BASE}{x}"\n'
        )
        assert _lines(self.rule, ctx) == [2]

    def test_negative_const_template_parameterized(self, make_ctx):
        """误报红线：常量模板走参数化 execute（使用行无拼接形态）不报。"""
        ctx = make_ctx(
            'sql = "SELECT * FROM t WHERE id = ?"\n'
            "cur.execute(sql, (uid,))\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_const_sql_without_concat(self, make_ctx):
        """常量 SQL 纯引用（无任何拼接形态）不报。"""
        ctx = make_ctx(
            'sql = "SELECT * FROM t WHERE id = 1"\n'
            "cur.execute(sql)\n"
        )
        assert self.rule.check(ctx) == []

    def test_negative_multi_step_chain_not_tracked(self, make_ctx):
        """单步口径：sql→sql2→execute 多步链只按既有单行口径报首行，链路不追踪。"""
        ctx = make_ctx(
            'sql = "SELECT * FROM t WHERE a = " + a\n'
            'sql2 = sql + " LIMIT 10"\n'
            "cur.execute(sql2)\n"
        )
        assert _lines(self.rule, ctx) == [1]

    def test_negative_clean_corpus_repo_snippet(self, make_ctx):
        """clean_corpus/pkg/repo.py 摘录：内联参数化查询 0 命中（误报红线）。"""
        ctx = make_ctx(
            "class Repository:\n"
            "    def __init__(self, conn):\n"
            "        self._conn = conn\n"
            "\n"
            "    def find_user(self, user_id: int):\n"
            '        row = self._conn.execute("SELECT id, name FROM users WHERE id = ?", (user_id,)).fetchone()\n'
            "        if row is None:\n"
            "            return None\n"
            '        return {"id": row[0], "name": row[1]}\n'
        )
        assert self.rule.check(ctx) == []

    def test_negative_keyword_only_in_comment_const_style(self, make_ctx):
        """常量名拼接但 SQL 关键字只在注释/非字面量处 → 不收集、不命中。"""
        ctx = make_ctx(
            "# SELECT * FROM t 模板见文档\n"
            "tmpl = load_template()\n"
            "query = tmpl + suffix\n"
        )
        assert self.rule.check(ctx) == []
