"""W16 死代码检测（audit.detect.deadcode）单测：全离线，tmp_path 造项目。"""

from __future__ import annotations

from pathlib import Path

import pytest

from audit.detect import deadcode
from audit.models import Category, Issue, IssueSource, Severity


# ---------------------------------------------------------------- 最小 stub


class _StubWorkspace:
    """最小工作区 stub：source_files / rel / read_file_text 闭包 tmp 目录。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def source_files(self, languages=None):
        return sorted(p for p in self.root.rglob("*") if p.is_file())

    def rel(self, path) -> str:
        return Path(path).resolve().relative_to(self.root.resolve()).as_posix()

    def read_file_text(self, rel) -> str:
        return (self.root / rel).read_text(encoding="utf-8")


class _StubCtx:
    """最小 PipelineContext stub：仅携带 workspace 与 extra（防御式降级用）。"""

    def __init__(self, root: Path) -> None:
        self.workspace = _StubWorkspace(root)
        self.extra: dict = {}


@pytest.fixture
def make_proj_ctx(tmp_path):
    """按 {相对路径: 文本} 在 tmp_path 落盘并返回 stub ctx 的工厂。"""

    def _make(files: dict[str, str]) -> _StubCtx:
        for rel, text in files.items():
            target = tmp_path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return _StubCtx(tmp_path)

    return _make


def _rule_ids(issues: list[Issue]) -> set[str]:
    """提取 Issue.evidence 中的规则 id 集合。"""
    ids = set()
    for issue in issues:
        for item in issue.evidence:
            if item.startswith("rule:"):
                ids.add(item.split(":", 1)[1])
    return ids


# ---------------------------------------------------------------- Python 用例


class TestPyDeadcode:
    def test_private_func_zero_refs_hit(self, make_proj_ctx):
        """模块级私有函数全库零引用 => PY-DEADCODE，style/low。"""
        ctx = make_proj_ctx(
            {
                "app.py": (
                    "def _helper(x):\n"
                    "    return x * 2\n"
                    "\n"
                    "\n"
                    "def public_main():\n"
                    "    return 1\n"
                )
            }
        )
        issues = deadcode.run(ctx)
        assert len(issues) == 1
        issue = issues[0]
        assert isinstance(issue, Issue) and issue.id == ""  # id 由 engine 统一补
        assert "rule:PY-DEADCODE" in issue.evidence
        assert issue.category == Category.STYLE
        assert issue.severity == Severity.LOW
        assert issue.source == IssueSource.RULE
        assert issue.confidence == pytest.approx(0.7)
        assert issue.file == "app.py"
        assert issue.line_start == 1 and issue.line_end == 1
        # description 必须带保守口径说明
        assert "静态保守口径" in issue.description
        assert "反射/动态调用" in issue.description

    def test_called_func_not_reported(self, make_proj_ctx):
        """私有函数被调用（引用计数 > 1）不报。"""
        ctx = make_proj_ctx(
            {
                "app.py": (
                    "def _helper(x):\n"
                    "    return x * 2\n"
                    "\n"
                    "\n"
                    "def public_main():\n"
                    "    return _helper(21)\n"
                )
            }
        )
        assert deadcode.run(ctx) == []

    def test_init_py_exempt(self, make_proj_ctx):
        """__init__.py 中定义（re-export 惯例）豁免。"""
        ctx = make_proj_ctx({"pkg/__init__.py": "def _factory():\n    return 1\n"})
        assert deadcode.run(ctx) == []

    def test_decorated_func_exempt(self, make_proj_ctx):
        """def 上方最近非空行以 @ 开头（装饰器注册，如 Flask 路由）豁免。"""
        ctx = make_proj_ctx(
            {
                "app.py": (
                    "@app.route('/ping')\n"
                    "def _handler():\n"
                    "    return 'pong'\n"
                )
            }
        )
        assert deadcode.run(ctx) == []

    def test_test_files_exempt(self, make_proj_ctx):
        """tests/ 目录或 test_ 前缀文件中的定义不采集。"""
        ctx = make_proj_ctx(
            {
                "tests/support.py": "def _fake_conn():\n    return 1\n",
                "test_thing.py": "def _make_case():\n    return 2\n",
            }
        )
        assert deadcode.run(ctx) == []

    def test_all_mention_exempt(self, make_proj_ctx):
        """被 __all__ 字符串提及的私有符号豁免（视为对外导出）。"""
        ctx = make_proj_ctx(
            {
                "api.py": (
                    "def _api():\n"
                    "    return 1\n"
                    "\n"
                    '__all__ = ["_api"]\n'
                )
            }
        )
        assert deadcode.run(ctx) == []

    def test_module_var_dead_hit(self, make_proj_ctx):
        """模块级私有变量零引用 => 命中（对应语料 _unused_var = 123 场景）。"""
        ctx = make_proj_ctx({"m.py": "_UNUSED_VAR = 123\n"})
        issues = deadcode.run(ctx)
        assert len(issues) == 1
        assert "rule:PY-DEADCODE" in issues[0].evidence
        assert issues[0].line_start == 1

    def test_module_var_referenced_not_reported(self, make_proj_ctx):
        """被函数引用的模块级私有变量不报（对应语料 _CACHE 场景，验证引用计数）。"""
        ctx = make_proj_ctx(
            {
                "m.py": (
                    "_CACHE = []\n"
                    "\n"
                    "\n"
                    "def cache_forever(x):\n"
                    "    _CACHE.append(x)\n"
                    "    return len(_CACHE)\n"
                )
            }
        )
        assert deadcode.run(ctx) == []

    def test_dunder_not_collected(self, make_proj_ctx):
        """dunder 名（__x__ 形态）不是单下划线私有，不采集。"""
        ctx = make_proj_ctx({"m.py": "__version__ = '0.1'\n"})
        assert deadcode.run(ctx) == []

    def test_workspace_failure_degrades(self):
        """workspace 异常时降级：不抛异常，返回空列表并记账。"""

        class _BadWorkspace:
            def source_files(self, languages=None):
                raise RuntimeError("boom")

        class _BadCtx:
            workspace = _BadWorkspace()
            extra: dict = {}

        ctx = _BadCtx()
        assert deadcode.run(ctx) == []
        assert "audit.detect.deadcode" in ctx.extra["post_scan_errors"]


# ---------------------------------------------------------------- JS/TS 用例


class TestJsDeadcode:
    def test_non_export_func_zero_refs_hit(self, make_proj_ctx):
        """非 export 的顶层函数零引用 => JS-DEADCODE。"""
        ctx = make_proj_ctx(
            {
                "app.js": (
                    "function unusedFn() {\n"
                    "  return 1;\n"
                    "}\n"
                    "\n"
                    "function otherFn(a) {\n"
                    "  return a + 1;\n"
                    "}\n"
                    "\n"
                    "const warmed = otherFn(1);\n"
                )
            }
        )
        issues = deadcode.run(ctx)
        assert len(issues) == 1  # otherFn 被引用不报，仅 unusedFn 命中
        issue = issues[0]
        assert issue.file == "app.js"
        assert issue.line_start == 1
        assert "rule:JS-DEADCODE" in issue.evidence
        assert "静态保守口径" in issue.description

    def test_export_func_exempt(self, make_proj_ctx):
        """export function 天然豁免（不匹配采集正则）。"""
        ctx = make_proj_ctx(
            {
                "app.js": (
                    "export function usedFn(a) {\n"
                    "  return a + 1;\n"
                    "}\n"
                ),
                "main.js": "import { usedFn } from './app.js';\nusedFn(1);\n",
            }
        )
        assert deadcode.run(ctx) == []

    def test_module_exports_exempt(self, make_proj_ctx):
        """赋值给 module.exports 的函数豁免。"""
        ctx = make_proj_ctx(
            {
                "legacy.js": (
                    "function legacyFn(a) {\n"
                    "  return a * 2;\n"
                    "}\n"
                    "\n"
                    "module.exports.legacyFn = legacyFn;\n"
                )
            }
        )
        assert deadcode.run(ctx) == []

    def test_string_mention_still_reported(self, make_proj_ctx):
        """被字符串提及不算引用：照报（已剔除字符串字面量后计数）。"""
        ctx = make_proj_ctx(
            {
                "a.js": "function unusedFn() {\n  return 1;\n}\n",
                "b.js": "const hint = 'unusedFn';\n",
            }
        )
        issues = deadcode.run(ctx)
        assert len(issues) == 1
        assert issues[0].file == "a.js"
        assert "rule:JS-DEADCODE" in issues[0].evidence

    def test_ts_rule_id(self, make_proj_ctx):
        """typescript 顶层非导出函数 => TS-DEADCODE。"""
        ctx = make_proj_ctx(
            {
                "util.ts": (
                    "export interface P {\n  id: number;\n}\n"
                    "\n"
                    "function unusedTsFn(p: P): number {\n"
                    "  return p.id;\n"
                    "}\n"
                )
            }
        )
        issues = deadcode.run(ctx)
        assert len(issues) == 1
        assert "rule:TS-DEADCODE" in issues[0].evidence
        assert issues[0].line_start == 5

    def test_clean_project_zero_hit(self, make_proj_ctx):
        """全部导出且无死符号的项目 0 命中。"""
        ctx = make_proj_ctx(
            {
                "a.js": "export function pick(items) {\n  return items;\n}\n",
                "b.ts": "export function join(a: string, b: string): string {\n  return a + b;\n}\n",
            }
        )
        assert deadcode.run(ctx) == []
