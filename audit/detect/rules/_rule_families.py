"""同构规则族参数化基类（W15-D）：PY/JS 成对规则的共享算法收敛于此。

W15-D 设计决策：
- 每个族只上收两语言**逐字一致**的算法骨架与公共元信息（category/severity/
  description/阈值常量），语言差异全部以「显式钩子」参数化，禁止行为耦合：
  - ``_get_scan``：语言子类以 ``staticmethod(get_scan)`` 绑定各自 common 的
    掩码扫描器（PyScan/JsScan 产物形状不同）；
  - ``_function_ranges``：绑定语言版函数作用域推断（PY 按缩进 / JS 按大括号配对）；
  - ``_min_indent``/``_is_deep``：DeepNesting 的层级口径（PY 固定 4 空格且跳过
    纯引号残留行 / JS 按文件缩进单位换算）；
  - ``_NUM_RE``/``_skip_line``/``_parse_number``/``_EXAMPLE_HINT``：MagicNumber
    的数字口径（JS 支持十六进制与具名常量豁免）与文案示例；
  - ``_TAIL_DETAIL``：LongFunction 消息尾注（两语言文案本就不同）。
- 规则 id（PY-/JS- 前缀）永远留在语言侧薄子类上，保证注册表 id 与文案
  对任何输入逐字节不变。
- HardcodedSecret 两语言检测强度**有意不同**（PY：分词+熵闸门 / JS：子串提示+
  已知前缀），check() 不上收，仅收敛元信息与 mask_snippet 契约位，见类注释。
"""

from __future__ import annotations

import re
from typing import Pattern

from audit.detect.base import Rule, RuleContext
from audit.models import Category, RuleHit, Severity

__all__ = [
    "DeepNestingRuleFamily",
    "HardcodedSecretRuleFamily",
    "LongFunctionRuleFamily",
    "MagicNumberRuleFamily",
    "TodoFixmeCommentRuleFamily",
]


# ---------------------------------------------------------------- 扫描器绑定钩子


def _lang_get_scan(ctx_lines: list[str], meta: dict):
    """族内钩子占位（W15-D）：语言侧薄子类以 ``_get_scan = staticmethod(get_scan)``
    覆盖为各自 common 的 get_scan（PyScan/JsScan），家族算法不感知扫描器差异。"""
    raise NotImplementedError


# ---------------------------------------------------------------- TodoFixme 族


class TodoFixmeCommentRuleFamily(Rule):
    """TODO/FIXME/HACK 注释残留规则族（W15-D 参数化）。

    两语言 check 逐字一致：仅消费 ``scan.comments``（PyScan/JsScan 均产出），
    语言差异只剩 id 前缀与扫描器绑定。
    """

    category = Category.STYLE
    severity = Severity.LOW
    description = "注释中的 TODO/FIXME/HACK 标记：代表已知未完成事项或临时绕过，应在交付前清理或建跟踪项。"

    _RE: Pattern[str] = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")

    _get_scan = staticmethod(_lang_get_scan)

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = self._get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for lineno, text in scan.comments:
            m = self._RE.search(text)
            if m:
                hits.append(
                    self.make_hit(
                        ctx,
                        lineno,
                        lineno,
                        f"第 {lineno} 行注释存在 `{m.group(1)}` 标记：{text.strip()[:80]}——"
                        "这是显式的未完成事项，应转为任务跟踪或立即处理，避免长期滞留。",
                        meta={"tag": m.group(1)},
                    )
                )
        return hits


# ---------------------------------------------------------------- LongFunction 族


class LongFunctionRuleFamily(Rule):
    """函数超过 80 行规则族（W15-D 参数化）。

    算法骨架两语言逐字一致；差异仅两处并以钩子显式化：消息尾注
    ``_TAIL_DETAIL``（PY「圈复杂度高」/ JS「分支爆炸」）与
    ``_function_ranges``（PY 缩进口径 / JS 大括号配对口径）。
    """

    category = Category.STYLE
    severity = Severity.MEDIUM
    description = "函数体超过 80 行：职责过多、难以测试与复用，应拆分为更小的函数。"

    MAX_LINES = 80
    _TAIL_DETAIL: str = ""

    _get_scan = staticmethod(_lang_get_scan)

    @staticmethod
    def _function_ranges(masked: list[str]):
        """语言子类覆盖：绑定各自 common 的 function_ranges。"""
        raise NotImplementedError

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = self._get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for fr in self._function_ranges(scan.masked):
            length = fr.end - fr.start + 1
            if length > self.MAX_LINES:
                hits.append(
                    self.make_hit(
                        ctx,
                        fr.start,
                        fr.end,
                        f"函数 `{fr.name}` 共 {length} 行（第 {fr.start}~{fr.end} 行），超过 80 行上限："
                        f"过长函数通常职责混杂、{self._TAIL_DETAIL}，难以测试和维护；建议按职责拆分。",
                        meta={"lines": length},
                    )
                )
        return hits


# ---------------------------------------------------------------- DeepNesting 族


class DeepNestingRuleFamily(Rule):
    """缩进达到 4 层及以上规则族（W15-D 参数化）。

    分组算法与命中文案两语言逐字一致；层级口径以钩子显式化：
    ``_min_indent``（PY：LEVEL_THRESHOLD*4 固定口径 / JS：按文件缩进单位换算）
    与 ``_is_deep``（PY 额外跳过掩码后仅剩引号的残留行）。
    """

    category = Category.STYLE
    severity = Severity.MEDIUM
    description = "代码嵌套达到 4 层以上：认知负担大、分支组合爆炸，应通过提前返回/抽取函数降低深度。"

    LEVEL_THRESHOLD = 4

    _get_scan = staticmethod(_lang_get_scan)

    def _min_indent(self, masked: list[str]) -> int:
        """语言子类覆盖：返回「判定为深层」的最小缩进宽度。"""
        raise NotImplementedError

    def _is_deep(self, ln: str, stripped: str, min_indent: int) -> bool:
        """语言子类覆盖：该行（stripped 为其去空白文本）是否达到深层缩进。"""
        raise NotImplementedError

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = self._get_scan(ctx.lines, ctx.meta)
        min_indent = self._min_indent(scan.masked)
        groups: list[tuple[int, int]] = []
        cur_start: int | None = None
        prev: int | None = None
        for idx in range(1, len(scan.masked) + 1):
            ln = scan.masked[idx - 1]
            stripped = ln.strip()
            deep = self._is_deep(ln, stripped, min_indent)
            if deep:
                if cur_start is None:
                    cur_start = idx
                prev = idx
            elif stripped:
                # 空行不打断分组，非空且未达深度的行打断
                if cur_start is not None and prev is not None:
                    groups.append((cur_start, prev))
                cur_start = None
                prev = None
        if cur_start is not None and prev is not None:
            groups.append((cur_start, prev))
        return [
            self.make_hit(
                ctx,
                a,
                b,
                f"第 {a}~{b} 行代码嵌套达到 {self.LEVEL_THRESHOLD} 层及以上（缩进 ≥{min_indent} 空格）："
                "深层嵌套显著增加理解与测试成本；建议用卫语句提前返回或将内层逻辑抽取成函数。",
            )
            for a, b in groups
        ]


# ---------------------------------------------------------------- MagicNumber 族


class MagicNumberRuleFamily(Rule):
    """无上下文大数字面量（≥1000）规则族（W15-D 参数化）。

    遍历/阈值/命中结构两语言逐字一致；数字口径与文案以钩子显式化：
    ``_NUM_RE``（JS 额外支持十六进制）、``_skip_line``（JS 额外豁免全大写具名
    常量声明与 re-export 行）、``_parse_number``（JS 十六进制解析）、
    ``_EXAMPLE_HINT``（修复建议示例文案，两语言本就不同）。
    """

    category = Category.STYLE
    severity = Severity.LOW
    description = "代码中出现无命名的大数字面量（≥1000）：含义不明、散落多处难以统一修改，应提取为具名常量。"

    MIN_VALUE = 1000.0
    _NUM_RE: Pattern[str]
    _EXAMPLE_HINT: str = ""

    _get_scan = staticmethod(_lang_get_scan)

    def _skip_line(self, ln: str) -> bool:
        """语言子类覆盖：该行是否整行跳过（import/具名常量等豁免口径）。"""
        raise NotImplementedError

    def _parse_number(self, text: str) -> float:
        """语言子类覆盖：把数字字面量文本解析为 float（非法时抛 ValueError）。"""
        raise NotImplementedError

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = self._get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            # 空行短路对两语言等价（空行无数字可匹配；JS 原实现即含此短路）
            if not ln.strip():
                continue
            if self._skip_line(ln):
                continue
            for m in self._NUM_RE.finditer(ln):
                text = m.group(1)
                try:
                    value = self._parse_number(text)
                except ValueError:  # pragma: no cover
                    continue
                if abs(value) >= self.MIN_VALUE:
                    hits.append(
                        self.make_hit(
                            ctx,
                            idx + 1,
                            idx + 1,
                            f"第 {idx + 1} 行使用魔法数字 `{text}`：字面量含义不明确且多处出现时难以统一维护；"
                            f"请提取为具名常量（如 {self._EXAMPLE_HINT}）并注释业务含义。",
                            meta={"value": value},
                        )
                    )
        return hits


# ---------------------------------------------------------------- HardcodedSecret 族


class HardcodedSecretRuleFamily(Rule):
    """硬编码密钥/口令规则族基类（W15-D）。

    W15-D 决策记录：两语言检测强度**有意不同**——PY 版做标识符分词+单数归一+
    Shannon 熵/字符集多样性闸门（R1-9/R4-1/R4-7），JS 版做变量名子串提示+已知
    密钥前缀匹配。统一为任一口径都会改变另一侧的命中结果，违反「规则行为零
    变化」门禁，故 check() 不上收；本基类只收敛两语言完全一致的元信息与
    mask_snippet 契约位（W14-A1），作为未来口径演进的唯一挂点。
    """

    category = Category.SECURITY
    severity = Severity.CRITICAL
    description = "密钥/口令硬编码在源码中：随代码库扩散，任何有读权限的人都能获取凭据。"
    # W14-A1（C-1，NFR-11）：snippet 进报告前对字面量值打码，不泄露密钥明文
    mask_snippet = True
