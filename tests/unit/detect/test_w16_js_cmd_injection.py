"""W16 静态规则补缺：JS-COMMAND-INJECTION / JS-NAMING-STYLE 正反用例 + 红线反例。

命令注入正例覆盖 require/import(node:) 两种导入 + `+` 拼接 / 模板插值 / 变量
三种动态首参，并验证 TS 共享语言；反例覆盖纯字符串字面量、无导入、正则
.exec、clean 语料摘录。命名规则按保守口径：PascalCase function 且全文件无
`new 该名` 使用才报。
"""

from __future__ import annotations

from audit.detect.rules.js.js_security_ext import (
    JsCommandInjectionRule,
    JsNamingStyleRule,
    build_js_security_ext_rules,
)


def _lines(rule, ctx):
    return [h.line_start for h in rule.check(ctx)]


def _js(make_ctx, source: str, rel_path: str = "m.js", language: str = "javascript"):
    return make_ctx(source, rel_path=rel_path, language=language)


# clean 语料（src/profile.js / src/config.js）摘录，逐字拷入作红线反例
CLEAN_PROFILE_EXCERPT = (
    "export async function fetchProfile(url) {\n"
    "  const resp = await fetch(url, { signal: AbortSignal.timeout(5000) });\n"
    "  if (!resp.ok) {\n"
    "    throw new Error(`HTTP ${resp.status}`);\n"
    "  }\n"
    "  return resp.json();\n"
    "}\n"
    "\n"
    "export function renderName(el, name) {\n"
    "  el.textContent = name;\n"
    "}\n"
)
CLEAN_CONFIG_EXCERPT = (
    "import { readFileSync } from 'node:fs';\n"
    "\n"
    "export function loadConfig(path) {\n"
    "  const raw = readFileSync(path, 'utf8');\n"
    "  return JSON.parse(raw);\n"
    "}\n"
)


class TestJsCommandInjection:
    rule = JsCommandInjectionRule()

    def test_positive_require_plus_concat(self, make_ctx):
        source = (
            "const cp = require('child_process');\n"
            "\n"
            "function ping(host) {\n"
            "  return cp.exec('ping -c 1 ' + host);\n"
            "}\n"
        )
        ctx = _js(make_ctx, source)
        assert _lines(self.rule, ctx) == [4]

    def test_positive_import_template_interp(self, make_ctx):
        source = (
            "import cp from 'node:child_process';\n"
            "\n"
            "export function ls(dir) {\n"
            "  return cp.execSync(`ls ${dir}`);\n"
            "}\n"
        )
        ctx = _js(make_ctx, source)
        assert _lines(self.rule, ctx) == [4]

    def test_positive_spawn_variable(self, make_ctx):
        source = (
            'const childProcess = require("child_process");\n'
            "\n"
            "function run(cmd) {\n"
            "  return childProcess.spawn(cmd);\n"
            "}\n"
        )
        ctx = _js(make_ctx, source)
        assert _lines(self.rule, ctx) == [4]

    def test_positive_typescript_shared(self, make_ctx):
        source = (
            "import cp from 'node:child_process';\n"
            "\n"
            "export function run(cmd: string): unknown {\n"
            "  return cp.exec('ls ' + cmd);\n"
            "}\n"
        )
        ctx = _js(make_ctx, source, rel_path="m.ts", language="typescript")
        assert _lines(self.rule, ctx) == [4]

    def test_negative_pure_string_literal(self, make_ctx):
        # 首参为纯字符串字面量（无插值）不命中
        source = (
            "const cp = require('child_process');\n"
            "\n"
            "function ls() {\n"
            "  return cp.exec('ls -la');\n"
            "}\n"
        )
        ctx = _js(make_ctx, source)
        assert self.rule.check(ctx) == []

    def test_negative_plain_template_no_interp(self, make_ctx):
        source = (
            "const cp = require('child_process');\n"
            "\n"
            "function ls() {\n"
            "  return cp.spawnSync(`ls -la`);\n"
            "}\n"
        )
        ctx = _js(make_ctx, source)
        assert self.rule.check(ctx) == []

    def test_negative_no_child_process_import(self, make_ctx):
        ctx = _js(make_ctx, "function f(x) {\n  return foo.exec(x);\n}\n")
        assert self.rule.check(ctx) == []

    def test_negative_regex_literal_exec(self, make_ctx):
        # /re/.exec 为 RegExp API，非命令执行
        source = (
            "const cp = require('child_process');\n"
            "\n"
            "function m(s) {\n"
            "  return /ab+c/.exec(s);\n"
            "}\n"
        )
        ctx = _js(make_ctx, source)
        assert self.rule.check(ctx) == []

    def test_negative_clean_corpus_config_excerpt(self, make_ctx):
        # 红线：clean 语料（src/config.js）无 child_process → 0 命中
        ctx = _js(make_ctx, CLEAN_CONFIG_EXCERPT)
        assert self.rule.check(ctx) == []


class TestJsNamingStyle:
    rule = JsNamingStyleRule()

    def test_positive_pascal_no_new(self, make_ctx):
        ctx = _js(make_ctx, "function RenderUser(user) {\n  return user.name;\n}\n")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_export_default_pascal(self, make_ctx):
        ctx = _js(make_ctx, "export default function Handler(req) {\n  return req;\n}\n")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_with_new_usage(self, make_ctx):
        # 构造函数惯例豁免：文件中存在 new 该名 调用
        source = (
            "function Point(x, y) {\n"
            "  this.x = x;\n"
            "}\n"
            "\n"
            "const p = new Point(1, 2);\n"
        )
        ctx = _js(make_ctx, source)
        assert self.rule.check(ctx) == []

    def test_single_hit_with_blank_line_before(self, make_ctx):
        # 回归：function_ranges 跨行头匹配从空行拼接出的幽灵区间不得重复上报
        source = (
            "const x = 1;\n"
            "\n"
            "function RenderUser(user) {\n"
            "  return user.name;\n"
            "}\n"
        )
        ctx = _js(make_ctx, source)
        assert _lines(self.rule, ctx) == [3]

    def test_negative_lowercase_function(self, make_ctx):
        ctx = _js(make_ctx, "function renderName(el, name) {\n  el.textContent = name;\n}\n")
        assert self.rule.check(ctx) == []

    def test_negative_test_file_exempt(self, make_ctx):
        ctx = _js(
            make_ctx,
            "function RenderUser(user) {\n  return user.name;\n}\n",
            rel_path="test/util.test.js",
        )
        assert self.rule.check(ctx) == []

    def test_negative_clean_corpus_profile_excerpt(self, make_ctx):
        # 红线：clean 语料（src/profile.js）函数命名全部小写驼峰 → 0 命中
        ctx = _js(make_ctx, CLEAN_PROFILE_EXCERPT)
        assert self.rule.check(ctx) == []


def test_build_js_security_ext_rules():
    rules = build_js_security_ext_rules()
    assert [r.id for r in rules] == ["JS-COMMAND-INJECTION", "JS-NAMING-STYLE"]
    assert rules[0].languages == ("javascript", "typescript")
    assert rules[1].languages == ("javascript",)
    assert rules[0].category.value == "security" and rules[0].severity.value == "high"
    assert rules[1].category.value == "style" and rules[1].severity.value == "low"
    # 契约 v1.6：新规则带正反示例
    assert all(r.bad_example and r.good_example for r in rules)
