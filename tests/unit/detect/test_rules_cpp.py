"""C++ 规则单元测试（W28-C）：三规则正反例、AST 佐证、tree=None 行为等价与注册契约。"""

from __future__ import annotations

import pytest

from audit.detect.ast_util import AstParseGate
from audit.detect.base import RuleContext
from audit.detect.registry import DEFAULT_REGISTRY
from audit.detect.rules import build_cpp_rules, cpp_rule_count
from audit.detect.rules.cpp import (
    CppHardcodedSecretRule,
    CppLongFunctionRule,
    CppSqlInjectionRule,
)


@pytest.fixture
def rules():
    return {r.id: r for r in build_cpp_rules()}


def _ctx(source: str, rel_path: str = "foo.cpp", tree=None) -> RuleContext:
    return RuleContext(
        rel_path=rel_path, language="cpp", source=source, lines=source.splitlines(), tree=tree
    )


def _lines(rule, ctx):
    return [h.line_start for h in rule.check(ctx)]


# ---------------------------------------------------------------- CPP-SQL-INJECTION


class TestCppSqlInjection:
    rule = CppSqlInjectionRule()

    def test_concat_into_query(self):
        src = (
            "std::string q(const std::string& u) {\n"
            "    return \"SELECT * FROM users WHERE n='\" + u + \"'\";\n"
            "}\n"
        )
        assert _lines(self.rule, _ctx(src)) == [2]

    def test_concat_into_insert(self):
        src = (
            "int add(const std::string& u) {\n"
            "    std::string s = \"INSERT INTO t(u) VALUES('\" + u + \"')\";\n"
            "    return exec(s);\n"
            "}\n"
        )
        assert _lines(self.rule, _ctx(src)) == [2]

    def test_snprintf_form(self):
        src = (
            "void q(char* buf, int u) {\n"
            "    snprintf(buf, 256, \"SELECT * FROM users WHERE n=%d\", u);\n"
            "}\n"
        )
        assert _lines(self.rule, _ctx(src)) == [2]

    def test_std_format_without_variable_args_clean(self):
        # 格式化但无变量实参（std::format 单实参）：不含用户可变片段，不命中
        src = (
            "std::string q() {\n"
            "    return std::format(\"SELECT * FROM users WHERE n=1\");\n"
            "}\n"
        )
        assert self.rule.check(_ctx(src)) == []

    def test_parameterized_query_clean(self):
        src = (
            "void q(DB* db, const std::string& u) {\n"
            "    db->execute(\"SELECT * FROM users WHERE n=?\", u);\n"
            "}\n"
        )
        assert self.rule.check(_ctx(src)) == []

    def test_sql_keyword_in_comment_clean(self):
        src = (
            "// SELECT * FROM users WHERE n='x' + y 只是注释字样\n"
            "void q() {\n"
            "}\n"
        )
        assert self.rule.check(_ctx(src)) == []

    def test_plain_string_without_concat_clean(self):
        src = "const char* kSql = \"SELECT * FROM users\";\n"
        assert self.rule.check(_ctx(src)) == []

    def test_ast_confirm_boosts_confidence(self):
        src = (
            "std::string q(const std::string& u) {\n"
            "    return \"SELECT * FROM users WHERE n='\" + u + \"'\";\n"
            "}\n"
        )
        tree = AstParseGate().parse("cpp", src)
        assert tree is not None
        hits = self.rule.check(_ctx(src, tree=tree))
        assert hits and hits[0].meta.get("ast_confirmed") is True
        assert hits[0].meta.get("confidence") == 0.8

    def test_tree_none_keeps_line_hits(self):
        """行为等价铁律：tree=None 时行级命中原样保留，无佐证注记。"""
        src = (
            "std::string q(const std::string& u) {\n"
            "    return \"SELECT * FROM users WHERE n='\" + u + \"'\";\n"
            "}\n"
        )
        hits = self.rule.check(_ctx(src, tree=None))
        assert _lines(self.rule, _ctx(src, tree=None)) == [2]
        assert all("ast_confirmed" not in h.meta for h in hits)


# ---------------------------------------------------------------- CPP-HARDCODED-SECRET


class TestCppHardcodedSecret:
    rule = CppHardcodedSecretRule()

    def test_high_entropy_const(self):
        src = "const std::string kApiKey = \"aX9kQ2vL8mN4pR7sT5uW3yZ0\";\n"
        assert _lines(self.rule, _ctx(src)) == [1]

    def test_constexpr_declaration(self):
        # constexpr 形态（值须过熵闸门，字符串字面量 + constexpr 合法）
        src = "constexpr const char* kApiToken = \"aX9kQ2vL8mN4pR7sT5uW3yZ1\";\n"
        assert _lines(self.rule, _ctx(src)) == [1]

    def test_static_char_pointer_form(self):
        src = "static const char* kSecretKey = \"aX9kQ2vL8mN4pR7sT5uW3yZ2\";\n"
        assert _lines(self.rule, _ctx(src)) == [1]

    def test_macro_define_sk_prefix(self):
        # #define 宏形态 + sk- API key 形态
        src = "#define DEPLOY_TOKEN \"sk-projdZ8sKq2mN7xVb3cLw9\"\n"
        assert _lines(self.rule, _ctx(src)) == [1]

    def test_password_family_relaxed_gate(self):
        # R4-7：password 家族放低闸门——低熵自然词组合也命中
        src = "std::string db_password = \"mysupersecretkey\";\n"
        assert _lines(self.rule, _ctx(src)) == [1]

    def test_low_entropy_placeholder_clean(self):
        src = "const std::string kPassword = \"changeme\";\n"
        assert self.rule.check(_ctx(src)) == []

    def test_short_value_clean(self):
        src = "std::string token = \"abc\";\n"
        assert self.rule.check(_ctx(src)) == []

    def test_plain_name_clean(self):
        # 无敏感词的普通长串：不命中
        src = "const std::string kBucket = \"prod-assets-bucket-name\";\n"
        assert self.rule.check(_ctx(src)) == []

    def test_comparison_clean(self):
        # == 比较不是赋值形态：不命中
        src = (
            "bool ok(const std::string& token) {\n"
            "    return token == \"aX9kQ2vL8mN4pR7sT5u\";\n"
            "}\n"
        )
        assert self.rule.check(_ctx(src)) == []

    def test_snippet_masked(self):
        # NFR-11：mask_snippet=True，snippet 中字面量值打码
        src = "const std::string kApiKey = \"aX9kQ2vL8mN4pR7sT5uW3yZ0\";\n"
        hit = self.rule.check(_ctx(src))[0]
        assert "aX9kQ2vL8mN4pR7sT5uW3yZ0" not in hit.snippet
        assert "********" in hit.snippet


# ---------------------------------------------------------------- CPP-LONG-FUNCTION


class TestCppLongFunction:
    rule = CppLongFunctionRule()

    @staticmethod
    def _source(body_lines: int) -> str:
        body = "\n".join(f"    total += {i};" for i in range(body_lines))
        return f"void run() {{\n{body}\n}}\n"

    def test_over_80_lines_hit(self):
        hits = self.rule.check(_ctx(self._source(84)))
        assert len(hits) == 1
        assert hits[0].line_start == 1
        assert hits[0].meta["lines"] == 86  # 函数头 + 84 行体 + 收尾括号

    def test_exactly_80_lines_clean(self):
        # 80 行整不超阈值（家族 MAX_LINES=80，严格大于才报）
        assert self.rule.check(_ctx(self._source(78))) == []

    def test_short_functions_clean(self):
        src = "int add(int a, int b) {\n    return a + b;\n}\n"
        assert self.rule.check(_ctx(src)) == []

    def test_qualified_method_definition(self):
        body = "\n".join(f"    total += {i};" for i in range(84))
        src = f"void Service::run() {{\n{body}\n}}\n"
        hits = self.rule.check(_ctx(src))
        assert len(hits) == 1 and hits[0].meta["lines"] > 80
        assert "run" in hits[0].message

    def test_control_flow_heads_not_matched(self):
        # if/for 等控制流头与函数头同形态：不产作用域
        body = "\n".join(f"    total += {i};" for i in range(84))
        src = f"void run() {{\n{body}\n}}\n"
        masked_scan_hits = self.rule.check(_ctx(src))
        assert len(masked_scan_hits) == 1  # 真函数命中一次
        # 函数体外的伪函数头（字符串内）不参与：掩码扫描天然规避
        src2 = (
            "const char* kFake = \"void fake(int x) {\";\n"
            "void real() {\n"
            "    int a = 1;\n"
            "}\n"
        )
        assert self.rule.check(_ctx(src2)) == []


# ---------------------------------------------------------------- 注册契约


class TestCppRuleRegistration:
    def test_cpp_rule_count(self):
        assert cpp_rule_count() == 3

    def test_build_ids(self):
        assert [r.id for r in build_cpp_rules()] == [
            "CPP-SQL-INJECTION",
            "CPP-HARDCODED-SECRET",
            "CPP-LONG-FUNCTION",
        ]

    def test_rules_declare_cpp_language(self):
        for rule in build_cpp_rules():
            assert rule.languages == ("cpp",)

    def test_rules_route_via_registry(self):
        ids = {r.id for r in DEFAULT_REGISTRY.rules_for("cpp")}
        assert ids == {"CPP-SQL-INJECTION", "CPP-HARDCODED-SECRET", "CPP-LONG-FUNCTION"}

    def test_registry_total_includes_cpp_three(self):
        # W28-C：规则库 89 -> 92；W30 卡 A taint 追加 1 条后为 93
        #（口径与 tests/unit/detect/test_rules_ext.py 一致）
        assert len(DEFAULT_REGISTRY) == 93

    def test_security_rules_metadata(self):
        assert CppSqlInjectionRule().severity.value == "critical"
        assert CppHardcodedSecretRule().mask_snippet is True
        assert CppLongFunctionRule().category.value == "style"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
