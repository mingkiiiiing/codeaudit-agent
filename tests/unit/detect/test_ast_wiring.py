"""P0-3 AST 接线测试：引擎为 python 文件解析 AST 传入 ctx.tree + 降级路径。

覆盖：
- build_rule_contexts：python 文件 tree 非 None / 非 python 文件 tree 为 None /
  接线统计写 ctx.extra["ast_wiring"] / 环境开关 CODEAUDIT_DISABLE_AST=1 全降级；
- AstParseGate：无解析器语言返回 None、超预算熔断（本文件降级 + 后续不再解析）；
- _scan_file_worker：worker 端同口径解析（行为等价铁律的并行侧）；
- 语法破损源码：容错解析不阻断扫描。
"""

from __future__ import annotations

from audit.detect.ast_util import AstParseGate, ast_enabled
from audit.detect.engine import _scan_file_worker, build_rule_contexts
from audit.detect.registry import get_registry
from audit.detect.rules.python import SqlInjectionConcatRule


class TestBuildRuleContextsWiring:
    def test_python_files_get_tree(self, pipeline_ctx):
        contexts = build_rule_contexts(pipeline_ctx)
        py = [rc for rc in contexts if rc.language == "python"]
        assert py, "demo_proj 应含 python 文件"
        assert all(rc.tree is not None for rc in py)

    def test_ast_wiring_stats_recorded(self, pipeline_ctx):
        contexts = build_rule_contexts(pipeline_ctx)
        n_py = sum(1 for rc in contexts if rc.language == "python")
        info = pipeline_ctx.extra["ast_wiring"]
        assert info["enabled"] is True
        assert info["parsed"] == n_py
        assert info["degraded"] == 0
        assert info["reason"] is None

    def test_env_switch_disables_ast(self, pipeline_ctx, monkeypatch):
        monkeypatch.setenv("CODEAUDIT_DISABLE_AST", "1")
        contexts = build_rule_contexts(pipeline_ctx)
        assert all(rc.tree is None for rc in contexts)
        info = pipeline_ctx.extra["ast_wiring"]
        assert info["enabled"] is False
        assert info["parsed"] == 0

    def test_non_python_language_gets_no_tree(self, tmp_path, monkeypatch):
        """非 python 源文件（js）不解析 AST：tree 保持 None，python 正常接线。"""
        from audit.config import AuditConfig
        from audit.llm.base import FakeLLMClient
        from audit.pipeline import PipelineContext
        from audit.workspace import WorkspaceContext

        monkeypatch.delenv("CODEAUDIT_DISABLE_AST", raising=False)
        src = tmp_path / "src"
        src.mkdir()
        (src / "m.py").write_text("x = 1\n", encoding="utf-8")
        (src / "web.js").write_text("var x = 1;\n", encoding="utf-8")
        workspace = WorkspaceContext(
            audit_id="wtest001",
            src_root=src,
            work_root=tmp_path,
            db_path=tmp_path / "index.db",
        )
        ctx = PipelineContext(
            config=AuditConfig(source_path=str(src)),
            workspace=workspace,
            llm=FakeLLMClient(),
            emitter=lambda event: None,
        )
        contexts = {rc.rel_path: rc for rc in build_rule_contexts(ctx)}
        assert contexts["m.py"].tree is not None
        assert contexts["web.js"].tree is None


class TestAstParseGate:
    def test_unsupported_language_degrades(self):
        gate = AstParseGate()
        assert gate.parse("cobol", "MOVE 1 TO X.\n") is None
        assert gate.stats["degraded"] == 1

    def test_timeout_budget_trips_circuit_breaker(self):
        gate = AstParseGate(per_file_budget_s=0.0)  # 预算 0：任何解析必然超时
        src = "x = 1\n"
        assert gate.parse("python", src) is None  # 本文件降级
        assert gate.disabled_reason is not None
        assert "超预算" in gate.disabled_reason
        assert gate.parse("python", src) is None  # 熔断：后续不再解析
        assert gate.stats["degraded"] == 2
        assert gate.stats["parsed"] == 0

    def test_env_switch_short_circuits(self, monkeypatch):
        monkeypatch.setenv("CODEAUDIT_DISABLE_AST", "1")
        assert ast_enabled() is False
        gate = AstParseGate()
        assert gate.parse("python", "x = 1\n") is None
        monkeypatch.setenv("CODEAUDIT_DISABLE_AST", "0")
        assert ast_enabled() is True

    def test_broken_syntax_still_returns_usable_tree(self):
        """tree-sitter 容错解析：语法破损也返回可用部分树（绝不阻断扫描）。"""
        gate = AstParseGate()
        tree = gate.parse("python", "def f(:\n    pass\n")  # 语法破损
        assert tree is not None
        assert gate.stats["parsed"] == 1


class TestWorkerWiring:
    def test_worker_parses_tree_and_confirms(self):
        """worker 端（并行路径）与串行同口径解析 AST：SQL 命中带 ast_confirmed。"""
        registry = get_registry()
        src = 'uid = input()\ncur.execute("SELECT * FROM users WHERE id=" + uid)\n'
        rules = [r for r in registry.rules_for("python") if r.id == "PY-SQL-INJECTION"]
        payload = ("m.py", "python", src, [], tuple(), tuple(rules))
        hit_dicts, errors = _scan_file_worker(payload)
        assert errors == {}
        assert hit_dicts, "SQL 拼接应命中"
        assert hit_dicts[0]["meta"]["ast_confirmed"] is True
        assert hit_dicts[0]["meta"]["confidence"] == 0.8

    def test_worker_env_switch_degrades_to_line_level(self, monkeypatch):
        """环境开关在 worker 端同样生效：tree=None → 行级兜底路径（无 ast_confirmed）。"""
        monkeypatch.setenv("CODEAUDIT_DISABLE_AST", "1")
        src = 'uid = input()\ncur.execute("SELECT * FROM users WHERE id=" + uid)\n'
        payload = ("m.py", "python", src, [], tuple(), tuple([SqlInjectionConcatRule()]))
        hit_dicts, errors = _scan_file_worker(payload)
        assert errors == {}
        assert hit_dicts, "行级兜底路径必须保留既有命中（金标不回退）"
        assert "ast_confirmed" not in hit_dicts[0]["meta"]
