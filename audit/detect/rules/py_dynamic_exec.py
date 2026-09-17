"""Python 静态规则库·动态执行/导入四形态（W19-D 清偿深度审计漏报，3 条 security）。

与 python.py/python_ext.py 同一实现思路：基于 `_python_common` 的逐行掩码扫描
（不依赖 tree-sitter），保证行号精确落在真实代码行上；`check()` 纯函数式，
不修改 ctx、无 IO。独立成文件，不触碰 python.py（W19 并行窗口所有权隔离）。

清偿的漏报形态（既有 PY-EVAL-EXEC 只认裸 eval(/exec(，五形态漏报四类）：
- `__import__(mod)` / `importlib.import_module(mod)`：用户可控动态导入；
- `compile(src, ...)`：动态编译源码，等价任意代码执行入口；
- `getattr(builtins, "eval")(x)` / `globals()[m]()`：间接动态执行（标疑级 low）。

口径说明（误报红线：clean 语料 0 命中）：
- 前两条规则只认"首参为非静态字面量"的调用；纯字符串字面量的
  `import_module("pkg")` / `compile("1+1", ...)` 等价静态声明，不报。
  静态判定基于掩码行：字面量内容已被置空，首参残留（去引号/空白）为空或仅
  为字符串前缀字母（r/b/f/u）即视为字面量；f-string 前缀回原文查 `{}` 插值，
  含插值仍视为动态。
- `compile` 匹配带 `(?<![\\w.])` 前沿：`re.compile(...)` 等"点号属性"形态
  （正则编译的日常用法）天然排除，绝不误报。
- `getattr` / `globals()[...]` 里的字符串字面量内容在掩码行上已置空，本规则
  从 scan.strings 跨度回原文提取内容后按危险内建名匹配（\\b 词边界口径，
  "openai_key"/"import_helper"/"re_eval" 这类含危险词片段的普通名不误报）；
  内容恰为 eval/exec/__import__/import/open/system/popen 之一才标疑。
- `globals()[...]`/`locals()[...]` 双口径：字符串字面量下标仅当内容恰为
  危险内建名时报；变量下标仅在"取值后立即调用"（动态分发）时报——纯取值
  （`globals()[key]` 查表）不报。
- 单行口径（已知局限）：下标不跨行；`from importlib import import_module`
  之后的裸 `import_module(mod)` 不在本规则范围（漏报，属已知限制）。
- 同一行可能同时命中本文件与既有 PY-EVAL-EXEC（裸 eval 行）——不同规则 id
  的同行命中由 engine 去重逻辑处理，本文件不做跨规则去重。
"""

from __future__ import annotations

import re
from typing import Pattern

from audit.detect.base import Rule, RuleContext, RuleHit
from audit.detect.rules._python_common import call_span, find_call, get_scan
from audit.models import Category, Severity

__all__ = [
    "DynamicCompileRule",
    "DynamicImportRule",
    "IndirectExecRule",
    "build_dynamic_exec_rules",
]


class _PyDynamicExecRule(Rule):
    """动态执行/导入规则公共基类：声明语言。"""

    languages = ("python",)


# ---------------------------------------------------------------- 扫描通用件


# 危险内建/等价执行名（\b 词边界口径：openai_key / import_helper 等片段不命中）
_DANGEROUS_NAME_RE: Pattern[str] = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:__import__|eval|exec|import|open|system|popen)"
    r"(?![A-Za-z0-9_])"
)

# 字符串前缀字母（r/b/f/u 及大写）：掩码残留仅含这些字母时仍视为静态字面量
_STRING_PREFIX_CHARS = frozenset("uUbBrRfF")


def _first_arg_region(
    masked: list[str], line: int, open_col: int
) -> tuple[tuple[int, int], tuple[int, int], str] | None:
    """定位调用首参在掩码行上的跨度（可跨行）。

    返回 ((起始行, 起始列), (结束行, 结束列), 掩码文本)，结束行列为开区间；
    空参数表（左括号后立即收括号/逗号）返回 None。括号深度保证嵌套调用
    （如 `compile(src, f(x), "eval")` 的首个顶层逗号）不截断。
    """
    depth = 0
    start: tuple[int, int] | None = None
    buf: list[str] = []
    i, j = line - 1, open_col + 1
    n = len(masked)
    while i < n:
        cur = masked[i]
        while j < len(cur):
            ch = cur[j]
            if ch in "([{":
                if depth == 0 and start is None:
                    start = (i + 1, j)
                depth += 1
                buf.append(ch)
            elif ch in ")]}":
                if depth == 0:
                    if start is None:
                        return None  # 空参数表
                    return start, (i + 1, j), "".join(buf)
                depth -= 1
                buf.append(ch)
            elif ch == "," and depth == 0:
                if start is None:
                    return None  # 空首参（畸形参数表）
                return start, (i + 1, j), "".join(buf)
            else:
                if depth == 0 and start is None and not ch.isspace():
                    start = (i + 1, j)
                buf.append(ch)
            j += 1
        buf.append(" ")
        i += 1
        j = 0
    return None


def _strings_in_region(scan, region: tuple[tuple[int, int], tuple[int, int], str]) -> list:
    """取落在首参跨度内的字符串字面量内容跨度（scan.strings 项，含前缀）。"""
    (sl, sc), (el, ec), _text = region
    out = []
    for item in scan.strings:
        lineno, a, b = item[0], item[1], item[2]
        if lineno < sl or lineno > el:
            continue
        if lineno == sl and a < sc:
            continue
        if lineno == el and b > ec:
            continue
        out.append(item)
    return out


def _is_static_string_arg(ctx: RuleContext, scan, region) -> bool:
    """判定调用首参是否纯静态字符串字面量（f-string 含插值视为动态）。

    掩码行上字面量内容已置空：首参残留（去引号/空白）为空或仅为字符串前缀
    字母时视为字面量；再对 f-string 回原文查 `{}` 插值，含插值仍算动态。
    """
    _start, _end, text = region
    leftover = {ch for ch in text if not ch.isspace() and ch not in "'\""}
    if leftover and not leftover <= _STRING_PREFIX_CHARS:
        return False
    for lineno, a, b, prefix in _strings_in_region(scan, region):
        if "f" in prefix.lower() and "{" in ctx.lines[lineno - 1][a:b]:
            return False
    return True


def _matching_bracket(ln: str, open_col: int) -> int | None:
    """从 open_col 处的 '[' 起找同行配对的 ']'；未闭合返回 None。"""
    depth = 0
    for k in range(open_col, len(ln)):
        if ln[k] == "[":
            depth += 1
        elif ln[k] == "]":
            depth -= 1
            if depth == 0:
                return k
    return None


# ==================================================================== 规则


_IMPORT_CALL_RES: tuple[tuple[Pattern[str], str], ...] = (
    (re.compile(r"(?<![\w.])__import__\s*\("), "__import__"),
    (re.compile(r"(?<![\w.])importlib\s*\.\s*import_module\s*\("), "importlib.import_module"),
)


class DynamicImportRule(_PyDynamicExecRule):
    """__import__ / importlib.import_module 首参非静态字面量：用户可控动态导入。"""

    id = "PY-DYNAMIC-IMPORT"
    category = Category.SECURITY
    severity = Severity.MEDIUM
    description = (
        "以变量/拼接动态导入模块（__import__/importlib.import_module 首参非静态字面量）："
        "用户可控的动态导入可被加载任意模块（恶意路径/覆盖标准库），建议白名单映射。"
    )

    bad_example = (
        "import importlib\n"
        "\n"
        "def load(mod):\n"
        "    return importlib.import_module(mod)  # mod 可控时可加载任意模块\n"
    )
    good_example = (
        "import importlib\n"
        "\n"
        "PLUGIN_WHITELIST = {\"report\": \"report\"}\n"
        "\n"
        "def load(mod):\n"
        "    name = PLUGIN_WHITELIST.get(mod)\n"
        "    if name is None:\n"
        "        raise ValueError(f\"unknown plugin: {mod}\")\n"
        "    return importlib.import_module(name)  # 白名单映射后的可枚举导入\n"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            for pat, api in _IMPORT_CALL_RES:
                for col in find_call(ln, pat):
                    region = _first_arg_region(scan.masked, idx + 1, col)
                    if region is None:
                        continue
                    if _is_static_string_arg(ctx, scan, region):
                        continue  # 纯字面量：等价静态导入声明，无风险
                    hits.append(
                        self.make_hit(
                            ctx,
                            idx + 1,
                            idx + 1,
                            f"第 {idx + 1} 行通过 `{api}` 以变量/拼接动态导入模块（首参非静态字面量）："
                            "用户可控的动态导入可被加载任意模块（恶意路径/覆盖标准库）；"
                            "请改用静态 import，或对模块名做白名单映射。",
                            meta={"api": api},
                        )
                    )
        return hits


_COMPILE_CALL_RE: Pattern[str] = re.compile(r"(?<![\w.])compile\s*\(")


class DynamicCompileRule(_PyDynamicExecRule):
    """compile() 首参非静态字面量：动态编译源码，等价任意代码执行入口。"""

    id = "PY-DYNAMIC-COMPILE"
    category = Category.SECURITY
    severity = Severity.MEDIUM
    description = (
        "调用内建 compile() 且源码首参为变量/拼接（非静态字面量）："
        "动态编译源码等价任意代码执行入口，配合 eval/exec 即可执行不可信代码。"
    )

    bad_example = (
        "def run(src):\n"
        "    code = compile(src, \"<s>\", \"eval\")  # src 可控时等价任意代码执行\n"
        "    return eval(code)\n"
    )
    good_example = (
        "import ast\n"
        "\n"
        "def read_config(src):\n"
        "    return ast.literal_eval(src)  # 只解析字面量，不执行代码\n"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            for col in find_call(ln, _COMPILE_CALL_RE):
                region = _first_arg_region(scan.masked, idx + 1, col)
                if region is None:
                    continue
                if _is_static_string_arg(ctx, scan, region):
                    continue  # 纯字面量源码：内容静态可审计，不在本规则范围
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行调用内建 `compile()` 且源码首参为变量/拼接（非静态字面量）："
                        "动态编译源码等价任意代码执行入口，输入可控时攻击者可执行任意代码；"
                        "请避免编译不可信输入，或改用 `ast.literal_eval`/白名单校验。",
                    )
                )
        return hits


_GETATTR_RE: Pattern[str] = re.compile(r"(?<![\w.])getattr\s*\(")
# globals()/locals() 是内建调用后下标：globals()["eval"] 的 () 不可省
_NAMESPACE_SUBSCRIPT_RE: Pattern[str] = re.compile(
    r"(?<![\w.])(globals|locals)\s*\(\s*\)\s*\["
)


class IndirectExecRule(_PyDynamicExecRule):
    """getattr 危险内建名字面量 / globals()[..] 动态分发：间接动态执行（标疑）。"""

    id = "PY-INDIRECT-EXEC"
    category = Category.SECURITY
    severity = Severity.LOW
    description = (
        "getattr 参数字符串字面量含危险内建名（eval/exec/__import__ 等），"
        "或 globals()/locals() 下标取值后立即调用：间接动态执行（标疑）——"
        "静态无法确证可控性，请人工确认。"
    )

    bad_example = (
        "import builtins\n"
        "\n"
        "def run(src):\n"
        "    return getattr(builtins, \"eval\")(src)  # 间接拿到 eval，等价任意代码执行\n"
    )
    good_example = (
        "OPERATIONS = {\"inspect\": do_inspect}  # 显式映射表，可枚举可审计\n"
        "\n"
        "def run(kind, src):\n"
        "    return OPERATIONS[kind](src)\n"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            hits.extend(self._check_getattr(ctx, scan, idx + 1, ln))
            hits.extend(self._check_namespace_subscript(ctx, scan, idx + 1, ln))
        return hits

    def _check_getattr(
        self, ctx: RuleContext, scan, lineno: int, ln: str
    ) -> list[RuleHit]:
        """getattr(...) 参数中的字符串字面量内容（回原文提取）含危险内建名。"""
        out: list[RuleHit] = []
        for col in find_call(ln, _GETATTR_RE):
            span = call_span(scan.masked, lineno, col)
            if span is None:
                continue
            end_line, end_col = span[0], span[1]
            names: list[str] = []
            for s_lineno, a, b, _prefix in scan.strings:
                if s_lineno < lineno or s_lineno > end_line:
                    continue
                if s_lineno == lineno and a <= col:
                    continue  # 调用起点之前的字符串（如同行字典下标）不算参数
                if s_lineno == end_line and b > end_col:
                    continue  # 调用收尾括号之后的字符串不算参数
                content = ctx.lines[s_lineno - 1][a:b]
                for dm in _DANGEROUS_NAME_RE.finditer(content):
                    if dm.group(0) not in names:
                        names.append(dm.group(0))
            if names:
                out.append(
                    self.make_hit(
                        ctx,
                        lineno,
                        lineno,
                        f"第 {lineno} 行 `getattr(...)` 的字符串字面量参数含危险内建名"
                        f"（{', '.join(names)}），构成间接动态执行（标疑）："
                        "静态无法确证可控性，请人工确认；"
                        "如需动态分发请使用显式映射表并校验来源。",
                        meta={"names": ",".join(names), "kind": "getattr"},
                    )
                )
        return out

    def _check_namespace_subscript(
        self, ctx: RuleContext, scan, lineno: int, ln: str
    ) -> list[RuleHit]:
        """globals()[..]/locals()[..]：危险名字面量下标，或变量下标紧跟调用。"""
        out: list[RuleHit] = []
        for m in _NAMESPACE_SUBSCRIPT_RE.finditer(ln):
            open_col = m.end() - 1
            close_col = _matching_bracket(ln, open_col)
            if close_col is None:
                continue  # 下标跨行：单行口径已知局限
            ns = m.group(1)
            names: list[str] = []
            for s_lineno, a, b, _prefix in scan.strings:
                if s_lineno != lineno or a < open_col + 1 or b > close_col:
                    continue
                content = ctx.lines[s_lineno - 1][a:b]
                for dm in _DANGEROUS_NAME_RE.finditer(content):
                    if dm.group(0) not in names:
                        names.append(dm.group(0))
            if names:
                out.append(
                    self.make_hit(
                        ctx,
                        lineno,
                        lineno,
                        f"第 {lineno} 行 `{ns}[...]` 下标字符串字面量含危险内建名"
                        f"（{', '.join(names)}），构成间接动态执行（标疑）："
                        "静态无法确证可控性，请人工确认；"
                        "如需按名取函数请使用显式映射表。",
                        meta={"names": ",".join(names), "kind": "namespace-subscript", "namespace": ns},
                    )
                )
                continue
            # 变量下标：仅"取值后立即调用"（动态分发）才标疑；纯取值查表不报
            sub = ln[open_col + 1 : close_col]
            tail = ln[close_col + 1 :].lstrip()
            if re.search(r"\w", sub) and tail.startswith("("):
                out.append(
                    self.make_hit(
                        ctx,
                        lineno,
                        lineno,
                        f"第 {lineno} 行以变量下标经 `{ns}[...]` 取值并立即调用，"
                        "构成间接动态执行（标疑）：静态无法确证可控性，请人工确认；"
                        "如需动态分发请使用显式映射表。",
                        meta={"kind": "namespace-dispatch", "namespace": ns},
                    )
                )
        return out


# ==================================================================== 注册


def build_dynamic_exec_rules() -> list[Rule]:
    """构建动态执行/导入形态全部规则实例（顺序即默认报告顺序）。

    注册接线由集成人统一完成（W19 并行窗口约定），本文件只提供构建函数。
    """
    return [
        DynamicImportRule(),
        DynamicCompileRule(),
        IndirectExecRule(),
    ]
