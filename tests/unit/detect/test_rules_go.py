"""Go 规则单元测试（W26-C）：三规则正反例、AST 佐证、tree=None 行为等价与注册契约。"""

from __future__ import annotations

import pytest

from audit.detect.ast_util import AstParseGate
from audit.detect.base import RuleContext
from audit.detect.registry import DEFAULT_REGISTRY
from audit.detect.rules import build_go_rules, go_rule_count
from audit.detect.rules.go import (
    GoHardcodedSecretRule,
    GoLongFunctionRule,
    GoSqlInjectionRule,
)


@pytest.fixture
def rules():
    return {r.id: r for r in build_go_rules()}


def _ctx(source: str, rel_path: str = "foo.go", tree=None) -> RuleContext:
    return RuleContext(
        rel_path=rel_path, language="go", source=source, lines=source.splitlines(), tree=tree
    )


def _lines(rule, ctx):
    return [h.line_start for h in rule.check(ctx)]


# ---------------------------------------------------------------- GO-SQL-INJECTION


class TestGoSqlInjection:
    rule = GoSqlInjectionRule()

    def test_concat_into_query(self):
        src = (
            "package d\n"
            "\n"
            "func q(db DB, u string) {\n"
            "\tdb.Query(\"SELECT * FROM users WHERE n='\" + u + \"'\")\n"
            "}\n"
        )
        assert _lines(self.rule, _ctx(src)) == [4]

    def test_concat_into_exec(self):
        src = (
            "package d\n"
            "\n"
            "func b(db DB, u string) {\n"
            "\tdb.Exec(\"INSERT INTO t(u) VALUES('\" + u + \"')\")\n"
            "}\n"
        )
        assert _lines(self.rule, _ctx(src)) == [4]

    def test_sprintf_form(self):
        src = (
            "package d\n"
            "\n"
            "func q(db DB, u string) {\n"
            "\tq := fmt.Sprintf(\"SELECT * FROM users WHERE n='%s'\", u)\n"
            "\tdb.Exec(q)\n"
            "}\n"
        )
        assert _lines(self.rule, _ctx(src)) == [4]

    def test_raw_string_concat_form(self):
        # 反引号原生串（go 惯例 SQL 写法）同样参与拼接判定
        src = (
            "package d\n"
            "\n"
            "func q(db DB, u string) {\n"
            "\tdb.Query(`SELECT * FROM users WHERE n='` + u + `'`)\n"
            "}\n"
        )
        assert _lines(self.rule, _ctx(src)) == [4]

    def test_sprintf_without_variable_args_clean(self):
        # 格式化但无变量实参：不含用户可变片段，不命中
        src = (
            "package d\n"
            "\n"
            "func q(db DB) {\n"
            "\tq := fmt.Sprintf(\"SELECT * FROM users WHERE n=1\")\n"
            "\tdb.Exec(q)\n"
            "}\n"
        )
        assert self.rule.check(_ctx(src)) == []

    def test_parameterized_query_clean(self):
        src = (
            "package d\n"
            "\n"
            "func q(db DB, u string) {\n"
            "\tdb.Query(\"SELECT * FROM users WHERE n=$1\", u)\n"
            "}\n"
        )
        assert self.rule.check(_ctx(src)) == []

    def test_sql_keyword_in_comment_clean(self):
        src = (
            "package d\n"
            "\n"
            "// SELECT * FROM users WHERE n='x' + y 只是注释字样\n"
            "func q() {\n"
            "}\n"
        )
        assert self.rule.check(_ctx(src)) == []

    def test_plain_string_without_concat_clean(self):
        src = "package d\n\nvar sql = \"SELECT * FROM users\"\n"
        assert self.rule.check(_ctx(src)) == []

    def test_ast_confirm_boosts_confidence(self):
        src = (
            "package d\n"
            "\n"
            "func q(db DB, u string) {\n"
            "\tdb.Query(\"SELECT * FROM users WHERE n='\" + u + \"'\")\n"
            "}\n"
        )
        tree = AstParseGate().parse("go", src)
        assert tree is not None
        hits = self.rule.check(_ctx(src, tree=tree))
        assert hits and hits[0].meta.get("ast_confirmed") is True
        assert hits[0].meta.get("confidence") == 0.8

    def test_tree_none_keeps_line_hits(self):
        """行为等价铁律：tree=None 时行级命中原样保留，无佐证注记。"""
        src = (
            "package d\n"
            "\n"
            "func q(db DB, u string) {\n"
            "\tdb.Query(\"SELECT * FROM users WHERE n='\" + u + \"'\")\n"
            "}\n"
        )
        hits = self.rule.check(_ctx(src, tree=None))
        assert _lines(self.rule, _ctx(src, tree=None)) == [4]
        assert all("ast_confirmed" not in h.meta for h in hits)


# ---------------------------------------------------------------- GO-HARDCODED-SECRET


class TestGoHardcodedSecret:
    rule = GoHardcodedSecretRule()

    def test_high_entropy_const(self):
        src = "package c\n\nconst APIKey = \"aX9kQ2vL8mN4pR7sT5uW3yZ0\"\n"
        assert _lines(self.rule, _ctx(src)) == [3]

    def test_password_family_relaxed_gate(self):
        # R4-7：password 家族放低闸门——低熵自然词组合也命中
        src = "package c\n\nvar dbPassword = \"mysupersecretkey\"\n"
        assert _lines(self.rule, _ctx(src)) == [3]

    def test_local_sk_prefix_variable(self):
        src = "package c\n\nfunc run() string {\n\tapiKey := \"sk-projdZ8sKq2mN7xVb3cLw9\"\n\treturn apiKey\n}\n"
        assert _lines(self.rule, _ctx(src)) == [4]

    def test_var_declaration_with_type(self):
        src = "package c\n\nvar apiToken string = \"aX9kQ2vL8mN4pR7sT5uW3yZ1\"\n"
        assert _lines(self.rule, _ctx(src)) == [3]

    def test_low_entropy_placeholder_clean(self):
        src = "package c\n\nconst Password = \"changeme\"\n"
        assert self.rule.check(_ctx(src)) == []

    def test_short_value_clean(self):
        src = "package c\n\nvar token = \"abc\"\n"
        assert self.rule.check(_ctx(src)) == []

    def test_plain_name_clean(self):
        # 无敏感词的普通长串：不命中
        src = "package c\n\nconst Bucket = \"prod-assets-bucket-name\"\n"
        assert self.rule.check(_ctx(src)) == []

    def test_comparison_and_struct_field_clean(self):
        # == 比较与 struct 字段/tag 不是赋值形态：不命中
        src = (
            "package c\n"
            "\n"
            "type T struct {\n"
            "\tName string `json:\"name\"`\n"
            "}\n"
            "\n"
            "func f(token string) bool {\n"
            "\treturn token == \"aX9kQ2vL8mN4pR7sT5u\"\n"
            "}\n"
        )
        assert self.rule.check(_ctx(src)) == []

    def test_snippet_masked(self):
        # NFR-11：mask_snippet=True，snippet 中字面量值打码
        src = "package c\n\nconst APIKey = \"aX9kQ2vL8mN4pR7sT5uW3yZ0\"\n"
        hit = self.rule.check(_ctx(src))[0]
        assert "aX9kQ2vL8mN4pR7sT5uW3yZ0" not in hit.snippet
        assert "********" in hit.snippet


# ---------------------------------------------------------------- GO-LONG-FUNCTION


class TestGoLongFunction:
    rule = GoLongFunctionRule()

    @staticmethod
    def _source(body_lines: int) -> str:
        body = "\n".join(f"\ttotal += {i}" for i in range(body_lines))
        return f"package b\n\nfunc run() {{\n{body}\n}}\n"

    def test_over_80_lines_hit(self):
        hits = self.rule.check(_ctx(self._source(84)))
        assert len(hits) == 1
        assert hits[0].line_start == 3
        assert hits[0].meta["lines"] == 86  # 函数头 + 84 行体 + 收尾括号

    def test_exactly_80_lines_clean(self):
        # 80 行整不超阈值（家族 MAX_LINES=80，严格大于才报）
        assert self.rule.check(_ctx(self._source(78))) == []

    def test_short_functions_clean(self):
        src = "package m\n\nfunc add(a, b int) int {\n\treturn a + b\n}\n"
        assert self.rule.check(_ctx(src)) == []

    def test_method_with_receiver(self):
        body = "\n".join(f"\ts.total += {i}" for i in range(84))
        src = f"package b\n\ntype B struct {{ total int }}\n\nfunc (b *B) run() {{\n{body}\n}}\n"
        hits = self.rule.check(_ctx(src))
        assert len(hits) == 1 and hits[0].meta["lines"] > 80
        assert "run" in hits[0].message

    def test_brace_pairing_ignores_string_braces(self):
        # 字符串中的大括号已被掩码，不影响块配对
        body = "\n".join(
            f'\tbuf[{i}] = "}}" + "{{"' if i % 2 else f'\tbuf[{i}] = "{{" + "}}"'
            for i in range(85)
        )
        src = f"package b\n\nfunc fill() {{\n{body}\n}}\n"
        hits = self.rule.check(_ctx(src))
        assert len(hits) == 1 and hits[0].meta["lines"] > 80


# ---------------------------------------------------------------- 注册契约


class TestGoRuleRegistration:
    def test_go_rule_count(self):
        assert go_rule_count() == 3

    def test_build_ids(self):
        assert [r.id for r in build_go_rules()] == [
            "GO-SQL-INJECTION",
            "GO-HARDCODED-SECRET",
            "GO-LONG-FUNCTION",
        ]

    def test_rules_declare_go_language(self):
        for rule in build_go_rules():
            assert rule.languages == ("go",)

    def test_rules_route_via_registry(self):
        ids = {r.id for r in DEFAULT_REGISTRY.rules_for("go")}
        assert ids == {"GO-SQL-INJECTION", "GO-HARDCODED-SECRET", "GO-LONG-FUNCTION"}

    def test_registry_total_includes_go_three(self):
        # W26-C：规则库 86 -> 89；W28-C C++ 追加 3 条、W30 卡 A taint 追加 1 条后为 93
        #（口径与 tests/unit/detect/test_rules_ext.py 一致）
        assert len(DEFAULT_REGISTRY) == 93

    def test_security_rules_metadata(self):
        assert GoSqlInjectionRule().severity.value == "critical"
        assert GoHardcodedSecretRule().mask_snippet is True
        assert GoLongFunctionRule().category.value == "style"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
