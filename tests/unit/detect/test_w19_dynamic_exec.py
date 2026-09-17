"""W19-D 动态执行/导入四形态规则（py_dynamic_exec.py）正反用例。

正例覆盖：W18 深度审计漏报语料 sec_variants/v_exec.py 的原样形态——
e1 `getattr(builtins, "eval")(src)`（low 标疑）、e2 `importlib.import_module(mod)`、
e3 `__import__(mod)`、e4 `compile(src, "<s>", "eval")`、e6 `globals()[m]()`
（变量下标动态分发），以及 f-string 插值、`globals()["eval"]` 危险名字面量下标。

红线反例（误报高危点）：`importlib.import_module("pkg")` 纯字面量（静态导入
声明）、`__import__("os")` 纯字面量、`re.compile(r"\\d+")` 点号形态（正则编译
日常用法，绝不报）、`compile("1+1", ...)` 纯字面量、`getattr(obj, "name")`
正常属性、`getattr(cfg, "openai_key")` 词边界软边界、`globals()["config"]`
良性下标、字符串/注释内的假调用、正常 import 语句、clean 语料摘录。
"""

from __future__ import annotations

from audit.detect.rules.py_dynamic_exec import (
    DynamicCompileRule,
    DynamicImportRule,
    IndirectExecRule,
    build_dynamic_exec_rules,
)


def _lines(rule, ctx):
    return [h.line_start for h in rule.check(ctx)]


class TestDynamicImport:
    rule = DynamicImportRule()

    def test_import_dunder_variable_hit(self, make_ctx):
        # v_exec.py e3 原样形态：__import__ 首参为变量
        ctx = make_ctx(
            "def e3(mod):\n"
            "    return __import__(mod)\n"
        )
        hits = self.rule.check(ctx)
        assert [h.line_start for h in hits] == [2]
        assert hits[0].meta["api"] == "__import__"

    def test_import_dunder_literal_no_hit(self, make_ctx):
        # 首参纯字面量：等价静态导入声明，不报
        ctx = make_ctx('mod = __import__("os")\n')
        assert self.rule.check(ctx) == []

    def test_import_module_variable_hit(self, make_ctx):
        # v_exec.py e2 原样形态：importlib.import_module 首参为变量
        ctx = make_ctx(
            "import importlib\n"
            "\n"
            "def e2(mod):\n"
            "    return importlib.import_module(mod)\n"
        )
        hits = self.rule.check(ctx)
        assert [h.line_start for h in hits] == [4]
        assert hits[0].meta["api"] == "importlib.import_module"

    def test_import_module_literal_no_hit(self, make_ctx):
        # 误报高危点：框架插件加载的静态字面量形态不报
        ctx = make_ctx('plugin = importlib.import_module("myapp.plugins.base")\n')
        assert self.rule.check(ctx) == []

    def test_import_module_concat_hit(self, make_ctx):
        # 首参拼接（"myapp.plugins." + name）属动态导入
        ctx = make_ctx('plugin = importlib.import_module("myapp.plugins." + name)\n')
        assert [h.line_start for h in self.rule.check(ctx)] == [1]

    def test_import_module_fstring_interp_hit(self, make_ctx):
        # f-string 含 {} 插值视为动态
        ctx = make_ctx('plugin = importlib.import_module(f"myapp.plugins.{name}")\n')
        assert [h.line_start for h in self.rule.check(ctx)] == [1]

    def test_masked_string_and_comment_no_hit(self, make_ctx):
        # 字符串/注释中的假调用不报（掩码行口径）
        ctx = make_ctx(
            'doc = "use __import__(mod) or importlib.import_module(mod) here"\n'
            "# __import__(mod)\n"
        )
        assert self.rule.check(ctx) == []

    def test_plain_import_statement_no_hit(self, make_ctx):
        # 正常 import 语句不报
        ctx = make_ctx(
            "import importlib\n"
            "import builtins\n"
            "from importlib import import_module\n"
        )
        assert self.rule.check(ctx) == []


class TestDynamicCompile:
    rule = DynamicCompileRule()

    def test_compile_variable_hit(self, make_ctx):
        # v_exec.py e4 原样形态：compile 首参为变量
        ctx = make_ctx(
            "def e4(src):\n"
            "    return compile(src, \"<s>\", \"eval\")\n"
        )
        assert [h.line_start for h in self.rule.check(ctx)] == [2]

    def test_compile_literal_no_hit(self, make_ctx):
        # 首参纯字面量：内容静态可审计，不报
        ctx = make_ctx('code = compile("1+1", "<string>", "eval")\n')
        assert self.rule.check(ctx) == []

    def test_re_compile_no_hit(self, make_ctx):
        # 关键反例：re.compile 点号形态（正则编译）绝不报
        ctx = make_ctx(
            "import re\n"
            "PATTERN = re.compile(r\"\\d+\")\n"
            "raw = re.compile(\"[a-z]+\", re.IGNORECASE)\n"
        )
        assert self.rule.check(ctx) == []

    def test_compile_in_string_no_hit(self, make_ctx):
        ctx = make_ctx("doc = 'call compile(src, \"<s>\", \"eval\") to compile'\n")
        assert self.rule.check(ctx) == []


class TestIndirectExec:
    rule = IndirectExecRule()

    def test_getattr_dangerous_hit_low(self, make_ctx):
        # v_exec.py e1 原样形态：getattr(builtins, "eval") → low 标疑
        ctx = make_ctx(
            "import builtins\n"
            "\n"
            "def e1(src):\n"
            "    return getattr(builtins, \"eval\")(src)\n"
        )
        hits = self.rule.check(ctx)
        assert [h.line_start for h in hits] == [4]
        assert hits[0].severity.value == "low"
        assert hits[0].meta["names"] == "eval"
        assert "标疑" in hits[0].message and "人工确认" in hits[0].message

    def test_getattr_normal_attr_no_hit(self, make_ctx):
        # 正常属性访问不报
        ctx = make_ctx('name = getattr(obj, "name")\n')
        assert self.rule.check(ctx) == []

    def test_getattr_word_boundary_no_hit(self, make_ctx):
        # 词边界口径：含危险词片段的普通名不误报
        ctx = make_ctx(
            'key = getattr(settings, "openai_key")\n'
            'helper = getattr(utils, "import_helper")\n'
        )
        assert self.rule.check(ctx) == []

    def test_globals_dangerous_literal_hit(self, make_ctx):
        # globals()["eval"] 危险名字面量下标 → 标疑
        ctx = make_ctx('fn = globals()["eval"]\n')
        hits = self.rule.check(ctx)
        assert [h.line_start for h in hits] == [1]
        assert hits[0].meta["names"] == "eval"
        assert hits[0].severity.value == "low"

    def test_globals_benign_literal_no_hit(self, make_ctx):
        # 良性字面量下标不报（即使紧跟调用）
        ctx = make_ctx(
            'cfg = globals()["config"]\n'
            'obj = globals()["app"](request)\n'
        )
        assert self.rule.check(ctx) == []

    def test_globals_variable_dispatch_hit(self, make_ctx):
        # v_exec.py e6 原样形态：变量下标取值后立即调用（动态分发）
        ctx = make_ctx(
            "def e6(m):\n"
            "    globals()[m]()\n"
        )
        hits = self.rule.check(ctx)
        assert [h.line_start for h in hits] == [2]
        assert hits[0].meta["kind"] == "namespace-dispatch"
        assert hits[0].severity.value == "low"

    def test_locals_variable_dispatch_hit(self, make_ctx):
        ctx = make_ctx(
            "def e7(fn):\n"
            "    locals()[fn]()\n"
        )
        assert [h.line_start for h in self.rule.check(ctx)] == [2]

    def test_globals_variable_lookup_without_call_no_hit(self, make_ctx):
        # 变量下标纯取值（查表）不报：仅"取值后立即调用"才标疑
        ctx = make_ctx("val = globals()[key]\n")
        assert self.rule.check(ctx) == []


class TestCleanCorpusExcerpt:
    """clean 语料摘录（orders.py / repo.py 关键行）→ 三条规则全 0 命中。"""

    def test_clean_orders_excerpt(self, make_ctx):
        ctx = make_ctx(
            "import json\n"
            "import logging\n"
            "import subprocess\n"
            "\n"
            "logger = logging.getLogger(__name__)\n"
            "\n"
            "def summarize(orders):\n"
            "    logger.info(\"summarized orders total=%s count=%s\", total, len(orders))\n"
            "\n"
            "def run_sync(command):\n"
            "    proc = subprocess.run(command, capture_output=True, text=True, check=True)\n"
            "    return proc.stdout.strip()\n"
        )
        assert all(r.check(ctx) == [] for r in build_dynamic_exec_rules())

    def test_clean_repo_excerpt(self, make_ctx):
        ctx = make_ctx(
            "class Repository:\n"
            "    def find_user(self, user_id):\n"
            "        row = self._conn.execute(\"SELECT id FROM users WHERE id = ?\", (user_id,)).fetchone()\n"
            "        return {\"id\": row[0], \"name\": row[1]}\n"
        )
        assert all(r.check(ctx) == [] for r in build_dynamic_exec_rules())


def test_build_dynamic_exec_rules():
    rules = build_dynamic_exec_rules()
    assert [r.id for r in rules] == [
        "PY-DYNAMIC-IMPORT",
        "PY-DYNAMIC-COMPILE",
        "PY-INDIRECT-EXEC",
    ]
    ids = {r.id: r for r in rules}
    for rule in rules:
        assert rule.languages == ("python",)
        assert rule.category.value == "security"
        assert rule.bad_example.strip() and rule.good_example.strip()
    assert ids["PY-DYNAMIC-IMPORT"].severity.value == "medium"
    assert ids["PY-DYNAMIC-COMPILE"].severity.value == "medium"
    assert ids["PY-INDIRECT-EXEC"].severity.value == "low"
