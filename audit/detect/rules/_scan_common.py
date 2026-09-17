"""跨语言通用扫描件（W15-D）：从 _python_common / _js_common 收敛的逐字重复件。

W15-D 决策记录：
- 本模块只收纳两份语言 common 中**逐字节一致**（经 AST 源码段 diff 逐一确认）的
  语言无关通用件：``indent_width`` / ``is_blank`` / ``find_call`` / ``call_span`` /
  ``enclosing_function`` 与 ``_TAB_WIDTH``。两个旧 common 改为从本模块转发导入，
  旧导入路径（含 engine.py 的 ``scan_python``、bench 与测试的既有导入）全部保持可用。
- ``FuncRange`` 未合并：JS 版多 ``kind`` 字段（function|arrow|method），Python 版无；
  dataclass 的 eq/repr 属可观察行为，为守「规则行为零变化」门禁保留各自定义，
  ``enclosing_function`` 以最小结构协议（start/end/indent）做类型约束，运行期无影响。
- ``string_value`` 是三处近似拷贝（python.py 的 PY-HARDCODED-SECRET、js/javascript.py
  的 JS-HARDCODED-SECRET、js/js_ext.py 的 ``_string_literal_value``）的统一实现：
  引号形态取历史调用形状的并集——「推断（仅 ' " ）」与「显式指定（含反引号）」；
  转义语义存在真实差异——Python 版不做转义跳转（反斜杠是普通字符）、
  JS javascript 版跳过转义且不保留、js_ext 版跳过转义但保留原文两字符。
  强行统一为任一转义语义都会改变含转义引号输入的命中结果，违反零行为变化门禁，
  故以 ``escape_mode`` 参数显式化，各调用点按原语义传参（调用处有注释指认）。

与两份语言 common 同一思路：逐字符/行级启发式即可满足规则对行号精度的要求，
且在语法不完全合法的源码上依然稳健。全部为纯函数。
"""

from __future__ import annotations

import re
from typing import Protocol

_TAB_WIDTH = 4

__all__ = [
    "call_span",
    "enclosing_function",
    "find_call",
    "indent_width",
    "is_blank",
    "string_value",
]


# ---------------------------------------------------------------- 基础行工具


def indent_width(line: str) -> int:
    """行首缩进宽度（tab 按 4 空格计）。"""
    expanded = line.expandtabs(_TAB_WIDTH)
    return len(expanded) - len(expanded.lstrip(" "))


def is_blank(line: str) -> bool:
    return not line.strip()


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


# ---------------------------------------------------------------- 作用域


class _FuncScope(Protocol):
    """enclosing_function 所需的最小作用域结构（Py/Js 两版 FuncRange 的公共字段）。"""

    start: int
    end: int
    indent: int


def enclosing_function(ranges: list[_FuncScope], lineno: int) -> _FuncScope | None:
    """返回包含该行号的最内层函数作用域。"""
    best: _FuncScope | None = None
    for r in ranges:
        if r.start <= lineno <= r.end:
            if best is None or r.indent > best.indent:
                best = r
    return best


# ---------------------------------------------------------------- 字符串字面量取值


def string_value(
    raw: str,
    quote_col: int,
    quote: str | None = None,
    *,
    escape_mode: str = "skip",
) -> str | None:
    """从 raw 的 quote_col 处开引号提取字符串字面量内容；未闭合返回 None。

    W15-D：三处历史拷贝的并集形状，语义差异以参数显式化（见模块 docstring）：
    - quote=None：推断引号，且只接受 ' / "（python.py 原语义；quote_col 越界或
      该列不是这两种引号时返回 None）；
    - quote=显式字符：要求 raw[quote_col] 与之一致（javascript.py 原语义；
      js_ext.py 原实现不校验该列，但其唯一调用点传入的正是 raw[quote_col]，
      校验恒真，行为等价）；
    - escape_mode="none"：反斜杠按普通字符处理，内容止于第一个未转义的同引号
      （python.py 原 ``raw.find`` 实现）；
    - escape_mode="skip"：跳过 ``\\x`` 两字符且不保留（javascript.py 原实现）；
    - escape_mode="keep"：跳过 ``\\x`` 两字符但把原文保留进结果（js_ext.py 原实现）。
    """
    if quote is None:
        if quote_col >= len(raw) or raw[quote_col] not in "'\"":
            return None
        q = raw[quote_col]
    else:
        if quote_col >= len(raw) or raw[quote_col] != quote:
            return None
        q = quote
    if escape_mode == "none":
        end = raw.find(q, quote_col + 1)
        if end < 0:
            return None
        return raw[quote_col + 1 : end]
    out: list[str] = []
    i = quote_col + 1
    while i < len(raw):
        ch = raw[i]
        if ch == "\\":
            if escape_mode == "keep":
                out.append(raw[i : i + 2])
            elif escape_mode != "skip":  # pragma: no cover - 防御非法取值
                raise ValueError(f"未知 escape_mode: {escape_mode!r}")
            i += 2
            continue
        if ch == q:
            return "".join(out)
        out.append(ch)
        i += 1
    return None
