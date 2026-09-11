"""TypeScript 规则正反用例（6 条）+ JS 共享安全规则在 TS 文件上的行为。"""

from __future__ import annotations

from audit.detect.rules.js.typescript import (
    AnyTypeRule,
    DoubleAssertionRule,
    ExportedAnyParamRule,
    ExportedFuncReturnTypeRule,
    NonNullAssertionAbuseRule,
    TsIgnoreRule,
    build_typescript_rules,
)


def _lines(rule, ctx):
    return [h.line_start for h in rule.check(ctx)]


def _ts(make_ctx, source: str, rel_path: str = "m.ts"):
    return make_ctx(source, rel_path=rel_path, language="typescript")


class TestAnyType:
    rule = AnyTypeRule()

    def test_positive_colon_any(self, make_ctx):
        ctx = _ts(make_ctx, "function f(x: any) {\n  return x;\n}\n")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_as_any(self, make_ctx):
        ctx = _ts(make_ctx, "const v = getData() as any;\n")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_unknown(self, make_ctx):
        ctx = _ts(make_ctx, "const v: unknown = getData();\n")
        assert self.rule.check(ctx) == []

    def test_negative_generic_other(self, make_ctx):
        ctx = _ts(make_ctx, "const xs: Array<string> = [];\n")
        assert self.rule.check(ctx) == []

    def test_negative_word_containing_any(self, make_ctx):
        ctx = _ts(make_ctx, "const company: Company = load();\n")
        assert self.rule.check(ctx) == []

    def test_negative_in_string(self, make_ctx):
        ctx = _ts(make_ctx, "const s = 'x: any';\n")
        assert self.rule.check(ctx) == []


class TestTsIgnore:
    rule = TsIgnoreRule()

    def test_positive(self, make_ctx):
        ctx = _ts(make_ctx, "// @ts-ignore\nconst v = risky();\n")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_ts_expect_error(self, make_ctx):
        ctx = _ts(make_ctx, "// @ts-expect-error 已登记\nconst v = risky();\n")
        assert self.rule.check(ctx) == []

    def test_negative_plain_comment(self, make_ctx):
        ctx = _ts(make_ctx, "// 普通注释\nconst v = 1;\n")
        assert self.rule.check(ctx) == []


class TestNonNullAssertionAbuse:
    rule = NonNullAssertionAbuseRule()

    def test_positive_five_occurrences(self, make_ctx):
        src = (
            "const a = getUser()!.name;\n"
            "const b = cfg!.opt!.value;\n"
            "const c = list![0].id;\n"
            "const d = a!.toString();\n"
            "const e = b!.trim();\n"
        )
        ctx = _ts(make_ctx, src)
        hits = self.rule.check(ctx)
        assert [h.line_start for h in hits] == [1]
        assert hits[0].meta["count"] >= 5

    def test_negative_few_occurrences(self, make_ctx):
        ctx = _ts(make_ctx, "const a = getUser()!.name;\nconst b = cfg!.value;\n")
        assert self.rule.check(ctx) == []

    def test_negative_logical_not(self, make_ctx):
        # 逻辑非 + 成员访问（!user.name）不是非空断言
        src = "if (!user.name) {\n  return;\n}\n" * 3
        ctx = _ts(make_ctx, src)
        assert self.rule.check(ctx) == []


class TestDoubleAssertion:
    rule = DoubleAssertionRule()

    def test_positive_as_unknown_as(self, make_ctx):
        ctx = _ts(make_ctx, "const u = parse(input) as unknown as User;\n")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_as_any_as(self, make_ctx):
        ctx = _ts(make_ctx, "const u = input as any as User;\n")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_single_assertion(self, make_ctx):
        ctx = _ts(make_ctx, "const u = input as User;\n")
        assert self.rule.check(ctx) == []

    def test_negative_in_string(self, make_ctx):
        ctx = _ts(make_ctx, "const tip = 'as unknown as User';\n")
        assert self.rule.check(ctx) == []


class TestExportedAnyParam:
    rule = ExportedAnyParamRule()

    def test_positive_exported_function(self, make_ctx):
        ctx = _ts(make_ctx, "export function handle(req: any) {\n  return req;\n}\n")
        assert _lines(self.rule, ctx) == [1]

    def test_positive_exported_arrow(self, make_ctx):
        ctx = _ts(make_ctx, "export const handler = (event: any) => {\n  return event;\n};\n")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_local_function(self, make_ctx):
        ctx = _ts(make_ctx, "function local(x: any) {\n  return x;\n}\n")
        assert self.rule.check(ctx) == []

    def test_negative_typed_params(self, make_ctx):
        ctx = _ts(make_ctx, "export function handle(req: Request): void {}\n")
        assert self.rule.check(ctx) == []


class TestExportedFuncReturnType:
    rule = ExportedFuncReturnTypeRule()

    def test_positive_missing_return_type(self, make_ctx):
        ctx = _ts(make_ctx, "export function getName(user: User) {\n  return user.name;\n}\n")
        assert _lines(self.rule, ctx) == [1]

    def test_negative_explicit_return_type(self, make_ctx):
        ctx = _ts(make_ctx, "export function getName(user: User): string {\n  return user.name;\n}\n")
        assert self.rule.check(ctx) == []

    def test_negative_local_function(self, make_ctx):
        ctx = _ts(make_ctx, "function local() {\n  return 1;\n}\n")
        assert self.rule.check(ctx) == []

    def test_positive_multiline_signature(self, make_ctx):
        ctx = _ts(make_ctx, "export function load(\n  id: string,\n  opt: Options,\n) {\n  return fetch(id);\n}\n")
        assert _lines(self.rule, ctx) == [1]


class TestTsBuildList:
    def test_unique_ids_and_prefix(self):
        rules = build_typescript_rules()
        ids = [r.id for r in rules]
        assert len(ids) == len(set(ids))
        assert len(ids) >= 6
        assert all(i.startswith("TS-") for i in ids)
        assert all(r.languages == ("typescript",) for r in rules)


class TestSharedSecurityRulesOnTs:
    """JS 共享安全规则应作用于 TS 文件。"""

    def test_eval_in_ts(self, make_ctx):
        from audit.detect.rules.js.javascript import EvalExecRule

        ctx = _ts(make_ctx, "const v = eval(src);\n")
        assert _lines(EvalExecRule(), ctx) == [1]

    def test_sql_concat_in_ts(self, make_ctx):
        from audit.detect.rules.js.javascript import SqlConcatRule

        ctx = _ts(make_ctx, 'db.query("SELECT * FROM users WHERE id = " + uid);\n')
        assert _lines(SqlConcatRule(), ctx) == [1]

    def test_hardcoded_secret_in_ts(self, make_ctx):
        from audit.detect.rules.js.javascript import HardcodedSecretRule

        ctx = _ts(make_ctx, 'const apiKey = "0123456789abcdef0123";\n')
        assert _lines(HardcodedSecretRule(), ctx) == [1]
