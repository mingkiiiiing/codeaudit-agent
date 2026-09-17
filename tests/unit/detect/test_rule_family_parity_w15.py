"""W15-D 规则族收敛奇偶校验（卡D 硬门禁）。

门禁语义：规则收敛重构对任何输入不得改变规则 id / severity / 默认值 /
报错文案 / 命中行号与数量。本文件从两个维度锁定：

1. **同构夹具奇偶校验**：四族参数化规则（LongFunction / DeepNesting /
   MagicNumber / TodoFixme）在行号镜像对齐的 Python 夹具与 JS 夹具上，
   命中数量、行号跨度、severity、meta 完全一致；文案在两语言本就相同的
   族（TodoFixme/DeepNesting）逐字节一致，在存在既定语言差异的族
   （LongFunction 尾注 / MagicNumber 示例文案）剔除差异片段后逐字节一致；
   差异夹具（未达阈值/负例）两侧均不误报。
2. **重构前常量锁定**：直接从 DEFAULT_REGISTRY（重构前的注册路径）读取
   规则实例，断言 id / severity / category / languages / mask_snippet 与
   重构前常量一致，并断言两侧薄子类确实挂接在参数化家族基类上。
"""

from __future__ import annotations

from audit.detect.base import Rule, RuleContext
from audit.detect.registry import DEFAULT_REGISTRY
from audit.detect.rules._rule_families import (
    DeepNestingRuleFamily,
    HardcodedSecretRuleFamily,
    LongFunctionRuleFamily,
    MagicNumberRuleFamily,
    TodoFixmeCommentRuleFamily,
)
from audit.detect.rules._scan_common import string_value
from audit.detect.rules.js.javascript import (
    DeepNestingRule as JsDeepNestingRule,
    HardcodedSecretRule as JsHardcodedSecretRule,
    LongFunctionRule as JsLongFunctionRule,
    MagicNumberRule as JsMagicNumberRule,
    TodoFixmeCommentRule as JsTodoFixmeCommentRule,
)
from audit.detect.rules.python import (
    DeepNestingRule as PyDeepNestingRule,
    HardcodedSecretRule as PySecretRule,
    LongFunctionRule as PyLongFunctionRule,
    MagicNumberRule as PyMagicNumberRule,
    TodoFixmeCommentRule as PyTodoFixmeCommentRule,
)
from audit.models import Category, Severity

# ---------------------------------------------------------------- 夹具构造（行号镜像对齐）


def _py_long_function(total_lines: int) -> str:
    """def 头 + (total_lines-1) 行函数体，函数作用域 [1, total_lines]。"""
    lines = ["def long_function():"]
    for i in range(1, total_lines):
        lines.append(f"    value_{i:03d} = {i}")
    return "\n".join(lines) + "\n"


def _js_long_function(total_lines: int) -> str:
    """function 头 + body + '}'，大括号配对作用域 [1, total_lines]（与 PY 版镜像）。"""
    lines = ["function long_function() {"]
    for i in range(1, total_lines - 1):
        lines.append(f"  value{i:03d} = {i};")
    lines.append("}")
    return "\n".join(lines) + "\n"


_PY_DEEP_NESTING = (
    "def handle(a, b, c):\n"
    "    if a:\n"
    "        if b:\n"
    "            if c:\n"
    "                return deep()\n"
)
_JS_DEEP_NESTING = (
    "function handle(a, b, c) {\n"
    "  if (a) {\n"
    "    if (b) {\n"
    "      if (c) {\n"
    "        return deep();\n"
    "      }\n"
    "    }\n"
    "  }\n"
    "}\n"
)
_PY_SHALLOW = (
    "def handle(a, b):\n"
    "    if a:\n"
    "        if b:\n"
    "            return 1\n"
)
_JS_SHALLOW = (
    "function handle(a, b) {\n"
    "  if (a) {\n"
    "    if (b) {\n"
    "      return 1;\n"
    "    }\n"
    "  }\n"
    "}\n"
)
_PY_MAGIC_POS = "timeout = 5000\n"
_JS_MAGIC_POS = "let timeout = 5000;\n"
_PY_MAGIC_NEG = "x = 999\n"
_JS_MAGIC_NEG = "let x = 999;\n"
_PY_TODO_POS = "def f():\n    run()  # TODO: 需要补全错误处理\n"
_JS_TODO_POS = "function f() {\n  run(); // TODO: 需要补全错误处理\n}\n"
_PY_TODO_NEG = "def f():\n    run()  # 说明文字\n"
_JS_TODO_NEG = "function f() {\n  run(); // 说明文字\n}\n"


# ---------------------------------------------------------------- 工具


def _hit_tuples(rule: Rule, source: str, rel_path: str, language: str):
    """直接构造 RuleContext 并返回 (start, end, severity, category, message, meta) 列表。"""
    ctx = RuleContext(rel_path=rel_path, language=language, source=source, lines=source.splitlines())
    return [(h.line_start, h.line_end, h.severity, h.category, h.message, h.meta) for h in rule.check(ctx)]


def _strip_language_variant(message: str) -> str:
    """剔除两语言**既定**的文案差异片段，其余部分必须逐字节一致。

    - LongFunction 尾注：PY「圈复杂度高」/ JS「分支爆炸」；
    - MagicNumber 示例：PY「THRESHOLD = 10000」/ JS「const MAX_RETRY = 3000」；
    - DeepNesting 缩进阈值数字：PY 固定 16（4 层×4 空格）/ JS 按缩进单位换算
      （2 空格文件为 8），数字归一但由专门断言锁定两侧取值。
    """
    return (
        message.replace("圈复杂度高", "<TAIL>")
        .replace("分支爆炸", "<TAIL>")
        .replace("THRESHOLD = 10000", "<HINT>")
        .replace("const MAX_RETRY = 3000", "<HINT>")
        .replace("（缩进 ≥16 空格）", "（缩进 ≥<N> 空格）")
        .replace("（缩进 ≥8 空格）", "（缩进 ≥<N> 空格）")
    )


# ---------------------------------------------------------------- 家族挂接与重构前常量


class TestFamilyWiringAndConstants:
    """薄子类挂接参数化家族 + 注册表 id/severity 与重构前常量一致。"""

    def test_family_wiring(self):
        assert issubclass(PyLongFunctionRule, LongFunctionRuleFamily)
        assert issubclass(JsLongFunctionRule, LongFunctionRuleFamily)
        assert issubclass(PyDeepNestingRule, DeepNestingRuleFamily)
        assert issubclass(JsDeepNestingRule, DeepNestingRuleFamily)
        assert issubclass(PyMagicNumberRule, MagicNumberRuleFamily)
        assert issubclass(JsMagicNumberRule, MagicNumberRuleFamily)
        assert issubclass(PyTodoFixmeCommentRule, TodoFixmeCommentRuleFamily)
        assert issubclass(JsTodoFixmeCommentRule, TodoFixmeCommentRuleFamily)

    def test_hardcoded_secret_family_hook(self):
        # W15-D 决策：检测强度两语言有意不同（check 不上收），仅元信息/契约位上收
        assert issubclass(JsHardcodedSecretRule, HardcodedSecretRuleFamily)
        assert issubclass(PySecretRule, HardcodedSecretRuleFamily)

    def test_registry_constants_unchanged(self):
        by_id = {r.id: r for r in DEFAULT_REGISTRY.all_rules}
        expected = {
            # 规则 id -> (category, severity, languages, mask_snippet)：均为重构前常量
            "PY-LONG-FUNCTION": (Category.STYLE, Severity.MEDIUM, ("python",), False),
            "JS-LONG-FUNCTION": (Category.STYLE, Severity.MEDIUM, ("javascript",), False),
            "PY-DEEP-NESTING": (Category.STYLE, Severity.MEDIUM, ("python",), False),
            "JS-DEEP-NESTING": (Category.STYLE, Severity.MEDIUM, ("javascript",), False),
            "PY-MAGIC-NUMBER": (Category.STYLE, Severity.LOW, ("python",), False),
            "JS-MAGIC-NUMBER": (Category.STYLE, Severity.LOW, ("javascript",), False),
            "PY-TODO-FIXME": (Category.STYLE, Severity.LOW, ("python",), False),
            "JS-TODO-FIXME": (Category.STYLE, Severity.LOW, ("javascript",), False),
            "PY-HARDCODED-SECRET": (Category.SECURITY, Severity.CRITICAL, ("python",), True),
            "JS-HARDCODED-SECRET": (
                Category.SECURITY,
                Severity.CRITICAL,
                ("javascript", "typescript"),
                True,
            ),
        }
        for rule_id, (cat, sev, langs, mask) in expected.items():
            rule = by_id[rule_id]  # 缺注册会直接 KeyError
            assert rule.category is cat, rule_id
            assert rule.severity is sev, rule_id
            assert rule.languages == langs, rule_id
            assert rule.mask_snippet is mask, rule_id
            assert rule.description, rule_id


# ---------------------------------------------------------------- 四族奇偶校验


class TestLongFunctionParity:
    def test_same_hits_on_mirrored_fixtures(self):
        py = _hit_tuples(PyLongFunctionRule(), _py_long_function(85), "m.py", "python")
        js = _hit_tuples(JsLongFunctionRule(), _js_long_function(85), "m.js", "javascript")
        assert len(py) == len(js) == 1
        assert (py[0][0], py[0][1], py[0][2], py[0][3], py[0][5]) == (
            js[0][0],
            js[0][1],
            js[0][2],
            js[0][3],
            js[0][5],
        )
        assert py[0][0] == 1 and py[0][1] == 85
        # 文案：剔除两语言既定尾注差异后逐字节一致
        assert _strip_language_variant(py[0][4]) == _strip_language_variant(js[0][4])

    def test_boundary_80_lines_no_hit(self, make_ctx):
        assert PyLongFunctionRule().check(make_ctx(_py_long_function(80))) == []
        assert JsLongFunctionRule().check(make_ctx(_js_long_function(80), "m.js", "javascript")) == []


class TestDeepNestingParity:
    def test_same_hits_on_mirrored_fixtures(self):
        py = _hit_tuples(PyDeepNestingRule(), _PY_DEEP_NESTING, "m.py", "python")
        js = _hit_tuples(JsDeepNestingRule(), _JS_DEEP_NESTING, "m.js", "javascript")
        assert len(py) == len(js) == 1
        assert (py[0][0], py[0][1], py[0][2], py[0][3], py[0][5]) == (
            js[0][0],
            js[0][1],
            js[0][2],
            js[0][3],
            js[0][5],
        )
        assert py[0][0] == 5 and py[0][1] == 5
        # 文案：剔除两语言既定阈值数字后逐字节一致；数字本身锁定历史口径
        assert _strip_language_variant(py[0][4]) == _strip_language_variant(js[0][4])
        assert "（缩进 ≥16 空格）" in py[0][4]  # PY：4 层 × 4 空格（固定口径）
        assert "（缩进 ≥8 空格）" in js[0][4]  # JS：4 层 × 2 空格（按文件缩进单位）

    def test_shallow_no_false_positive(self):
        assert _hit_tuples(PyDeepNestingRule(), _PY_SHALLOW, "m.py", "python") == []
        assert _hit_tuples(JsDeepNestingRule(), _JS_SHALLOW, "m.js", "javascript") == []


class TestMagicNumberParity:
    def test_same_hits_on_mirrored_fixtures(self):
        py = _hit_tuples(PyMagicNumberRule(), _PY_MAGIC_POS, "m.py", "python")
        js = _hit_tuples(JsMagicNumberRule(), _JS_MAGIC_POS, "m.js", "javascript")
        assert len(py) == len(js) == 1
        assert (py[0][0], py[0][1], py[0][2], py[0][3], py[0][5]) == (
            js[0][0],
            js[0][1],
            js[0][2],
            js[0][3],
            js[0][5],
        )
        assert py[0][5] == {"value": 5000.0}
        # 文案：剔除两语言既定示例差异后逐字节一致
        assert _strip_language_variant(py[0][4]) == _strip_language_variant(js[0][4])

    def test_below_threshold_no_false_positive(self):
        assert _hit_tuples(PyMagicNumberRule(), _PY_MAGIC_NEG, "m.py", "python") == []
        assert _hit_tuples(JsMagicNumberRule(), _JS_MAGIC_NEG, "m.js", "javascript") == []

    def test_language_specific_exemptions_preserved(self):
        # 既定语言差异锁定：JS 全大写具名常量豁免 / PY 不豁免（历史行为）
        assert _hit_tuples(JsMagicNumberRule(), "const MAX_RETRY = 3000;\n", "m.js", "javascript") == []
        py_hit = _hit_tuples(PyMagicNumberRule(), "MAX_RETRY = 3000\n", "m.py", "python")
        assert len(py_hit) == 1
        # 既定语言差异锁定：JS 十六进制字面量参与检查 / PY 正则不匹配（历史行为）
        assert len(_hit_tuples(JsMagicNumberRule(), "let mask = 0x1000;\n", "m.js", "javascript")) == 1
        assert _hit_tuples(PyMagicNumberRule(), "mask = 0x1000\n", "m.py", "python") == []


class TestTodoFixmeParity:
    def test_same_hits_on_mirrored_fixtures(self):
        py = _hit_tuples(PyTodoFixmeCommentRule(), _PY_TODO_POS, "m.py", "python")
        js = _hit_tuples(JsTodoFixmeCommentRule(), _JS_TODO_POS, "m.js", "javascript")
        assert py == js  # 文案两语言逐字节一致：全字段比对
        assert py[0][0] == 2
        assert py[0][5] == {"tag": "TODO"}

    def test_plain_comment_no_false_positive(self):
        assert _hit_tuples(PyTodoFixmeCommentRule(), _PY_TODO_NEG, "m.py", "python") == []
        assert _hit_tuples(JsTodoFixmeCommentRule(), _JS_TODO_NEG, "m.js", "javascript") == []


# ---------------------------------------------------------------- _string_value 收敛后的三语义锁定


class TestStringValueUnified:
    """W15-D：三处历史拷贝合并为 _scan_common.string_value，转义语义以参数显式化。"""

    BS = chr(92)  # 反斜杠字面量（避免转义歧义）

    def test_python_semantics_no_escape(self):
        # PY-HARDCODED-SECRET 历史语义：不做转义跳转，内容止于第一个同引号
        line = 'K = "a' + self.BS + '"b"'
        assert string_value(line, 4, escape_mode="none") == "a" + self.BS

    def test_js_semantics_skip_escape(self):
        # JS-HARDCODED-SECRET 历史语义：跳过转义两字符且不保留
        line = 'K = "a' + self.BS + '"b"'
        assert string_value(line, 4, escape_mode="skip") == "ab"

    def test_js_ext_semantics_keep_escape(self):
        # js_ext._string_literal_value 历史语义：跳过转义但保留原文两字符
        line = "K = `a" + self.BS + "`b`"
        assert string_value(line, 4, "`", escape_mode="keep") == "a" + self.BS + "`b"

    def test_unclosed_and_non_quote(self):
        assert string_value("K = 'x", 4) is None
        assert string_value("k = v", 4) is None  # quote_col 处不是引号
        assert string_value('K = "x"', 99) is None  # 越界
