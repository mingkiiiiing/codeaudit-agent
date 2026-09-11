"""JavaScript 规则正反用例（16 条）+ 共享扫描器行为验证。"""

from __future__ import annotations

from audit.detect.rules.js._js_common import function_ranges, scan_js
from audit.detect.rules.js.javascript import (
    ConsoleLogRule,
    ConstReassignRule,
    DebuggerStatementRule,
    DeepNestingRule,
    DocumentWriteRule,
    EvalExecRule,
    FetchNoTimeoutRule,
    HardcodedSecretRule,
    InnerHtmlAssignRule,
    LooseEqualityRule,
    LongFunctionRule,
    MagicNumberRule,
    SqlConcatRule,
    TimerStringArgRule,
    TodoFixmeCommentRule,
    VarDeclarationRule,
    build_javascript_rules,
)


def _lines(rule, ctx):
    return [h.line_start for h in rule.check(ctx)]


# ---------------------------------------------------------------- bug 类


class TestLooseEquality:
    rule = LooseEqualityRule()

    def test_positive_eq(self, make_ctx):
        ctx = make_ctx("if (a == b) {\n  run();\n}\n", rel_path="m.js", language="javascript")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_ne(self, make_ctx):
        ctx = make_ctx("while (i != n) {\n  i++;\n}\n", rel_path="m.js", language="javascript")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_strict_eq(self, make_ctx):
        ctx = make_ctx("if (a === b) {}\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []

    def test_negative_strict_ne(self, make_ctx):
        ctx = make_ctx("if (a !== b) {}\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []

    def test_negative_lte_gte(self, make_ctx):
        ctx = make_ctx("if (a <= b && c >= d) {}\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []

    def test_negative_in_string(self, make_ctx):
        ctx = make_ctx("const msg = 'a == b';\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []


class TestVarDeclaration:
    rule = VarDeclarationRule()

    def test_positive_var(self, make_ctx):
        ctx = make_ctx("var x = 1;\n", rel_path="m.js", language="javascript")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_for_var(self, make_ctx):
        ctx = make_ctx("for (var i = 0; i < 10; i++) {}\n", rel_path="m.js", language="javascript")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_let_const(self, make_ctx):
        ctx = make_ctx("let x = 1;\nconst y = 2;\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []

    def test_negative_identifier_contains_var(self, make_ctx):
        ctx = make_ctx("const variable = 1;\nlet barvar = 2;\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []

    def test_negative_in_string(self, make_ctx):
        ctx = make_ctx('const s = "var x = 1";\n', rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []


class TestDebuggerStatement:
    rule = DebuggerStatementRule()

    def test_positive(self, make_ctx):
        ctx = make_ctx("function f() {\n  debugger;\n  return 1;\n}\n", rel_path="m.js", language="javascript")
        assert _lines(self.rule, ctx) == [2]

    def test_positive_bare(self, make_ctx):
        ctx = make_ctx("debugger\n", rel_path="m.js", language="javascript")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_property_access(self, make_ctx):
        ctx = make_ctx("const v = obj.debugger;\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []

    def test_negative_in_comment(self, make_ctx):
        ctx = make_ctx("// debugger 语句已移除\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []


class TestTimerStringArg:
    rule = TimerStringArgRule()

    def test_positive_settimeout(self, make_ctx):
        ctx = make_ctx('setTimeout("doThing()", 1000);\n', rel_path="m.js", language="javascript")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_setinterval(self, make_ctx):
        ctx = make_ctx("setInterval('tick()', 500);\n", rel_path="m.js", language="javascript")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_function_ref(self, make_ctx):
        ctx = make_ctx("setTimeout(doThing, 1000);\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []

    def test_negative_arrow(self, make_ctx):
        ctx = make_ctx("setTimeout(() => doThing(), 1000);\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []

    def test_negative_in_string(self, make_ctx):
        ctx = make_ctx('const s = "setTimeout(1, 1000)";\n', rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []


class TestConstReassign:
    rule = ConstReassignRule()

    def test_positive_toplevel(self, make_ctx):
        ctx = make_ctx("const x = 1;\nx = 2;\n", rel_path="m.js", language="javascript")
        assert _lines(self.rule, ctx) == [2]

    def test_positive_in_function(self, make_ctx):
        ctx = make_ctx(
            "function f() {\n  const total = compute();\n  total = 5;\n}\n",
            rel_path="m.js",
            language="javascript",
        )
        assert _lines(self.rule, ctx) == [3]

    def test_positive_compound_assign(self, make_ctx):
        ctx = make_ctx("const n = 1;\nn += 2;\n", rel_path="m.js", language="javascript")
        assert _lines(self.rule, ctx) == [2]

    def test_negative_no_reassign(self, make_ctx):
        ctx = make_ctx("const x = 1;\nconsole.info(x);\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []

    def test_negative_inner_scope(self, make_ctx):
        # 内层作用域的同名赋值属于不同绑定，不报告
        ctx = make_ctx(
            "const x = 1;\nfunction g() {\n  x = 2;\n}\n",
            rel_path="m.js",
            language="javascript",
        )
        assert self.rule.check(ctx) == []

    def test_negative_object_member(self, make_ctx):
        # const 对象的属性赋值合法
        ctx = make_ctx(
            "const obj = {};\nobj.name = 'a';\n",
            rel_path="m.js",
            language="javascript",
        )
        assert self.rule.check(ctx) == []

    def test_negative_let_reassign_ok(self, make_ctx):
        ctx = make_ctx("let x = 1;\nx = 2;\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []


# ---------------------------------------------------------------- security 类


class TestEvalExec:
    rule = EvalExecRule()

    def test_positive_eval(self, make_ctx):
        ctx = make_ctx("const v = eval(userInput);\n", rel_path="m.js", language="javascript")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_new_function(self, make_ctx):
        ctx = make_ctx('const f = new Function("return 1");\n', rel_path="m.js", language="javascript")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_similar_name(self, make_ctx):
        ctx = make_ctx("const v = evaluate(userInput);\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []

    def test_negative_json_parse(self, make_ctx):
        ctx = make_ctx("const v = JSON.parse(text);\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []

    def test_hits_typescript_too(self, make_ctx):
        ctx = make_ctx("const v = eval(src);\n", rel_path="m.ts", language="typescript")
        assert _lines(self.rule, ctx) == [1]


class TestInnerHtml:
    rule = InnerHtmlAssignRule()

    def test_positive_concat(self, make_ctx):
        ctx = make_ctx(
            "el.innerHTML = '<b>' + user.name + '</b>';\n",
            rel_path="m.js",
            language="javascript",
        )
        assert _lines(self.rule, ctx) == [1]

    def test_positive_variable(self, make_ctx):
        ctx = make_ctx("el.innerHTML = userHtml;\n", rel_path="m.js", language="javascript")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_template(self, make_ctx):
        ctx = make_ctx(
            "el.innerHTML = `<div>${user.name}</div>`;\n",
            rel_path="m.js",
            language="javascript",
        )
        assert _lines(self.rule, ctx) == [1]

    def test_positive_multiline_concat(self, make_ctx):
        ctx = make_ctx(
            "el.innerHTML = '<p>' +\n  userContent +\n'</p>';\n",
            rel_path="m.js",
            language="javascript",
        )
        assert _lines(self.rule, ctx) == [1]

    def test_negative_clear(self, make_ctx):
        ctx = make_ctx("el.innerHTML = '';\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []

    def test_negative_constant_markup(self, make_ctx):
        ctx = make_ctx("el.innerHTML = '<b>static</b>';\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []

    def test_negative_text_content(self, make_ctx):
        ctx = make_ctx("el.textContent = user.name;\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []


class TestDocumentWrite:
    rule = DocumentWriteRule()

    def test_positive_write(self, make_ctx):
        ctx = make_ctx('document.write("<h1>hi</h1>");\n', rel_path="m.js", language="javascript")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_writeln(self, make_ctx):
        ctx = make_ctx("document.writeln(line);\n", rel_path="m.js", language="javascript")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_query_selector(self, make_ctx):
        ctx = make_ctx('const el = document.querySelector("#a");\n', rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []

    def test_negative_other_writer(self, make_ctx):
        ctx = make_ctx("writer.write(chunk);\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []


class TestSqlConcat:
    rule = SqlConcatRule()

    def test_positive_plus_concat(self, make_ctx):
        ctx = make_ctx(
            'db.query("SELECT * FROM users WHERE id = " + uid);\n',
            rel_path="m.js",
            language="javascript",
        )
        assert _lines(self.rule, ctx) == [1]

    def test_positive_template_interp(self, make_ctx):
        ctx = make_ctx(
            "db.query(`SELECT * FROM users WHERE name = '${name}'`);\n",
            rel_path="m.js",
            language="javascript",
        )
        assert _lines(self.rule, ctx) == [1]

    def test_negative_parameterized(self, make_ctx):
        ctx = make_ctx(
            'db.query("SELECT * FROM users WHERE id = ?", [uid]);\n',
            rel_path="m.js",
            language="javascript",
        )
        assert self.rule.check(ctx) == []

    def test_negative_plain_string(self, make_ctx):
        # SQL 出现在字符串中但没有拼接，不构成注入
        ctx = make_ctx(
            'const label = "SELECT * FROM users";\n',
            rel_path="m.js",
            language="javascript",
        )
        assert self.rule.check(ctx) == []


class TestHardcodedSecret:
    rule = HardcodedSecretRule()

    def test_positive_api_key(self, make_ctx):
        ctx = make_ctx(
            'const apiKey = "0123456789abcdef0123";\n',
            rel_path="m.js",
            language="javascript",
        )
        assert _lines(self.rule, ctx) == [1]

    def test_positive_sk_prefix(self, make_ctx):
        ctx = make_ctx(
            'const model_key = "sk-9d8f7a6b5c4d3e2f1a0b";\n',
            rel_path="m.js",
            language="javascript",
        )
        assert _lines(self.rule, ctx) == [1]

    def test_positive_object_literal(self, make_ctx):
        ctx = make_ctx(
            'const cfg = { clientSecret: "0123456789abcdef" };\n',
            rel_path="m.js",
            language="javascript",
        )
        assert _lines(self.rule, ctx) == [1]

    def test_negative_env_read(self, make_ctx):
        ctx = make_ctx(
            "const apiKey = process.env.API_KEY;\n",
            rel_path="m.js",
            language="javascript",
        )
        assert self.rule.check(ctx) == []

    def test_negative_short_value(self, make_ctx):
        ctx = make_ctx('const token = "abc123";\n', rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []

    def test_negative_unrelated_long_string(self, make_ctx):
        ctx = make_ctx(
            'const title = "a very long plain title";\n',
            rel_path="m.js",
            language="javascript",
        )
        assert self.rule.check(ctx) == []


# ---------------------------------------------------------------- performance 类


class TestFetchNoTimeout:
    rule = FetchNoTimeoutRule()

    def test_positive_fetch(self, make_ctx):
        ctx = make_ctx("await fetch(url);\n", rel_path="m.js", language="javascript")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_axios_get(self, make_ctx):
        ctx = make_ctx("axios.get('/api/users');\n", rel_path="m.js", language="javascript")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_with_signal(self, make_ctx):
        ctx = make_ctx(
            "fetch(url, { signal: controller.signal });\n",
            rel_path="m.js",
            language="javascript",
        )
        assert self.rule.check(ctx) == []

    def test_negative_with_timeout(self, make_ctx):
        ctx = make_ctx(
            "axios.get('/api/users', { timeout: 3000 });\n",
            rel_path="m.js",
            language="javascript",
        )
        assert self.rule.check(ctx) == []

    def test_negative_in_string(self, make_ctx):
        ctx = make_ctx('const tip = "await fetch(url)";\n', rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []


# ---------------------------------------------------------------- style 类


class TestConsoleLog:
    rule = ConsoleLogRule()

    def test_positive(self, make_ctx):
        ctx = make_ctx('console.log("debug", x);\n', rel_path="src/app.js", language="javascript")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_test_file(self, make_ctx):
        ctx = make_ctx("console.log(x);\n", rel_path="src/app.test.js", language="javascript")
        assert self.rule.check(ctx) == []

    def test_negative_logger(self, make_ctx):
        ctx = make_ctx("logger.log(x);\n", rel_path="src/app.js", language="javascript")
        assert self.rule.check(ctx) == []

    def test_negative_console_info(self, make_ctx):
        ctx = make_ctx("console.info(x);\n", rel_path="src/app.js", language="javascript")
        assert self.rule.check(ctx) == []


class TestLongFunction:
    rule = LongFunctionRule()

    @staticmethod
    def _src(body_lines: int) -> str:
        lines = ["function big() {"]
        lines += [f"  step{i}();" for i in range(body_lines)]
        lines.append("}")
        return "\n".join(lines) + "\n"

    def test_positive_over_80(self, make_ctx):
        ctx = make_ctx(self._src(80), rel_path="m.js", language="javascript")
        hits = self.rule.check(ctx)
        assert [h.line_start for h in hits] == [1]
        assert hits[0].line_end == 82  # 1 头 + 80 体 + 1 尾

    def test_negative_exactly_80(self, make_ctx):
        ctx = make_ctx(self._src(78), rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []

    def test_negative_small_arrow(self, make_ctx):
        ctx = make_ctx("const f = (x) => {\n  return x + 1;\n};\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []


class TestDeepNesting:
    rule = DeepNestingRule()

    def test_positive_four_levels(self, make_ctx):
        src = (
            "function f() {\n"
            "  if (a) {\n"
            "    if (b) {\n"
            "      if (c) {\n"
            "        work();\n"
            "      }\n"
            "    }\n"
            "  }\n"
            "}\n"
        )
        ctx = make_ctx(src, rel_path="m.js", language="javascript")
        assert _lines(self.rule, ctx) == [5]

    def test_negative_three_levels(self, make_ctx):
        src = (
            "function f() {\n"
            "  if (a) {\n"
            "    if (b) {\n"
            "      work();\n"
            "    }\n"
            "  }\n"
            "}\n"
        )
        ctx = make_ctx(src, rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []


class TestMagicNumber:
    rule = MagicNumberRule()

    def test_positive_compare(self, make_ctx):
        ctx = make_ctx("if (count > 10000) {\n  stop();\n}\n", rel_path="m.js", language="javascript")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_hex(self, make_ctx):
        ctx = make_ctx("if ((flags & 0xFFFF) !== 0) {}\n", rel_path="m.js", language="javascript")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_named_const(self, make_ctx):
        ctx = make_ctx("const MAX_SIZE = 10000;\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []

    def test_negative_below_threshold(self, make_ctx):
        ctx = make_ctx("if (count > 999) {}\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []

    def test_negative_import_line(self, make_ctx):
        ctx = make_ctx("import { take } from './util';\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []


class TestTodoFixme:
    rule = TodoFixmeCommentRule()

    def test_positive_todo(self, make_ctx):
        ctx = make_ctx("// TODO: implement later\nrun();\n", rel_path="m.js", language="javascript")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_block_comment(self, make_ctx):
        ctx = make_ctx("run();\n/* FIXME: hotfix pending */\n", rel_path="m.js", language="javascript")
        assert _lines(self.rule, ctx) == [2]

    def test_negative_plain_comment(self, make_ctx):
        ctx = make_ctx("// 已完成的事项说明\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []

    def test_negative_marker_in_code_identifier(self, make_ctx):
        # 代码里的 todo 变量不算注释残留
        ctx = make_ctx("const todoList = [];\n", rel_path="m.js", language="javascript")
        assert self.rule.check(ctx) == []


# ---------------------------------------------------------------- 扫描器契约


class TestScannerContract:
    def test_regex_and_division_disambiguation(self):
        scan = scan_js(["const v = /a{2}/.test(s) / 2;"])
        # 正则内容被掩码，代码部分保留；正则 2 个定界符 + 除号共 3 个斜杠
        assert "test" in scan.masked[0]
        assert "{" not in scan.masked[0]
        assert scan.masked[0].count("/") == 3

    def test_template_interpolation_kept(self):
        scan = scan_js(["const s = `a ${x + 1} b`;"])
        assert "${x + 1}" in scan.masked[0]

    def test_comments_and_strings_collected(self):
        src = ["// note one", "const s = 'x'; /* note two */"]
        scan = scan_js(src)
        assert [t for _, t in scan.comments] == ["note one", "note two"]
        assert any(lineno == 2 for lineno, *_ in scan.strings)

    def test_function_ranges_cover_three_kinds(self):
        src = [
            "function decl(a) {",
            "  return a;",
            "}",
            "const arrow = (b) => {",
            "  return b;",
            "};",
            "class K {",
            "  method(c) {",
            "    return c;",
            "  }",
            "}",
        ]
        names = {f.name: (f.start, f.end) for f in function_ranges(scan_js(src).masked)}
        assert names["decl"] == (1, 3)
        assert names["arrow"] == (4, 6)
        assert names["method"] == (8, 10)


# ---------------------------------------------------------------- 语言隔离


class TestLanguageIsolation:
    def test_registry_routes_by_language(self):
        from audit.detect.registry import get_registry

        reg = get_registry()
        py_ids = {r.id for r in reg.rules_for("python")}
        js_ids = {r.id for r in reg.rules_for("javascript")}
        ts_ids = {r.id for r in reg.rules_for("typescript")}
        assert not (py_ids & js_ids)  # JS 规则不会作用于 Python 文件
        # 5 条语言无关的安全规则与 TS 共享，另有 ≥6 条 TS 专属规则
        shared = {"JS-EVAL-EXEC", "JS-INNERHTML", "JS-DOCUMENT-WRITE", "JS-SQL-CONCAT", "JS-HARDCODED-SECRET"}
        assert shared <= ts_ids
        assert len(ts_ids - js_ids) >= 6

    def test_build_list_unique_ids(self):
        rules = build_javascript_rules()
        ids = [r.id for r in rules]
        assert len(ids) == len(set(ids))
        assert len(ids) >= 15
        assert all(i.startswith("JS-") for i in ids)
