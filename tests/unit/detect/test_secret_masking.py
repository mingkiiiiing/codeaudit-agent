"""W14-A1（C-1 / NFR-11）：硬编码密钥 snippet 打码回归测试。

NFR-11 与 README「数据隐私」承诺：检测到的硬编码密钥在报告中打码呈现。
本文件集中覆盖：
- 打码函数 mask_string_literals 的字面量形态（单/双/反引号、转义、空串）；
- Rule.mask_snippet 类属性契约：仅置 True 的规则（两条密钥规则）打码，
  其余规则 snippet 行为不变；
- 管线级集中回归（--no-llm 等价路径：run_detection 无 review_fn）：
  临时文件写入已知假密钥，断言最终 Issue 的 code_snippet 不含明文密钥，
  且行号/文件字段正常、message 仍可读。PY 与 JS 侧都要覆盖。
"""

from __future__ import annotations

import asyncio

from audit.detect.base import Rule, RuleContext, mask_string_literals
from audit.detect.engine import hits_to_issues, run_detection
from audit.pipeline import PipelineContext

# 已知的假密钥（仅测试用，非真实凭据）
PY_SK_KEY = "sk-abcdef1234567890abcdef1234567890"
PY_PASSWORD = "mysupersecretkey123456"
JS_API_KEY = "0123456789abcdef012345"

SECRET_PY = (
    "import os\n"
    "\n"
    f'API_KEY = "{PY_SK_KEY}"\n'
    f'DB_PASSWORD = "{PY_PASSWORD}"\n'
    'SAFE_URL = "https://example.com/api"\n'
)

SECRET_JS = (
    f'const apiKey = "{JS_API_KEY}";\n'
    'const config = { dbPassword: "mysupersecretkey123456" };\n'
)


# ---------------------------------------------------------------- 打码函数


class TestMaskStringLiterals:
    def test_double_quote(self):
        assert mask_string_literals('API_KEY = "sk-abc123"') == 'API_KEY = "********"'

    def test_single_quote(self):
        assert mask_string_literals("API_KEY = 'sk-abc123'") == "API_KEY = '********'"

    def test_backtick(self):
        assert mask_string_literals("const t = `sk-abc123`;") == "const t = `********`;"

    def test_keeps_variable_and_quotes(self):
        out = mask_string_literals('DB_PASSWORD = "mysupersecretkey123456"')
        assert out.startswith('DB_PASSWORD = "')
        assert out.endswith('"')
        assert "mysupersecret" not in out

    def test_empty_literal_kept(self):
        assert mask_string_literals('x = ""') == 'x = ""'

    def test_escapes(self):
        # 转义引号不打断字面量：整个 "a\"b" 被视为一个字面量打码
        assert mask_string_literals('k = "a\\"b"') == 'k = "********"'

    def test_multiple_literals_on_one_line(self):
        out = mask_string_literals('a = "one"; b = "two"')
        assert out == 'a = "********"; b = "********"'

    def test_no_literal_unchanged(self):
        assert mask_string_literals("x = 1 + 2") == "x = 1 + 2"

    def test_unclosed_quote_unchanged(self):
        assert mask_string_literals('k = "unterminated') == 'k = "unterminated'


# ---------------------------------------------------------------- 基类契约


class _MaskedRule(Rule):
    id = "T-MASKED"
    mask_snippet = True

    def check(self, ctx: RuleContext):
        return []


class _PlainRule(Rule):
    id = "T-PLAIN"

    def check(self, ctx: RuleContext):
        return []


class TestMakeHitMaskContract:
    def _ctx(self, tmp_path_name: str = "m.py") -> RuleContext:
        source = 'API_KEY = "sk-abc123"\n'
        lines = source.splitlines()
        return RuleContext(rel_path=tmp_path_name, language="python", source=source, lines=lines)

    def test_masked_rule_masks_backfilled_snippet(self):
        hit = _MaskedRule().make_hit(self._ctx(), 1, 1, "msg")
        assert hit.snippet == 'API_KEY = "********"'

    def test_masked_rule_masks_explicit_snippet_too(self):
        # 显式传 snippet 时同样兜底打码（规则声明 mask_snippet 即表达"不得明文"）
        hit = _MaskedRule().make_hit(self._ctx(), 1, 1, "msg", snippet='API_KEY = "sk-abc123"')
        assert hit.snippet == 'API_KEY = "********"'

    def test_plain_rule_keeps_snippet(self):
        hit = _PlainRule().make_hit(self._ctx(), 1, 1, "msg")
        assert hit.snippet == 'API_KEY = "sk-abc123"'


# ---------------------------------------------------------------- 管线级回归


def _run_detection(tmp_dir, files: dict[str, str]) -> PipelineContext:
    """--no-llm 等价路径：真实工作副本 + run_detection（无 review_fn → 纯规则）。"""
    from audit.config import AuditConfig
    from audit.llm.base import FakeLLMClient
    from audit.pipeline import PipelineContext as PC
    from audit.workspace import WorkspaceContext

    for name, text in files.items():
        path = tmp_dir / "src" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    workspace = WorkspaceContext(
        audit_id="w14a1",
        src_root=tmp_dir / "src",
        work_root=tmp_dir,
        db_path=tmp_dir / "index.db",
    )

    async def _emit(event: dict) -> None:
        return None

    ctx = PC(
        config=AuditConfig(source_path=str(workspace.src_root)),
        workspace=workspace,
        llm=FakeLLMClient(),
        emitter=_emit,
    )
    asyncio.run(run_detection(ctx))
    return ctx


class TestPipelineSecretMasking:
    def test_python_snippets_masked(self, tmp_path):
        ctx = _run_detection(tmp_path, {"leaked.py": SECRET_PY})
        issues = [i for i in ctx.issues if i.file == "leaked.py"]
        assert len(issues) == 2  # API_KEY 与 DB_PASSWORD 各一条，SAFE_URL 不报

        by_line = {i.line_start: i for i in issues}
        assert set(by_line) == {3, 4}

        key_issue = by_line[3]
        assert key_issue.code_snippet == 'API_KEY = "********"'
        pwd_issue = by_line[4]
        assert pwd_issue.code_snippet == 'DB_PASSWORD = "********"'

        # 报告全量字段无明文密钥
        for secret in (PY_SK_KEY, PY_PASSWORD):
            for issue in ctx.issues:
                assert secret not in issue.code_snippet
                assert secret not in issue.description

        # message 仍可读（标题与描述保留上下文）
        assert "疑似敏感凭据" in key_issue.title
        assert "疑似敏感凭据" in key_issue.description
        assert key_issue.file == "leaked.py"

    def test_js_snippets_masked(self, tmp_path):
        ctx = _run_detection(tmp_path, {"leaked.js": SECRET_JS})
        issues = [i for i in ctx.issues if i.file == "leaked.js"]
        assert len(issues) == 2

        by_line = {i.line_start: i for i in issues}
        assert set(by_line) == {1, 2}
        assert by_line[1].code_snippet == 'const apiKey = "********";'
        assert by_line[2].code_snippet == 'const config = { dbPassword: "********" };'

        for issue in ctx.issues:
            assert JS_API_KEY not in issue.code_snippet
            assert "mysupersecretkey123456" not in issue.code_snippet
            assert issue.line_start >= 1

    def test_hits_to_issues_passthrough_masked(self, tmp_path):
        """hits_to_issues 直接透传已打码的 hit.snippet（规则源头打码，引擎不改写）。"""
        source = SECRET_PY
        lines = source.splitlines()
        rc = RuleContext(rel_path="leaked.py", language="python", source=source, lines=lines)
        from audit.detect.base import RuleRegistry
        from audit.detect.rules import build_python_rules

        reg = RuleRegistry()
        reg.register_all(build_python_rules())
        hits = []
        for rule in reg.rules_for("python"):
            hits.extend(rule.check(rc))
        issues = hits_to_issues(hits, registry=reg)
        assert issues
        for secret in (PY_SK_KEY, PY_PASSWORD):
            assert all(secret not in i.code_snippet for i in issues)
