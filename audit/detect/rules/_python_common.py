"""Python 源码轻量扫描工具：字符串/注释掩码、函数与循环作用域、调用参数定位。

被 audit.detect.rules.python 中的各静态规则共享，全部为纯函数。
采用逐字符状态机而非 tree-sitter：行级启发式即可满足规则对行号精度的要求，
且在语法不完全合法的源码上依然稳健。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TAB_WIDTH = 4

__all__ = [
    "FuncRange",
    "LoopRange",
    "PyScan",
    "call_span",
    "enclosing_function",
    "find_call",
    "function_ranges",
    "get_scan",
    "handler_body",
    "indent_width",
    "is_blank",
    "is_test_file",
    "line_in_loops",
    "loop_ranges",
    "scan_python",
]


# ---------------------------------------------------------------- 扫描产物


@dataclass
class PyScan:
    """一次源码扫描的全部产物（行号均为 1-based，列号 0-based）。

    - masked：字符串字面量内容与注释被替换为空格后的行（保留引号/# 位置），
      规则在其上匹配可天然避免命中字符串与注释。
    - comments：(行号, 注释文本)，供 TODO/FIXME 类规则使用。
    - comment_start：行号 -> 该行 '#' 的列号（无注释则缺省）。
    - strings：(行号, 内容起始列, 内容结束列(不含), 前缀小写)，
      描述每行上字符串字面量"内容"的跨度（不含引号）；三引号跨行时逐行记录。
    """

    masked: list[str]
    comments: list[tuple[int, str]] = field(default_factory=list)
    comment_start: dict[int, int] = field(default_factory=dict)
    strings: list[tuple[int, int, int, str]] = field(default_factory=list)


def _string_prefix(line: str, quote_col: int) -> str:
    """取字符串开引号前的字母前缀（r/b/f/u 等），小写返回。"""
    i = quote_col - 1
    out: list[str] = []
    while i >= 0 and line[i].isalpha() and line[i].lower() in "rbfus":
        out.append(line[i].lower())
        i -= 1
    return "".join(reversed(out))


def scan_python(lines: list[str]) -> PyScan:
    """逐字符扫描：产出掩码行、注释清单与字符串内容跨度。"""
    chars = [list(ln) for ln in lines]
    scan = PyScan(masked=[])
    n = len(lines)

    def blank(li: int, a: int, b: int) -> None:
        for k in range(a, min(b, len(chars[li]))):
            chars[li][k] = " "

    def add_span(li: int, a: int, b: int, prefix: str) -> None:
        if b > a:
            scan.strings.append((li + 1, a, b, prefix))

    i, j = 0, 0
    while i < n:
        cur = lines[i]
        if j >= len(cur):
            i += 1
            j = 0
            continue
        c = cur[j]
        if c == "#":  # 注释：整段掩码并记录
            scan.comment_start[i + 1] = j
            scan.comments.append((i + 1, cur[j + 1 :].strip()))
            blank(i, j, len(cur))
            i += 1
            j = 0
            continue
        if c == "'" or c == '"':
            q = c
            prefix = _string_prefix(cur, j)
            if cur.startswith(q * 3, j):
                # 三引号：可跨行，逐行记录内容跨度
                seg_start = j + 3
                j = seg_start
                while i < n:
                    cur2 = lines[i]
                    closed_here = False
                    while j < len(cur2):
                        if cur2[j] == "\\":
                            nj = min(j + 1, len(cur2) - 1)
                            blank(i, j, nj + 1)
                            j = nj + 1
                            continue
                        if cur2.startswith(q * 3, j):
                            add_span(i, seg_start, j, prefix)
                            blank(i, seg_start, j)
                            j += 3
                            closed_here = True
                            break
                        j += 1
                    if closed_here:
                        break
                    add_span(i, seg_start, len(cur2), prefix)
                    blank(i, seg_start, len(cur2))
                    i += 1
                    j = 0
                    seg_start = 0
                continue
            # 单引号：不允许跨行（跨行视为未闭合，按行尾截断，避免误报扩散）
            seg_start = j + 1
            j = seg_start
            closed = False
            while j < len(cur):
                ch2 = cur[j]
                if ch2 == "\\":
                    nj = min(j + 1, len(cur) - 1)
                    blank(i, j, nj + 1)
                    j = nj + 1
                    continue
                if ch2 == q:
                    add_span(i, seg_start, j, prefix)
                    blank(i, seg_start, j)
                    j += 1
                    closed = True
                    break
                j += 1
            if not closed:
                add_span(i, seg_start, len(cur), prefix)
                blank(i, seg_start, len(cur))
            continue
        j += 1
    scan.masked = ["".join(row) for row in chars]
    return scan


def get_scan(ctx_lines: list[str], meta: dict) -> PyScan:
    """优先复用引擎预计算的扫描结果（meta["pyscan"]），否则现算。"""
    scan = meta.get("pyscan")
    if isinstance(scan, PyScan):
        return scan
    return scan_python(ctx_lines)


# ---------------------------------------------------------------- 基础行工具


def indent_width(line: str) -> int:
    """行首缩进宽度（tab 按 4 空格计）。"""
    expanded = line.expandtabs(_TAB_WIDTH)
    return len(expanded) - len(expanded.lstrip(" "))


def is_blank(line: str) -> bool:
    return not line.strip()


def is_test_file(rel_path: str) -> bool:
    """判断是否测试相关文件（assert/print 类规则对其跳过）。"""
    parts = rel_path.replace("\\", "/").split("/")
    if any(p in ("tests", "test") for p in parts[:-1]):
        return True
    name = parts[-1]
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


# ---------------------------------------------------------------- 作用域


@dataclass
class FuncRange:
    """函数/方法作用域（1-based 闭区间 [start, end]）。"""

    name: str
    start: int
    end: int
    indent: int


@dataclass
class LoopRange:
    """for/while 循环作用域（header 行为 start，body 末行为 end）。"""

    start: int
    end: int
    indent: int


_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")
_LOOP_RE = re.compile(r"^\s*(?:async\s+)?(?:for|while)\b.*:\s*$")


def function_ranges(masked: list[str]) -> list[FuncRange]:
    """按缩进推断全部函数作用域（嵌套函数的行同时属于外层范围）。"""
    out: list[FuncRange] = []
    for idx, ln in enumerate(masked):
        m = _DEF_RE.match(ln)
        if not m:
            continue
        indent = indent_width(ln)
        last = idx
        k = idx + 1
        while k < len(masked):
            ln2 = masked[k]
            if ln2.strip():
                if indent_width(ln2) <= indent:
                    break
                last = k
            k += 1
        out.append(FuncRange(name=m.group(1), start=idx + 1, end=last + 1, indent=indent))
    return out


def enclosing_function(ranges: list[FuncRange], lineno: int) -> FuncRange | None:
    """返回包含该行号的最内层函数作用域。"""
    best: FuncRange | None = None
    for r in ranges:
        if r.start <= lineno <= r.end:
            if best is None or r.indent > best.indent:
                best = r
    return best


def loop_ranges(masked: list[str]) -> list[LoopRange]:
    """推断全部 for/while 循环作用域。"""
    out: list[LoopRange] = []
    for idx, ln in enumerate(masked):
        if not _LOOP_RE.match(ln):
            continue
        indent = indent_width(ln)
        last = idx
        k = idx + 1
        while k < len(masked):
            ln2 = masked[k]
            if ln2.strip():
                if indent_width(ln2) <= indent:
                    break
                last = k
            k += 1
        if last > idx:
            out.append(LoopRange(start=idx + 1, end=last + 1, indent=indent))
    return out


def line_in_loops(loops: list[LoopRange], lineno: int) -> list[LoopRange]:
    """该行是否落在某循环体内（header 行本身不算 body）。"""
    return [lp for lp in loops if lp.start < lineno <= lp.end]


def handler_body(masked: list[str], lineno: int) -> tuple[int, int] | None:
    """except 处理器的 body 行范围（1-based 闭区间）；空 body 返回 None。"""
    base = indent_width(masked[lineno - 1])
    first: int | None = None
    last = lineno
    k = lineno + 1
    while k <= len(masked):
        ln = masked[k - 1]
        if not ln.strip():
            k += 1
            continue
        if indent_width(ln) <= base:
            break
        if first is None:
            first = k
        last = k
        k += 1
    if first is None:
        return None
    return first, last


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
