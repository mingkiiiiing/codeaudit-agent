"""Python 静态规则库·圈复杂度数值化（W19-A，1 条 style/medium）。

与 python.py/python_ext.py 同一实现思路：基于 `_python_common` 的逐行掩码扫描
+ `function_ranges` 缩进作用域（不依赖 tree-sitter），保证行号精确落在真实代码
行上；`check()` 纯函数式，不修改 ctx、无 IO。

口径说明（CC = 决策点数 + 1，对齐 SonarQube/Radon 的标准定义）：
- 基线 1；以下决策点各计 1：
  - 分支：`if`、`elif`（`\belif\b` 与 `\bif\b` 互不重复计——`elif` 中 `if` 前
    是字母 `l`，不构成词边界）；三元表达式 `x if c else y` 的 `if` 同样计入分支
    （三元即 1 条新路径）；`else` 不计（不新增路径）。
  - 循环：`for`（含推导式 for）、`while`；`with` 不计（资源管理，非分支）。
  - 布尔运算：`and`、`or` 各计 1（短路点即一条新路径）。
  - 异常：`except` 每个处理器计 1（`try`/`finally` 不计）。
  - 断言：`assert` 计 1（assert 失败即一条分支路径）。
- 计数在掩码行（字符串字面量内容与注释已置空，见 ``PyScan.masked``）上按
  ``\b`` 词边界做 token 匹配，天然避免字符串/注释误计。
- 已知限制（可接受误差，均为低估方向）：
  - f-string 的 ``{...}`` 插值表达式随字符串整体掩码，其中的决策点不计；
  - 3.10 `match/case` 的 case 分支未计入；
  - 跨行续行表达式按物理行分别计数（token 总数不受影响）。

阈值语义：环境变量 ``CODEAUDIT_CC_THRESHOLD``（默认 10）；**CC > 阈值才报**
（对齐 SonarQube 默认 10 的语义：10 及以下不报，11 报）。每函数最多报 1 条，
命中行 = 函数 def 行。类方法/嵌套函数同样被 ``function_ranges`` 覆盖：嵌套
函数单独计其自身范围，不再累加进外层函数（与 Radon 口径一致）。
"""

from __future__ import annotations

import os
import re
from typing import Pattern

from audit.detect.base import Rule, RuleContext
from audit.detect.rules._python_common import FuncRange, function_ranges, get_scan
from audit.models import Category, RuleHit, Severity

__all__ = ["PyCyclomaticComplexityRule", "build_py_complexity_rules"]

_DEFAULT_THRESHOLD = 10
_THRESHOLD_ENV = "CODEAUDIT_CC_THRESHOLD"


def _threshold() -> int:
    """读阈值：env 覆盖优先，非法值回退默认 10（保证同输入确定性）。"""
    raw = os.environ.get(_THRESHOLD_ENV, "").strip()
    if not raw:
        return _DEFAULT_THRESHOLD
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_THRESHOLD


# 决策点 token → 计数归类键。全部走 \b 词边界匹配：`elif` 不被 `\bif\b` 命中、
# `format`/`ordering` 等标识符不被 `\bfor\b`/`\bor\b` 命中（下划线是词字符）。
_POINT_RES: tuple[tuple[str, Pattern[str]], ...] = (
    ("if", re.compile(r"\bif\b")),
    ("elif", re.compile(r"\belif\b")),
    ("for", re.compile(r"\bfor\b")),
    ("while", re.compile(r"\bwhile\b")),
    ("and", re.compile(r"\band\b")),
    ("or", re.compile(r"\bor\b")),
    ("except", re.compile(r"\bexcept\b")),
    ("assert", re.compile(r"\bassert\b")),
)

# 归类展示分组（顺序即 message 中的固定顺序，0 值组不展示）
_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("分支", ("if", "elif")),
    ("循环", ("for", "while")),
    ("布尔运算", ("and", "or")),
    ("异常", ("except",)),
    ("断言", ("assert",)),
)


class PyCyclomaticComplexityRule(Rule):
    """圈复杂度超过阈值（CC = 决策点数 + 1，>阈值才报）。"""

    id = "PY-CYCLOMATIC-COMPLEXITY"
    category = Category.STYLE
    severity = Severity.MEDIUM
    description = (
        "函数圈复杂度超过阈值：决策分支过多意味着路径组合爆炸，难以测试与维护；"
        "应拆分为更小函数，或用卫语句/字典分派降低分支。"
    )

    bad_example = (
        "def grade(score, bonus):\n"
        "    if score < 0 or score > 100:\n"
        "        return 0\n"
        "    elif score >= 90 and bonus:\n"
        "        return 10\n"
        "    elif score >= 80 and not bonus:\n"
        "        return 8\n"
        "    # ……十余个分支串行排布，路径组合难以穷举\n"
    )
    good_example = (
        "_GRADE_TABLE = ((90, 10), (80, 8), (60, 6))\n"
        "\n"
        "def grade(score, bonus):\n"
        "    if score < 0 or score > 100:\n"
        "        return 0\n"
        "    return next((v for low, v in _GRADE_TABLE if score >= low), 0) + bonus\n"
    )

    languages = ("python",)

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        ranges = function_ranges(scan.masked)
        if not ranges:
            return []
        threshold = _threshold()
        hits: list[RuleHit] = []
        for fr in ranges:
            counts = self._count_points(scan.masked, fr, ranges)
            cc = 1 + sum(counts.values())
            if cc <= threshold:
                continue
            hits.append(self._hit(ctx, fr, cc, threshold, counts))
        return hits

    @staticmethod
    def _nested_spans(fr: FuncRange, ranges: list[FuncRange]) -> list[tuple[int, int]]:
        """完全落在 fr 内部的嵌套函数行区间：从外层计数中剔除（单独计）。"""
        return [
            (other.start, other.end)
            for other in ranges
            if other is not fr and other.start > fr.start and other.end <= fr.end
        ]

    def _count_points(
        self, masked: list[str], fr: FuncRange, ranges: list[FuncRange]
    ) -> dict[str, int]:
        """在函数范围的掩码行上按 token 计决策点（嵌套函数行剔除）。"""
        counts = {key: 0 for key, _p in _POINT_RES}
        spans = self._nested_spans(fr, ranges)
        for lineno in range(fr.start, fr.end + 1):
            if any(a <= lineno <= b for a, b in spans):
                continue
            ln = masked[lineno - 1]
            for key, pat in _POINT_RES:
                counts[key] += len(pat.findall(ln))
        return counts

    def _hit(
        self,
        ctx: RuleContext,
        fr: FuncRange,
        cc: int,
        threshold: int,
        counts: dict[str, int],
    ) -> RuleHit:
        dist = "、".join(
            f"{label} {sum(counts[k] for k in keys)}" for label, keys in _GROUPS
            if sum(counts[k] for k in keys) > 0
        ) or "无决策点明细"
        return self.make_hit(
            ctx,
            fr.start,
            fr.start,
            f"函数 `{fr.name}` 圈复杂度 {cc}（阈值 {threshold}）：{dist}"
            "——建议拆分为更小函数或用卫语句/字典分派降低分支",
            meta={
                "cc": cc,
                "threshold": threshold,
                "branches": counts["if"] + counts["elif"],
                "loops": counts["for"] + counts["while"],
                "boolean_ops": counts["and"] + counts["or"],
                "exceptions": counts["except"],
                "asserts": counts["assert"],
            },
        )


def build_py_complexity_rules() -> list[Rule]:
    """构建圈复杂度规则实例（registry 接线由集成人统一处理）。"""
    return [PyCyclomaticComplexityRule()]
