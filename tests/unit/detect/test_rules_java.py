"""Java 规则单元测试（W24-A）：三规则正反例、AST 佐证、tree=None 行为等价与注册契约。"""

from __future__ import annotations

import pytest

from audit.detect.ast_util import AstParseGate
from audit.detect.base import RuleContext
from audit.detect.registry import DEFAULT_REGISTRY
from audit.detect.rules import build_java_rules, java_rule_count
from audit.detect.rules.java import (
    JavaHardcodedSecretRule,
    JavaLongFunctionRule,
    JavaSqlInjectionRule,
)


@pytest.fixture
def rules():
    return {r.id: r for r in build_java_rules()}


def _ctx(source: str, rel_path: str = "Foo.java", tree=None) -> RuleContext:
    return RuleContext(
        rel_path=rel_path, language="java", source=source, lines=source.splitlines(), tree=tree
    )


def _lines(rule, ctx):
    return [h.line_start for h in rule.check(ctx)]


# ---------------------------------------------------------------- JAVA-SQL-INJECTION


class TestJavaSqlInjection:
    rule = JavaSqlInjectionRule()

    def test_concat_into_execute(self):
        src = (
            "public class D {\n"
            "    void q(java.sql.Statement st, String u) {\n"
            "        st.executeQuery(\"SELECT * FROM users WHERE n='\" + u + \"'\");\n"
            "    }\n"
            "}\n"
        )
        assert _lines(self.rule, _ctx(src)) == [3]

    def test_concat_into_prepare_statement(self):
        src = (
            "public class D {\n"
            "    void b(java.sql.Connection c, String u) {\n"
            "        c.prepareStatement(\"INSERT INTO t(u) VALUES('\" + u + \"')\");\n"
            "    }\n"
            "}\n"
        )
        assert _lines(self.rule, _ctx(src)) == [3]

    def test_parameterized_query_clean(self):
        src = (
            "public class D {\n"
            "    void q(java.sql.Connection c, String u) {\n"
            "        c.prepareStatement(\"SELECT * FROM users WHERE n=?\");\n"
            "    }\n"
            "}\n"
        )
        assert self.rule.check(_ctx(src)) == []

    def test_sql_keyword_in_comment_clean(self):
        src = (
            "public class D {\n"
            "    // SELECT * FROM users WHERE n='x' + y 只是注释字样\n"
            "    void q() {\n"
            "    }\n"
            "}\n"
        )
        assert self.rule.check(_ctx(src)) == []

    def test_plain_string_without_concat_clean(self):
        src = (
            "public class D {\n"
            "    String sql = \"SELECT * FROM users\";\n"
            "}\n"
        )
        assert self.rule.check(_ctx(src)) == []

    def test_ast_confirm_boosts_confidence(self):
        src = (
            "public class D {\n"
            "    void q(java.sql.Statement st, String u) {\n"
            "        st.executeQuery(\"SELECT * FROM users WHERE n='\" + u + \"'\");\n"
            "    }\n"
            "}\n"
        )
        tree = AstParseGate().parse("java", src)
        assert tree is not None
        hits = self.rule.check(_ctx(src, tree=tree))
        assert hits and hits[0].meta.get("ast_confirmed") is True
        assert hits[0].meta.get("confidence") == 0.8

    def test_tree_none_keeps_line_hits(self):
        """行为等价铁律：tree=None 时行级命中原样保留，无佐证注记。"""
        src = (
            "public class D {\n"
            "    void q(java.sql.Statement st, String u) {\n"
            "        st.executeQuery(\"SELECT * FROM users WHERE n='\" + u + \"'\");\n"
            "    }\n"
            "}\n"
        )
        hits = self.rule.check(_ctx(src, tree=None))
        assert _lines(self.rule, _ctx(src, tree=None)) == [3]
        assert all("ast_confirmed" not in h.meta for h in hits)


# ---------------------------------------------------------------- JAVA-HARDCODED-SECRET


class TestJavaHardcodedSecret:
    rule = JavaHardcodedSecretRule()

    def test_high_entropy_api_key(self):
        src = 'public class C {\n    private static final String API_KEY = "aX9kQ2vL8mN4pR7sT5uW3yZ0";\n}\n'
        assert _lines(self.rule, _ctx(src)) == [2]

    def test_password_family_relaxed_gate(self):
        # R4-7：password 家族放低闸门——低熵自然词组合也命中
        src = 'public class C {\n    private String dbPassword = "mysupersecretkey";\n}\n'
        assert _lines(self.rule, _ctx(src)) == [2]

    def test_local_sk_prefix_variable(self):
        src = 'public class C {\n    String run() {\n        String apiKey = "sk-projdZ8sKq2mN7xVb3cLw9";\n        return apiKey;\n    }\n}\n'
        assert _lines(self.rule, _ctx(src)) == [3]

    def test_low_entropy_placeholder_clean(self):
        src = 'public class C {\n    private static final String PASSWORD = "changeme";\n}\n'
        assert self.rule.check(_ctx(src)) == []

    def test_short_value_clean(self):
        src = 'public class C {\n    private String token = "abc";\n}\n'
        assert self.rule.check(_ctx(src)) == []

    def test_plain_name_clean(self):
        # 无敏感词的普通长串：不命中
        src = 'public class C {\n    private static final String BUCKET = "prod-assets-bucket-name";\n}\n'
        assert self.rule.check(_ctx(src)) == []

    def test_snippet_masked(self):
        # NFR-11：mask_snippet=True，snippet 中字面量值打码
        src = 'public class C {\n    private static final String API_KEY = "aX9kQ2vL8mN4pR7sT5uW3yZ0";\n}\n'
        hit = self.rule.check(_ctx(src))[0]
        assert "aX9kQ2vL8mN4pR7sT5uW3yZ0" not in hit.snippet
        assert "********" in hit.snippet


# ---------------------------------------------------------------- JAVA-LONG-FUNCTION


class TestJavaLongFunction:
    rule = JavaLongFunctionRule()

    @staticmethod
    def _source(body_lines: int) -> str:
        body = "\n".join(f"        total += {i};" for i in range(body_lines))
        return f"public class B {{\n    public void run() {{\n{body}\n    }}\n}}\n"

    def test_over_80_lines_hit(self):
        hits = self.rule.check(_ctx(self._source(82)))
        assert len(hits) == 1
        assert hits[0].line_start == 2
        assert hits[0].meta["lines"] == 84  # 方法头 + 82 行体 + 收尾括号

    def test_exactly_80_lines_clean(self):
        # 80 行整不超阈值（家族 MAX_LINES=80，严格大于才报）
        assert self.rule.check(_ctx(self._source(78))) == []

    def test_short_methods_clean(self):
        src = "public class M {\n    int add(int a, int b) {\n        return a + b;\n    }\n}\n"
        assert self.rule.check(_ctx(src)) == []

    def test_brace_pairing_ignores_string_braces(self):
        # 字符串中的大括号已被掩码，不影响块配对
        body = "\n".join(
            f'        buf[{i}] = "}}";' if i % 2 else f'        buf[{i}] = "{{";'
            for i in range(85)
        )
        src = f"public class B {{\n    void fill() {{\n{body}\n    }}\n}}\n"
        hits = self.rule.check(_ctx(src))
        assert len(hits) == 1 and hits[0].meta["lines"] > 80


# ---------------------------------------------------------------- 注册契约


class TestJavaRuleRegistration:
    def test_java_rule_count(self):
        assert java_rule_count() == 3

    def test_build_ids(self):
        assert [r.id for r in build_java_rules()] == [
            "JAVA-SQL-INJECTION",
            "JAVA-HARDCODED-SECRET",
            "JAVA-LONG-FUNCTION",
        ]

    def test_rules_declare_java_language(self):
        for rule in build_java_rules():
            assert rule.languages == ("java",)

    def test_rules_route_via_registry(self):
        ids = {r.id for r in DEFAULT_REGISTRY.rules_for("java")}
        assert ids == {"JAVA-SQL-INJECTION", "JAVA-HARDCODED-SECRET", "JAVA-LONG-FUNCTION"}

    def test_registry_total_includes_java_three(self):
        # W24-A：规则库 83 -> 86；W26-C Go / W28-C C++ 各追加 3 条、W30 卡 A taint
        # 追加 1 条后为 93（口径与 tests/unit/detect/test_rules_ext.py 一致）
        assert len(DEFAULT_REGISTRY) == 93

    def test_security_rules_metadata(self):
        assert JavaSqlInjectionRule().severity.value == "critical"
        assert JavaHardcodedSecretRule().mask_snippet is True
        assert JavaLongFunctionRule().category.value == "style"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
