"""TypeScript 静态规则库（6 条，聚焦 TS 特有的类型系统逃逸问题）。

安全类问题（eval/XSS/SQL 拼接/硬编码密钥等）与 JS 语法完全一致，
由 `javascript.build_javascript_rules` 中的共享规则（languages 含 typescript）覆盖，
本模块不重复实现。所有规则 `check()` 纯函数式，不修改 ctx、无 IO。
"""

from __future__ import annotations

import re
from typing import Pattern

from audit.detect.base import Rule, RuleContext, RuleHit
from audit.detect.rules.js._js_common import get_scan
from audit.models import Category, Severity

__all__ = ["build_typescript_rules"]


class _TsRule(Rule):
    """仅作用于 TypeScript 的规则基类。"""

    languages = ("typescript",)


class AnyTypeRule(_TsRule):
    """使用 any 类型（`: any` / `as any`）。"""

    id = "TS-ANY"
    category = Category.STYLE
    severity = Severity.LOW
    description = "使用 any 类型：关闭该值的一切类型检查，类型错误会静默传递到运行时，逐渐腐蚀整个类型边界。"

    _RE: Pattern[str] = re.compile(r"(?::\s*any\b|(?<![\w.$])\bas\s+any\b)")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            if self._RE.search(ln):
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行使用 `any` 类型：any 会关闭该值的全部类型检查，"
                        "拼写错误与不存在的属性要等到运行时才暴露，并沿调用链向外扩散；"
                        "请改用具体类型、泛型或 unknown + 类型收窄。",
                    )
                )
        return hits


class TsIgnoreRule(_TsRule):
    """@ts-ignore 抑制类型错误。"""

    id = "TS-IGNORE"
    category = Category.STYLE
    severity = Severity.MEDIUM
    description = "使用 @ts-ignore 压制类型错误：被忽略的错误仍会在运行时发生，且后续重构不会被编译器保护。"

    _RE: Pattern[str] = re.compile(r"@ts-ignore\b")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for lineno, text in scan.comments:
            if self._RE.search(text):
                hits.append(
                    self.make_hit(
                        ctx,
                        lineno,
                        lineno,
                        f"第 {lineno} 行使用 `@ts-ignore` 压制类型错误：被掩盖的类型错误仍会在运行时爆发，"
                        "且后续代码重构失去编译器保护；请修复类型问题，"
                        "确实无法立即修复时改用 @ts-expect-error（类型消失时会报错提醒清理）。",
                        meta={"comment": text.strip()[:80]},
                    )
                )
        return hits


class NonNullAssertionAbuseRule(_TsRule):
    """同一文件非空断言 `!.` 使用 ≥5 次。"""

    id = "TS-NONNULL-ABUSE"
    category = Category.BUG
    severity = Severity.MEDIUM
    description = "同一文件大量使用 `!.` 非空断言：断言处的 null 检查被编译器跳过，值为空时直接运行时崩溃。"

    _RE: Pattern[str] = re.compile(r"(?<=[\w)\]])!\.")
    THRESHOLD = 5

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        total = 0
        first_line: int | None = None
        for idx, ln in enumerate(scan.masked):
            count = len(self._RE.findall(ln))
            if count and first_line is None:
                first_line = idx + 1
            total += count
        if first_line is None or total < self.THRESHOLD:
            return []
        return [
            self.make_hit(
                ctx,
                first_line,
                first_line,
                f"本文件共出现 {total} 处 `!.` 非空断言（首次在第 {first_line} 行）："
                "每处断言都跳过了编译器的 null 检查，任何一处的值为 null/undefined 都会直接抛出 "
                "`Cannot read properties of ...` 运行时错误；请改用可选链 `?.`、提前判空或收窄类型。",
                meta={"count": total},
            )
        ]


class DoubleAssertionRule(_TsRule):
    """`as unknown as` 双重断言绕过类型系统。"""

    id = "TS-NEVER-ASSERT"
    category = Category.BUG
    severity = Severity.HIGH
    description = "使用 `as unknown as X` 双重断言：任何类型都能被强转成任何类型，类型系统在此处完全失效。"

    _RE: Pattern[str] = re.compile(r"\bas\s+unknown\s+as\b|\bas\s+any\s+as\b")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            if self._RE.search(ln):
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行使用 `as unknown as` 双重断言：先转 unknown 再转目标类型，"
                        "等于绕过了 TypeScript 全部类型检查，字段缺失/类型不符都要到运行时才暴露；"
                        "请补充真实的类型定义、校验函数（zod/类型守卫）或修正数据来源的类型。",
                    )
                )
        return hits


class ExportedAnyParamRule(_TsRule):
    """导出函数的参数使用 any 类型。"""

    id = "TS-EXPLICIT-ANY-PARAM"
    category = Category.STYLE
    severity = Severity.LOW
    description = "导出（公共 API）函数的参数使用 any：外部调用方失去全部类型约束，API 契约名存实亡。"

    _EXPORT_FUNC_RE: Pattern[str] = re.compile(
        r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function\s+[\w$]+|(?:const|let|var)\s+[\w$]+)\s*[((=]"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            if not self._EXPORT_FUNC_RE.match(ln):
                continue
            signature = self._signature(scan.masked, idx)
            if signature and re.search(r"\(\s*[^)]*:\s*any\b", signature):
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行导出函数的参数使用了 `any` 类型：导出函数是对外契约，"
                        "any 参数让调用方失去类型检查，参数传错要到运行时才暴露；"
                        "请为参数定义具体类型或使用泛型。",
                    )
                )
        return hits

    @staticmethod
    def _signature(masked: list[str], start: int, max_lines: int = 6) -> str:
        """拼接导出函数头部的签名文本（到 `{` 或 `=>` 截止）。"""
        parts: list[str] = []
        for k in range(start, min(start + max_lines, len(masked))):
            text = masked[k].strip()
            parts.append(text)
            if "{" in text or "=>" in text or ";" in text:
                break
        return " ".join(parts)


class ExportedFuncReturnTypeRule(_TsRule):
    """导出函数缺少显式返回类型标注。"""

    id = "TS-FUNC-STYLE"
    category = Category.STYLE
    severity = Severity.LOW
    description = "导出（公共 API）函数缺少显式返回类型：返回值结构变化会静默传遍所有调用方，IDE 跳转与契约审查均不可用。"

    _EXPORT_FUNC_RE: Pattern[str] = re.compile(
        r"^\s*export\s+(?:default\s+)?(?:async\s+)?function\s+[\w$]+\s*[(<]"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            if not self._EXPORT_FUNC_RE.match(ln):
                continue
            signature = self._signature(scan.masked, idx)
            if not signature:
                continue
            # 找到参数列表收口的 ')'，检查其后到块开始前是否有 ": Type"
            depth = 0
            closed_at = -1
            for pos, ch in enumerate(signature):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        closed_at = pos
                        break
            if closed_at < 0:
                continue
            tail = signature[closed_at + 1 :]
            if re.match(r"\s*:\s*[\w<>\[\]{}|,\s.\"']+", tail):
                continue
            name_m = re.search(r"function\s+([\w$]+)", signature)
            name = name_m.group(1) if name_m else "<anonymous>"
            hits.append(
                self.make_hit(
                    ctx,
                    idx + 1,
                    idx + 1,
                    f"第 {idx + 1} 行导出函数 `{name}` 缺少显式返回类型标注："
                    "返回值结构一旦变化会静默传播到所有调用方，公共 API 的契约无法被编译器守护；"
                    "请为导出函数补充显式返回类型。",
                )
            )
        return hits

    @staticmethod
    def _signature(masked: list[str], start: int, max_lines: int = 6) -> str:
        """拼接导出函数头部的签名文本（到 `{` 或 `;` 截止）。"""
        parts: list[str] = []
        for k in range(start, min(start + max_lines, len(masked))):
            text = masked[k].strip()
            parts.append(text)
            if "{" in text or ";" in text:
                break
        return " ".join(parts)


# ==================================================================== 注册


def build_typescript_rules() -> list[Rule]:
    """构建全部内置 TypeScript 规则实例（顺序即默认报告顺序）。"""
    return [
        AnyTypeRule(),
        TsIgnoreRule(),
        NonNullAssertionAbuseRule(),
        DoubleAssertionRule(),
        ExportedAnyParamRule(),
        ExportedFuncReturnTypeRule(),
    ]
