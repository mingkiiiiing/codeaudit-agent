"""JS/TS 源码轻量扫描工具：字符串/模板/注释/正则掩码、大括号深度、函数作用域、缩进层级。

被 audit.detect.rules.js.javascript 与 audit.detect.rules.js.typescript 中的
静态规则共享，全部为纯函数。与 ``_python_common`` 同一思路：采用逐字符状态机
而非 tree-sitter，行级启发式即可满足规则对行号精度的要求，且在语法不完全合法
的源码上依然稳健。

掩码约定（与 Python 版一致）：
- 字符串/模板/正则字面量的"内容"与注释被替换为空格，引号与定界符保留位置；
- 模板字符串的 ``${...}`` 插值属于真实代码，插值内部不掩码（保留 ``${``/``}``）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TAB_WIDTH = 4

__all__ = [
    "FuncRange",
    "JsScan",
    "call_span",
    "enclosing_function",
    "find_call",
    "function_ranges",
    "get_scan",
    "indent_unit",
    "indent_width",
    "is_blank",
    "is_js_test_file",
    "scan_js",
]

# ---------------------------------------------------------------- 扫描产物


@dataclass
class JsScan:
    """一次源码扫描的全部产物（行号均为 1-based，列号 0-based）。

    - masked：字符串/模板/正则字面量内容与注释被替换为空格后的行（引号与
      ``${``/``}`` 定界符保留），规则在其上匹配可天然避免命中字符串与注释；
    - comments：(行号, 注释文本)，供 TODO/FIXME 与 @ts-ignore 类规则使用；
    - strings：(行号, 内容起始列, 内容结束列(不含), 引号字符)，描述每行上
      字符串/模板字面量"内容"的跨度（不含引号）；模板跨行时逐行记录；
    - depth_before / depth_after：行前/行后的净大括号深度（基于掩码行统计，
      字符串与正则中的大括号不参与），供作用域与常量再赋值推断使用。
    """

    masked: list[str]
    comments: list[tuple[int, str]] = field(default_factory=list)
    strings: list[tuple[int, int, int, str]] = field(default_factory=list)
    depth_before: list[int] = field(default_factory=list)
    depth_after: list[int] = field(default_factory=list)


# 上一个有效 token 为这些关键字时，`/` 开启正则字面量而非除号
_REGEX_KEYWORDS = frozenset(
    [
        "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
        "throw", "case", "do", "else", "yield", "await",
    ]
)
# 上一个有效 token 的尾部字符属于这些符号时，`/` 开启正则字面量
_REGEX_PREV_CHARS = frozenset("(,=:[!&|?{};+-*%~^<>")


def scan_js(lines: list[str]) -> JsScan:
    """逐字符状态机扫描：产出掩码行、注释清单、字符串内容跨度与大括号深度。"""
    chars = [list(ln) for ln in lines]
    scan = JsScan(masked=[])
    n = len(lines)

    def blank(li: int, a: int, b: int) -> None:
        for k in range(a, min(b, len(chars[li]))):
            chars[li][k] = " "

    def add_span(li: int, a: int, b: int, quote: str) -> None:
        if b > a:
            scan.strings.append((li + 1, a, b, quote))

    mode = "code"  # code | sq | dq | tpl | line_comment | block_comment
    # 栈：元素 "tpl"（模板文本）或 "interp"（模板插值中的代码）
    stack: list[str] = []
    interp_depth: list[int] = []  # 与 stack 中 "interp" 对应的未配对 '{' 深度
    last_sig = ""  # 上一个有效 token（用于正则字面量判定）
    token_buf: list[str] = []

    def flush_token() -> None:
        nonlocal last_sig, token_buf
        if token_buf:
            last_sig = "".join(token_buf)
            token_buf = []

    i, j = 0, 0
    while i < n:
        cur = lines[i]
        if j >= len(cur):
            if mode == "line_comment":
                mode = "code"
            flush_token()  # 标识符不可跨行：行尾必须结算 token
            i += 1
            j = 0
            continue
        c = cur[j]

        if mode == "line_comment":
            scan.comments.append((i + 1, cur[j:].strip()))
            blank(i, j, len(cur))
            mode = "code"
            i += 1
            j = 0
            continue

        if mode == "block_comment":
            end = cur.find("*/", j)
            if end < 0:
                text = cur[j:].strip()
                if text:
                    scan.comments.append((i + 1, text))
                blank(i, j, len(cur))
                i += 1
                j = 0
                continue
            text = cur[j:end].strip()
            if text:
                scan.comments.append((i + 1, text))
            blank(i, j, end + 2)
            j = end + 2
            mode = "code"
            continue

        if mode in ("sq", "dq"):
            q = "'" if mode == "sq" else '"'
            seg_start = j
            closed = False
            while j < len(cur):
                ch = cur[j]
                if ch == "\\":
                    nj = min(j + 1, len(cur) - 1)
                    blank(i, j, nj + 1)
                    j = nj + 1
                    continue
                if ch == q:
                    closed = True
                    break
                j += 1
            end_col = j if closed else len(cur)
            add_span(i, seg_start, end_col, q)
            blank(i, seg_start, end_col)
            # 单双引号字符串不允许跨行：未闭合按行尾截断回 code，避免误报扩散
            mode = "code"
            last_sig = q
            j = end_col + (1 if closed else 0)
            continue

        if mode == "tpl":
            seg_start = j
            closed = False
            while j < len(cur):
                ch = cur[j]
                if ch == "\\":
                    nj = min(j + 1, len(cur) - 1)
                    blank(i, j, nj + 1)
                    j = nj + 1
                    continue
                if ch == "`":
                    closed = True
                    break
                if ch == "$" and j + 1 < len(cur) and cur[j + 1] == "{":
                    break
                j += 1
            add_span(i, seg_start, j, "`")
            blank(i, seg_start, j)
            if closed:
                if stack and stack[-1] == "tpl":
                    stack.pop()
                mode = "code"
                last_sig = "`"
                j += 1
                continue
            if j < len(cur) and cur[j] == "$":
                # `${`：进入模板插值（真实代码），保留定界符
                stack.append("interp")
                interp_depth.append(0)
                mode = "code"
                j += 2
                continue
            # 行尾：模板跨行继续
            i += 1
            j = 0
            continue

        # ---------------- code 模式 ----------------
        if c.isalnum() or c in "_$":
            token_buf.append(c)
            j += 1
            continue
        if c.isspace():
            # 空白是 token 边界：结算 token，但不能覆盖 last_sig
            flush_token()
            j += 1
            continue
        flush_token()
        if c == "/" and j + 1 < len(cur) and cur[j + 1] == "/":
            mode = "line_comment"
            j += 2
            continue
        if c == "/" and j + 1 < len(cur) and cur[j + 1] == "*":
            mode = "block_comment"
            j += 2
            continue
        if c == "'":
            mode = "sq"
            j += 1
            continue
        if c == '"':
            mode = "dq"
            j += 1
            continue
        if c == "`":
            stack.append("tpl")
            mode = "tpl"
            j += 1
            continue
        if c == "/":
            is_regex = (
                not last_sig
                or last_sig in _REGEX_KEYWORDS
                or last_sig[-1] in _REGEX_PREV_CHARS
            )
            if is_regex:
                # 正则字面量：到未转义且不在字符类中的 `/` 截止（仅本行）
                k = j + 1
                in_class = False
                closed = False
                while k < len(cur):
                    ch = cur[k]
                    if ch == "\\":
                        k += 2
                        continue
                    if ch == "[":
                        in_class = True
                    elif ch == "]":
                        in_class = False
                    elif ch == "/" and not in_class:
                        closed = True
                        break
                    k += 1
                if closed:
                    blank(i, j + 1, k)
                    last_sig = "/"
                    j = k + 1
                    continue
                # 未闭合：按除号处理，避免误吞整行
            last_sig = c
            j += 1
            continue
        if c == "{":
            if stack and stack[-1] == "interp":
                interp_depth[-1] += 1
            last_sig = c
            j += 1
            continue
        if c == "}":
            if stack and stack[-1] == "interp":
                if interp_depth[-1] == 0:
                    stack.pop()
                    interp_depth.pop()
                    mode = "tpl"
                    j += 1
                    continue
                interp_depth[-1] -= 1
            last_sig = c
            j += 1
            continue
        last_sig = c
        j += 1

    flush_token()
    scan.masked = ["".join(row) for row in chars]
    # 大括号深度（掩码行上统计，字符串/正则/注释中的大括号已被置空）
    depth = 0
    for ln in scan.masked:
        scan.depth_before.append(depth)
        depth += ln.count("{") - ln.count("}")
        scan.depth_after.append(depth)
    return scan


def get_scan(ctx_lines: list[str], meta: dict) -> JsScan:
    """优先复用引擎预计算的扫描结果（meta["jsscan"]），否则现算。"""
    scan = meta.get("jsscan")
    if isinstance(scan, JsScan):
        return scan
    return scan_js(ctx_lines)


# ---------------------------------------------------------------- 基础行工具


def indent_width(line: str) -> int:
    """行首缩进宽度（tab 按 4 空格计）。"""
    expanded = line.expandtabs(_TAB_WIDTH)
    return len(expanded) - len(expanded.lstrip(" "))


def is_blank(line: str) -> bool:
    return not line.strip()


def indent_unit(masked: list[str]) -> int:
    """推断文件的基础缩进单位：全部非空行中最小的正缩进宽度（无则 4）。

    JS/TS 社区常见 2 空格或 4 空格，层级判定按单位换算而非写死。
    """
    unit = 0
    for ln in masked:
        if not ln.strip():
            continue
        w = indent_width(ln)
        if w > 0 and (unit == 0 or w < unit):
            unit = w
    return unit or 4


def is_js_test_file(rel_path: str) -> bool:
    """判断是否 JS/TS 测试相关文件（console.log 类规则对其跳过）。"""
    parts = rel_path.replace("\\", "/").split("/")
    if any(p in ("__tests__", "tests", "test") for p in parts[:-1]):
        return True
    name = parts[-1]
    return (
        name.startswith("test_")
        or name.endswith((".test.js", ".test.ts", ".test.jsx", ".test.tsx"))
        or name.endswith((".spec.js", ".spec.ts", ".spec.jsx", ".spec.tsx"))
    )


# ---------------------------------------------------------------- 作用域


@dataclass
class FuncRange:
    """函数/方法作用域（1-based 闭区间 [start, end]）。"""

    name: str
    start: int
    end: int
    indent: int
    kind: str = "function"  # function | arrow | method


_FUNC_DECL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*[*]?\s*([\w$]+)\s*[(<]"
)
_ARROW_ASSIGN_RE = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([\w$]+)\s*(?::[^=]+?)?=\s*"
    r"(?:async\s+)?(?:function\b|(\([^()]*\)|[\w$]+)\s*=>)"
)
_METHOD_RE = re.compile(
    r"^\s+(?:(?:public|private|protected|static|readonly|async|get|set|override)\s+|\*)*"
    r"([\w$]+)(?:<[^>()]*>)?\s*\([^;{}]*\)\s*(?::[^{;]+)?\{\s*$"
)
_METHOD_EXCLUDE = frozenset(["if", "for", "while", "switch", "catch", "with", "function"])


def _header_match(masked: list[str], idx: int, regex: re.Pattern[str], max_join: int = 3) -> re.Match[str] | None:
    """在第 idx 行尝试匹配函数头；行尾未完时向下拼接至多 max_join 行再匹配。"""
    for end in range(idx, min(idx + max_join, len(masked))):
        joined = " ".join(s.strip() for s in masked[idx : end + 1])
        m = regex.match(joined)
        if m:
            return m
        if "{" in joined or "=>" in joined or ";" in joined:
            break
    return None


def _block_end(masked: list[str], start_idx0: int) -> int | None:
    """从第 start_idx0 行（0-based）起找 `{` 并配对，返回块结束行（1-based）。

    向下最多找 5 行仍无 `{` 视为无块（表达式箭头函数等），返回 None。
    """
    bal = 0
    found_open = False
    for k in range(start_idx0, len(masked)):
        if not found_open and k > start_idx0 + 5:
            return None
        for ch in masked[k]:
            if ch == "{":
                bal += 1
                found_open = True
            elif ch == "}":
                bal -= 1
                if found_open and bal <= 0:
                    return k + 1
    return len(masked) if found_open else None


def function_ranges(masked: list[str]) -> list[FuncRange]:
    """推断全部函数作用域（嵌套函数的行同时属于外层范围）。

    覆盖三类声明：``function name(...)``、``const name = (...) => {``（含单参
    无括号形式）、类/对象字面量方法 ``name(...) {``。块结束行由掩码行的大括号
    配对确定（字符串/正则中的大括号已被掩码，不干扰配对）。
    """
    out: list[FuncRange] = []
    for idx, ln in enumerate(masked):
        kind = ""
        name = ""
        m = _FUNC_DECL_RE.match(ln) or _header_match(masked, idx, _FUNC_DECL_RE)
        if m:
            kind, name = "function", m.group(1)
        else:
            m = _ARROW_ASSIGN_RE.match(ln) or _header_match(masked, idx, _ARROW_ASSIGN_RE)
            if m:
                kind, name = "arrow", m.group(1)
            else:
                m2 = _METHOD_RE.match(ln)
                if m2 and m2.group(1) not in _METHOD_EXCLUDE:
                    kind, name = "method", m2.group(1)
        if not kind:
            continue
        end = _block_end(masked, idx)
        if end is None or end <= idx:
            continue
        out.append(FuncRange(name=name, start=idx + 1, end=end, indent=indent_width(ln), kind=kind))
    return out


def enclosing_function(ranges: list[FuncRange], lineno: int) -> FuncRange | None:
    """返回包含该行号的最内层函数作用域。"""
    best: FuncRange | None = None
    for r in ranges:
        if r.start <= lineno <= r.end:
            if best is None or r.indent > best.indent:
                best = r
    return best


# ---------------------------------------------------------------- 调用定位


def find_call(masked_line: str, call_pattern: re.Pattern[str]) -> list[int]:
    """在掩码行上找调用起点。

    call_pattern 必须以 ``\\s*\\(`` 结尾（匹配到左括号为止），
    返回每个匹配的左括号所在列。
    """
    out: list[int] = []
    for m in call_pattern.finditer(masked_line):
        col = m.end() - 1
        if col >= 0 and masked_line[col] == "(":
            out.append(col)
    return out


def call_span(masked: list[str], line: int, open_col: int) -> tuple[int, int, str] | None:
    """从 (1-based line, open_col 处的 '(') 起跨行匹配括号。

    返回 (结束行号, 结束列, 参数文本)；掩码行保证字符串内的括号不干扰配对。
    括号永不闭合（语法残缺）时返回 None。
    """
    depth = 0
    buf: list[str] = []
    i, j = line - 1, open_col
    n = len(masked)
    while i < n:
        cur = masked[i]
        while j < len(cur):
            c = cur[j]
            if c == "(":
                depth += 1
                if depth > 1:
                    buf.append(c)
            elif c == ")":
                depth -= 1
                if depth <= 0:
                    return i + 1, j, "".join(buf)
                buf.append(c)
            elif depth >= 1:
                buf.append(c)
            j += 1
        buf.append(" ")
        i += 1
        j = 0
    return None
