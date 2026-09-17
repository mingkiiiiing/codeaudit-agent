"""扩充第二期规则正反用例（W6-A3：PY 8 条 + JS/TS 6 条）+ 规则手册生成器测试。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from audit.detect.registry import DEFAULT_REGISTRY
from audit.detect.rules.js.js_ext import (
    AwaitInLoopRule,
    DocumentCookieWriteRule,
    DoubleEqNullRule,
    EmptyCatchRule,
    LocalStorageSensitiveRule,
    TsDependsOnAnyRule,
)
from audit.detect.rules.python_ext import (
    AssertTupleRule,
    GlobalStateMutateRule,
    MutableClassAttrRule,
    OpenWithoutEncodingRule,
    ReturnInInitRule,
    StringFormatInLoggingRule,
    SubprocessWithoutCheckRule,
    TypeCompareRule,
)

ROOT = Path(__file__).resolve().parents[3]

NEW_PY_RULE_IDS = [
    "PY-SUBPROCESS-WITHOUT-CHECK",
    "PY-OPEN-WITHOUT-ENCODING",
    "PY-ASSERT-TUPLE",
    "PY-MUTABLE-CLASS-ATTR",
    "PY-TYPE-COMPARE",
    "PY-STRING-FORMAT-IN-LOGGING",
    "PY-RETURN-IN-INIT",
    "PY-GLOBAL-STATE-MUTATE",
]
NEW_JS_RULE_IDS = [
    "JS-AWAIT-IN-LOOP",
    "JS-DOUBLE-EQ-NULL",
    "JS-EMPTY-CATCH",
    "JS-LOCALSTORAGE-SENSITIVE",
    "JS-DOCUMENT-COOKIE-WRITE",
    "TS-DEPENDS-ON-ANY",
]
BASELINE_RULE_COUNT = 49
# W16（专项验收短板清偿）追加注册的规则数：arch_layers 2 + py_naming 2 +
# pii_rules 2（PY-PII-LOG / PY-PII-SQL）+ js_security_ext 2；
# W19（深度审计 P1 清偿）追加 7 条：py_complexity 1 + py_concurrency 2 +
# py_orm 1 + py_dynamic_exec 3；规则库 63 -> 78，见 CHANGELOG Wave 16/19。
W16_RULE_COUNT = 8
W19_RULE_COUNT = 7
# W20（P2 清偿）追加：py_security_ops 2（PY-DEFAULT-CREDENTIAL / PY-LOG-FORGERY）；
# 规则库 63 -> 80，见 CHANGELOG Wave 20。
W20_RULE_COUNT = 2
# W21（门禁清白配套）追加：py_web_routing 2（PY-WEB-ROUTE-NO-AUTH / PY-WEB-NO-RATE-LIMIT）；
# 规则库 80 -> 82，见 CHANGELOG W21。
W21_RULE_COUNT = 2
# W23（工作轮 P0-3 AST 断供修复）追加：py_none_deref 1（PY-NONE-DEREF）；
# 规则库 82 -> 83，见 CHANGELOG Wave 23。
W23_RULE_COUNT = 1
# W24-A（Java 语言包）追加：java 3（JAVA-SQL-INJECTION / JAVA-HARDCODED-SECRET /
# JAVA-LONG-FUNCTION）；规则库 83 -> 86。
W24_RULE_COUNT = 3


def _lines(rule, ctx):
    return [h.line_start for h in rule.check(ctx)]


def _js(make_ctx, source: str, rel_path: str = "m.js"):
    return make_ctx(source, rel_path=rel_path, language="javascript")


def _ts(make_ctx, source: str, rel_path: str = "m.ts"):
    return make_ctx(source, rel_path=rel_path, language="typescript")


# ---------------------------------------------------------------- Python 扩充规则


class TestSubprocessWithoutCheck:
    rule = SubprocessWithoutCheckRule()

    def test_positive_module_run(self, make_ctx):
        ctx = make_ctx("import subprocess\nsubprocess.run(['ls'])\n")
        assert _lines(self.rule, ctx) == [2]

    def test_positive_module_call(self, make_ctx):
        ctx = make_ctx("import subprocess\nrc = subprocess.call(['ls'])\n")
        assert _lines(self.rule, ctx) == [2]

    def test_positive_from_import(self, make_ctx):
        ctx = make_ctx("from subprocess import run\ndef main():\n    run(['git', 'push'])\n")
        assert _lines(self.rule, ctx) == [3]

    def test_negative_check_true(self, make_ctx):
        ctx = make_ctx("import subprocess\nsubprocess.run(['ls'], check=True)\n")
        assert self.rule.check(ctx) == []

    def test_negative_check_call_raises_by_itself(self, make_ctx):
        ctx = make_ctx("import subprocess\nsubprocess.check_call(['ls'])\nsubprocess.check_output(['ls'])\n")
        assert self.rule.check(ctx) == []

    def test_negative_unrelated_run_call(self, make_ctx):
        ctx = make_ctx("class P:\n    def run(self):\n        return 1\np = P()\np.run()\n")
        assert self.rule.check(ctx) == []

    def test_negative_in_string(self, make_ctx):
        ctx = make_ctx("msg = 'subprocess.runwithout check'\n")
        assert self.rule.check(ctx) == []


class TestOpenWithoutEncoding:
    rule = OpenWithoutEncodingRule()

    def test_positive_with_open(self, make_ctx):
        ctx = make_ctx("with open('cfg.json') as f:\n    data = f.read()\n")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_assign_open(self, make_ctx):
        ctx = make_ctx("f = open(path)\n")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_explicit_encoding(self, make_ctx):
        ctx = make_ctx("f = open(path, encoding='utf-8')\n")
        assert self.rule.check(ctx) == []

    def test_negative_binary_mode(self, make_ctx):
        ctx = make_ctx("f = open(path, 'rb')\ng = open(path, 'wb+')\n")
        assert self.rule.check(ctx) == []

    def test_negative_method_open(self, make_ctx):
        ctx = make_ctx("f = archive.open(name)\n")
        assert self.rule.check(ctx) == []


class TestAssertTuple:
    rule = AssertTupleRule()

    def test_positive_if_tuple(self, make_ctx):
        ctx = make_ctx("if (a, b):\n    pass\n")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_while_tuple(self, make_ctx):
        ctx = make_ctx("while (x, y):\n    break\n")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_elif_tuple(self, make_ctx):
        ctx = make_ctx("if a:\n    pass\nelif (b, c):\n    pass\n")
        assert _lines(self.rule, ctx) == [3]

    def test_negative_tuple_comparison(self, make_ctx):
        ctx = make_ctx("if (a, b) == other:\n    pass\n")
        assert self.rule.check(ctx) == []

    def test_negative_plain_condition(self, make_ctx):
        ctx = make_ctx("if a and b:\n    pass\nwhile (i < n):\n    i += 1\n")
        assert self.rule.check(ctx) == []

    def test_negative_in_string(self, make_ctx):
        ctx = make_ctx("doc = 'if (a, b):'\n")
        assert self.rule.check(ctx) == []


class TestMutableClassAttr:
    rule = MutableClassAttrRule()

    def test_positive_list_dict_set(self, make_ctx):
        ctx = make_ctx(
            "class Basket:\n"
            "    items = []\n"
            "    cfg = {}\n"
            "    tags = set()\n"
            "\n"
            "    def add(self, x):\n"
            "        self.items.append(x)\n"
        )
        assert _lines(self.rule, ctx) == [2, 3, 4]

    def test_positive_annotated(self, make_ctx):
        ctx = make_ctx("class C:\n    buf: list = []\n")
        assert _lines(self.rule, ctx) == [2]

    def test_negative_instance_attr_in_init(self, make_ctx):
        ctx = make_ctx("class C:\n    def __init__(self):\n        self.items = []\n")
        assert self.rule.check(ctx) == []

    def test_negative_immutable_class_attr(self, make_ctx):
        ctx = make_ctx("class C:\n    name = 'x'\n    count = 0\n")
        assert self.rule.check(ctx) == []


class TestTypeCompare:
    rule = TypeCompareRule()

    def test_positive_eq_types(self, make_ctx):
        ctx = make_ctx("if type(x) == type(y):\n    pass\n")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_is_type(self, make_ctx):
        ctx = make_ctx("if type(x) is dict:\n    pass\n")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_ne_types(self, make_ctx):
        ctx = make_ctx("while type(x) != type(y):\n    break\n")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_isinstance(self, make_ctx):
        ctx = make_ctx("if isinstance(x, dict):\n    pass\n")
        assert self.rule.check(ctx) == []

    def test_negative_in_string(self, make_ctx):
        ctx = make_ctx("hint = 'type(x) == type(y)'\n")
        assert self.rule.check(ctx) == []


class TestStringFormatInLogging:
    rule = StringFormatInLoggingRule()

    def test_positive_percent(self, make_ctx):
        ctx = make_ctx("import logging\nlogger = logging.getLogger(__name__)\nlogger.info('got %d items' % n)\n")
        assert _lines(self.rule, ctx) == [3]

    def test_positive_format(self, make_ctx):
        ctx = make_ctx("logger.info('user {}'.format(u))\n")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_fstring(self, make_ctx):
        ctx = make_ctx("logger.warning(f'latency {latency}ms')\n")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_lazy_args(self, make_ctx):
        ctx = make_ctx("logger.info('got %d items', n)\nlogger.error('user %s', u)\n")
        assert self.rule.check(ctx) == []

    def test_negative_percent_inside_string(self, make_ctx):
        ctx = make_ctx("logger.info('100% sure')\n")
        assert self.rule.check(ctx) == []

    def test_negative_non_logging_call(self, make_ctx):
        ctx = make_ctx("text = 'user {}'.format(u)\n")
        assert self.rule.check(ctx) == []


class TestReturnInInit:
    rule = ReturnInInitRule()

    def test_positive_return_self(self, make_ctx):
        ctx = make_ctx("class C:\n    def __init__(self):\n        self.x = 1\n        return self\n")
        assert _lines(self.rule, ctx) == [4]

    def test_positive_return_value(self, make_ctx):
        ctx = make_ctx("class C:\n    def __init__(self):\n        return compute()\n")
        assert _lines(self.rule, ctx) == [3]

    def test_negative_return_none(self, make_ctx):
        ctx = make_ctx("class C:\n    def __init__(self):\n        return None\n")
        assert self.rule.check(ctx) == []

    def test_negative_bare_return(self, make_ctx):
        ctx = make_ctx("class C:\n    def __init__(self):\n        if bad:\n            return\n        self.x = 1\n")
        assert self.rule.check(ctx) == []

    def test_negative_other_method(self, make_ctx):
        ctx = make_ctx("class C:\n    def build(self):\n        return self\n")
        assert self.rule.check(ctx) == []


class TestGlobalStateMutate:
    rule = GlobalStateMutateRule()

    def test_positive_rebind(self, make_ctx):
        ctx = make_ctx("cache = {}\n\ndef reset():\n    global cache\n    cache = {}\n")
        assert _lines(self.rule, ctx) == [5]

    def test_positive_augmented_assign(self, make_ctx):
        ctx = make_ctx("counter = 0\n\ndef bump():\n    global counter\n    counter += 1\n")
        assert _lines(self.rule, ctx) == [5]

    def test_negative_read_only_global(self, make_ctx):
        ctx = make_ctx("cache = {}\n\ndef reader():\n    global cache\n    return cache\n")
        assert self.rule.check(ctx) == []

    def test_negative_constant_rebind(self, make_ctx):
        # 全大写按常量约定豁免（改常量属另一类问题，不归入"可变模块状态"）
        ctx = make_ctx("CONFIG = None\n\ndef setup():\n    global CONFIG\n    CONFIG = 1\n")
        assert self.rule.check(ctx) == []

    def test_negative_local_shadow(self, make_ctx):
        ctx = make_ctx("cache = {}\n\ndef other():\n    cache = {}\n    return cache\n")
        assert self.rule.check(ctx) == []


# ---------------------------------------------------------------- JS/TS 扩充规则


class TestAwaitInLoop:
    rule = AwaitInLoopRule()

    def test_positive_for(self, make_ctx):
        ctx = _js(make_ctx, "async function f(ids) {\n  for (const id of ids) {\n    await load(id);\n  }\n}\n")
        assert _lines(self.rule, ctx) == [3]

    def test_positive_while(self, make_ctx):
        ctx = _js(make_ctx, "async function f(q) {\n  while (q.length) {\n    const x = await next(q);\n  }\n}\n")
        assert _lines(self.rule, ctx) == [3]

    def test_negative_await_outside_loop(self, make_ctx):
        ctx = _js(make_ctx, "async function f(ids) {\n  const all = await Promise.all(ids);\n  return all;\n}\n")
        assert self.rule.check(ctx) == []

    def test_negative_await_in_callback_declared_in_loop(self, make_ctx):
        ctx = _js(
            make_ctx,
            "async function f(as) {\n  for (const a of as) {\n    as.map(async (x) => await g(x));\n  }\n}\n",
        )
        assert self.rule.check(ctx) == []

    def test_negative_for_await_header_not_reported(self, make_ctx):
        ctx = _js(
            make_ctx,
            "async function f(stream) {\n  for await (const chunk of stream) {\n    handle(chunk);\n  }\n}\n",
        )
        assert self.rule.check(ctx) == []


class TestDoubleEqNull:
    rule = DoubleEqNullRule()

    def test_positive_eq_null(self, make_ctx):
        ctx = _js(make_ctx, "if (value == null) {\n  value = 0;\n}\n")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_null_ne(self, make_ctx):
        ctx = _js(make_ctx, "if (null != value) {}\n")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_strict_eq(self, make_ctx):
        ctx = _js(make_ctx, "if (a === null) {}\nif (a !== null) {}\n")
        assert self.rule.check(ctx) == []

    def test_negative_word_containing_null(self, make_ctx):
        ctx = _js(make_ctx, "if (a == nulled) {}\n")
        assert self.rule.check(ctx) == []

    def test_negative_null_in_string(self, make_ctx):
        ctx = _js(make_ctx, "const msg = 'a == null';\n")
        assert self.rule.check(ctx) == []


class TestLocalStorageSensitive:
    rule = LocalStorageSensitiveRule()

    def test_positive_string_key(self, make_ctx):
        ctx = _js(make_ctx, "localStorage.setItem('access_token', token);\n")
        assert _lines(self.rule, ctx) == [1]
        assert self.rule.check(ctx)[0].meta["key"] == "access_token"

    def test_positive_identifier_key(self, make_ctx):
        ctx = _js(make_ctx, "localStorage.setItem(apiKey, value);\n")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_password_key(self, make_ctx):
        ctx = _js(make_ctx, "localStorage.setItem('db_password', pwd);\n")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_plain_key(self, make_ctx):
        ctx = _js(make_ctx, "localStorage.setItem('theme', 'dark');\n")
        assert self.rule.check(ctx) == []

    def test_negative_get_item(self, make_ctx):
        ctx = _js(make_ctx, "const t = localStorage.getItem('token');\n")
        assert self.rule.check(ctx) == []

    def test_shared_with_typescript(self, make_ctx):
        ctx = _ts(make_ctx, "localStorage.setItem('auth_token', token);\n")
        assert _lines(self.rule, ctx) == [1]


class TestEmptyCatch:
    rule = EmptyCatchRule()

    def test_positive_inline_empty(self, make_ctx):
        ctx = _js(make_ctx, "try {\n  a();\n} catch (e) {}\n")
        assert _lines(self.rule, ctx) == [3]

    def test_positive_multiline_empty(self, make_ctx):
        ctx = _js(make_ctx, "try {\n  b();\n} catch {\n}\n")
        assert _lines(self.rule, ctx) == [3]

    def test_negative_with_body(self, make_ctx):
        ctx = _js(make_ctx, "try {\n  b();\n} catch (e) {\n  log(e);\n}\n")
        assert self.rule.check(ctx) == []

    def test_negative_one_line_with_body(self, make_ctx):
        ctx = _js(make_ctx, "try { c(); } catch (e) { handle(e); }\n")
        assert self.rule.check(ctx) == []


class TestDocumentCookieWrite:
    rule = DocumentCookieWriteRule()

    def test_positive_concat(self, make_ctx):
        ctx = _js(make_ctx, "document.cookie = 'session=' + token + '; path=/';\n")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_template(self, make_ctx):
        ctx = _js(make_ctx, "document.cookie = `a=${value}`;\n")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_variable(self, make_ctx):
        ctx = _js(make_ctx, "document.cookie = cookieText;\n")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_constant(self, make_ctx):
        ctx = _js(make_ctx, "document.cookie = 'a=b; path=/';\ndocument.cookie = '';\n")
        assert self.rule.check(ctx) == []

    def test_shared_with_typescript(self, make_ctx):
        ctx = _ts(make_ctx, "document.cookie = 'k=' + v;\n")
        assert _lines(self.rule, ctx) == [1]


class TestTsDependsOnAny:
    rule = TsDependsOnAnyRule()

    def test_positive_missing_return_type(self, make_ctx):
        ctx = _ts(make_ctx, "function transform(raw: any) {\n  return JSON.parse(raw);\n}\n")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_exported(self, make_ctx):
        ctx = _ts(make_ctx, "export function build(input: any) {\n  return input;\n}\n")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_annotated_return(self, make_ctx):
        ctx = _ts(
            make_ctx,
            "function ok(raw: any): Record<string, unknown> {\n  return JSON.parse(raw);\n}\n",
        )
        assert self.rule.check(ctx) == []

    def test_negative_no_any_param(self, make_ctx):
        ctx = _ts(make_ctx, "function fine(raw: string) {\n  return raw;\n}\n")
        assert self.rule.check(ctx) == []

    def test_negative_overload_declaration(self, make_ctx):
        ctx = _ts(make_ctx, "declare function f(x: any): void;\n")
        assert self.rule.check(ctx) == []


# ---------------------------------------------------------------- 注册表契约


class TestRegistryExt:
    def test_total_count_is_baseline_plus_ext(self):
        assert len(DEFAULT_REGISTRY) == BASELINE_RULE_COUNT + len(NEW_PY_RULE_IDS) + len(NEW_JS_RULE_IDS) + W16_RULE_COUNT + W19_RULE_COUNT + W20_RULE_COUNT + W21_RULE_COUNT + W23_RULE_COUNT + W24_RULE_COUNT

    def test_every_new_rule_registered_exactly_once(self):
        all_ids = [r.id for r in DEFAULT_REGISTRY.all_rules]
        for rule_id in NEW_PY_RULE_IDS + NEW_JS_RULE_IDS:
            assert all_ids.count(rule_id) == 1, f"{rule_id} 注册次数异常"

    def test_python_ext_rules_route_to_python_only(self):
        for rule in DEFAULT_REGISTRY.all_rules:
            if rule.id in NEW_PY_RULE_IDS:
                assert rule.languages == ("python",)
                assert rule.check is not None

    def test_ts_rule_routes_to_typescript_only(self):
        rule = next(r for r in DEFAULT_REGISTRY.all_rules if r.id == "TS-DEPENDS-ON-ANY")
        assert rule.languages == ("typescript",)

    def test_security_ext_rules_shared_between_js_and_ts(self):
        shared_ids = {"JS-LOCALSTORAGE-SENSITIVE", "JS-DOCUMENT-COOKIE-WRITE"}
        for rule in DEFAULT_REGISTRY.all_rules:
            if rule.id in shared_ids:
                assert set(rule.languages) == {"javascript", "typescript"}

    def test_ext_rules_declare_examples(self):
        # 契约 v1.6：扩充规则必须带正反示例供手册生成
        for rule in DEFAULT_REGISTRY.all_rules:
            if rule.id in NEW_PY_RULE_IDS + NEW_JS_RULE_IDS:
                assert getattr(rule, "good_example", "").strip(), f"{rule.id} 缺 good_example"
                assert getattr(rule, "bad_example", "").strip(), f"{rule.id} 缺 bad_example"


# ---------------------------------------------------------------- 规则手册生成器


def _build_markdown():
    import importlib.util

    spec = importlib.util.spec_from_file_location("gen_rule_docs", ROOT / "scripts" / "gen_rule_docs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestGenRuleDocs:
    def test_markdown_contains_every_new_rule(self, capsys):
        module = _build_markdown()
        from audit.detect.registry import get_registry

        content = module.build_markdown(get_registry())
        for rule_id in NEW_PY_RULE_IDS + NEW_JS_RULE_IDS:
            assert f"### {rule_id}" in content, f"明细缺少 {rule_id}"
            assert f"| {rule_id} |" in content, f"总表缺少 {rule_id}"

    def test_markdown_structure(self):
        module = _build_markdown()
        from audit.detect.registry import get_registry

        content = module.build_markdown(get_registry())
        assert content.startswith("# 规则手册")
        assert "## 规则总表" in content
        assert "## 规则明细" in content
        assert "## 统计" in content
        # 扩充规则全部带正反示例 → 一定渲染代码块
        assert "**反例**" in content and "**正例**" in content
        assert "```python" in content and "```javascript" in content

    def test_markdown_is_deterministic(self):
        module = _build_markdown()
        from audit.detect.registry import get_registry

        first = module.build_markdown(get_registry())
        second = module.build_markdown(get_registry())
        assert first == second

    def test_cli_writes_idempotent_file(self, tmp_path):
        out1 = tmp_path / "rules1.md"
        out2 = tmp_path / "rules2.md"
        script = ROOT / "scripts" / "gen_rule_docs.py"
        for out in (out1, out2):
            proc = subprocess.run(
                [sys.executable, str(script), "--out", str(out)],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            assert proc.returncode == 0, proc.stderr
        content1 = out1.read_text(encoding="utf-8")
        content2 = out2.read_text(encoding="utf-8")
        assert content1 == content2
        for rule_id in NEW_PY_RULE_IDS + NEW_JS_RULE_IDS:
            assert rule_id in content1

    def test_cli_default_out_is_docs_site_rules_md(self):
        target = ROOT / "docs-site" / "rules.md"
        assert target.exists(), "docs-site/rules.md 应随仓库提交"
        content = target.read_text(encoding="utf-8")
        assert "PY-SUBPROCESS-WITHOUT-CHECK" in content
        assert "TS-DEPENDS-ON-ANY" in content


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
