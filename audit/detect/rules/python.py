"""Python 静态规则库（27 条，覆盖 bug/performance/style/security 四类）。

实现方式：基于 `_python_common` 的逐行掩码扫描 + 作用域启发式（不依赖 tree-sitter），
保证行号精确落在真实代码行上。所有规则 `check()` 纯函数式，不修改 ctx、无 IO。

每条规则对应 demo_proj 金标（tests/samples/demo_proj/GOLDEN_ISSUES.md）的覆盖情况
见各规则的 docstring 与 tests/unit/detect/test_golden_demo_proj.py。
"""

from __future__ import annotations

import keyword as _keyword
import math
import re
from typing import Any, Pattern

from audit.detect.ast_util import confirm_hit, ident_text, line_node_map, root_identifier
from audit.detect.base import Rule, RuleContext, RuleHit
from audit.detect.rules._python_common import (
    PyScan,
    call_span,
    enclosing_function,
    find_call,
    function_ranges,
    get_scan,
    handler_body,
    indent_width,
    is_test_file,
    line_in_loops,
    loop_ranges,
)
from audit.detect.rules._rule_families import (
    DeepNestingRuleFamily,
    HardcodedSecretRuleFamily,
    LongFunctionRuleFamily,
    MagicNumberRuleFamily,
    TodoFixmeCommentRuleFamily,
)
from audit.detect.rules._scan_common import string_value
from audit.models import Category, RuleHit, Severity

__all__ = ["build_python_rules"]


class _PyRule(Rule):
    """Python 规则公共基类：声明语言。"""

    languages = ("python",)


# ==================================================================== bug 类


class BareExceptRule(_PyRule):
    """裸 except（对应金标 G5）。"""

    id = "PY-BARE-EXCEPT"
    category = Category.BUG
    severity = Severity.MEDIUM
    description = "使用裸 `except:` 会捕获包括 SystemExit/KeyboardInterrupt 在内的一切异常，且丢失异常上下文。"

    _RE: Pattern[str] = re.compile(r"^\s*except\s*:")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            if self._RE.match(ln):
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行使用裸 `except:`：会吞掉包括 Ctrl-C、系统退出在内的所有异常并丢失堆栈上下文，"
                        "真实故障被掩盖后程序可能在错误状态下继续运行；应改为捕获具体异常类型。",
                    )
                )
        return hits


class ExceptPassRule(_PyRule):
    """except 后静默 pass（对应金标 G5 的 pass 分支；与 BARE-EXCEPT 互补，不含裸 except）。"""

    id = "PY-EXCEPT-PASS"
    category = Category.BUG
    severity = Severity.MEDIUM
    description = "except 分支体只有 pass：异常被静默吞掉，出错时既无日志也无恢复动作。"

    _EXCEPT_RE: Pattern[str] = re.compile(r"^\s*except\b[^:]*:")
    _BARE_RE: Pattern[str] = re.compile(r"^\s*except\s*:")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            if self._BARE_RE.match(ln) or not self._EXCEPT_RE.match(ln):
                continue  # 裸 except 由 PY-BARE-EXCEPT 负责
            body = handler_body(scan.masked, idx + 1)
            if body and all(scan.masked[k - 1].strip() == "pass" for k in range(body[0], body[1] + 1)):
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        body[1],
                        f"第 {idx + 1} 行的 except 分支体只有 `pass`：异常被静默吞掉，故障发生时无任何痕迹可查；"
                        "至少应记录日志或在无法处理时重新抛出。",
                    )
                )
        return hits


class EmptyExceptContextRule(_PyRule):
    """except 体只有占位符（... / docstring）：异常处理逻辑缺失。"""

    id = "PY-EMPTY-EXCEPT"
    category = Category.BUG
    severity = Severity.LOW
    description = "except 分支体只有 `...` 或字符串占位（如 docstring），异常处理逻辑尚未实现或被遗忘。"

    _EXCEPT_RE: Pattern[str] = re.compile(r"^\s*except\b[^:]*:")

    @staticmethod
    def _is_trivial(text: str) -> bool:
        s = text.strip()
        if s in ("...", ""):
            return True
        return set(s) <= {'"', "'", " "}

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            if not self._EXCEPT_RE.match(ln):
                continue
            body = handler_body(scan.masked, idx + 1)
            if body and all(self._is_trivial(scan.masked[k - 1]) for k in range(body[0], body[1] + 1)):
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        body[1],
                        f"第 {idx + 1} 行的 except 分支体为空占位（`...`/docstring）：异常处理逻辑缺失，"
                        "触发异常时行为与未捕获几乎相同；请补全处理逻辑或移除该分支。",
                    )
                )
        return hits


class MutableDefaultArgRule(_PyRule):
    """可变默认参数（对应金标 G6）。"""

    id = "PY-MUTABLE-DEFAULT"
    category = Category.BUG
    severity = Severity.HIGH
    description = "函数默认参数使用 []/{}/set() 等可变对象：默认值在多次调用间共享，跨调用累积脏数据。"

    _DEF_RE: Pattern[str] = re.compile(r"^\s*(?:async\s+)?def\s+\w+\s*\(")
    _MUT_RE: Pattern[str] = re.compile(
        r"(\w+)\s*(?::\s*[^=]+?)?\s*=\s*(\[\s*\]|\{\s*\}|set\s*\(\s*\)|list\s*\(\s*\)|dict\s*\(\s*\))\s*(?:,|$)"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            if not self._DEF_RE.match(ln):
                continue
            open_col = ln.find("(")
            span = call_span(scan.masked, idx + 1, open_col)
            args_text = span[2] if span else ln[open_col + 1 :]
            for m in self._MUT_RE.finditer(args_text):
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行参数 `{m.group(1)}` 使用可变默认值 `{m.group(2).strip()}`："
                        "默认对象在进程内只创建一次并在多次调用间共享，前一次调用的修改会泄漏到后续调用；"
                        "应改为默认 None 并在函数体内创建新对象。",
                    )
                )
        return hits


class EqNoneRule(_PyRule):
    """== None 比较（对应金标 G7）。"""

    id = "PY-EQ-NONE"
    category = Category.BUG
    severity = Severity.LOW
    description = "用 `==`/`!=` 与 None 比较：重载了 __eq__ 的对象会得到错误结果，应使用 `is`/`is not`。"

    _RE: Pattern[str] = re.compile(r"==\s*None\b|!=\s*None\b|None\s*==")

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
                        f"第 {idx + 1} 行使用 `==`/`!=` 与 None 比较：会触发对象自定义的 __eq__，"
                        "对重载相等性的对象可能误判；与单例 None 比较应使用 `is`/`is not`。",
                    )
                )
        return hits


class OpenNoCloseRule(_PyRule):
    """open() 赋值后未 close 且未用 with（对应金标 G4）。"""

    id = "PY-OPEN-NO-CLOSE"
    category = Category.BUG
    severity = Severity.HIGH
    description = "open() 的返回值既未用 with 管理，也未在作用域内显式 close：文件句柄泄漏。"

    _OPEN_ASSIGN_RE: Pattern[str] = re.compile(r"(\w+)\s*=\s*open\s*\(")
    _CLOSE_RE = r"\b{var}\s*\.\s*close\s*\("

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for fr in function_ranges(scan.masked):
            region = "\n".join(scan.masked[fr.start - 1 : fr.end])
            for lineno in range(fr.start, fr.end + 1):
                ln = scan.masked[lineno - 1]
                for m in self._OPEN_ASSIGN_RE.finditer(ln):
                    var = m.group(1)
                    if re.search(self._CLOSE_RE.format(var=re.escape(var)), region):
                        continue
                    hits.append(
                        self.make_hit(
                            ctx,
                            lineno,
                            lineno,
                            f"第 {lineno} 行 `open()` 的返回值赋给 `{var}` 后，在函数 `{fr.name}` 内既未调用 "
                            f"`{var}.close()` 也未使用 `with` 语句：异常或提前返回时文件句柄泄漏；请改用 "
                            "`with open(...) as f:` 管理。",
                        )
                    )
        return hits


class UnreachableCodeRule(_PyRule):
    """return/raise/break/continue 之后仍有同层语句。"""

    id = "PY-UNREACHABLE-CODE"
    category = Category.BUG
    severity = Severity.MEDIUM
    description = "控制流终止语句（return/raise/break/continue）之后存在同缩进语句：永远不可达，多为逻辑错误或死代码。"

    _TERM_RE: Pattern[str] = re.compile(r"^\s*(?:return|raise|break|continue)\b")
    _BLOCK_OPENER_RE: Pattern[str] = re.compile(r"^\s*(?:else|elif|except|finally|case)\b[^:]*:")

    @staticmethod
    def _balanced(ln: str) -> bool:
        return (
            ln.count("(") == ln.count(")")
            and ln.count("[") == ln.count("]")
            and ln.count("{") == ln.count("}")
        )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for fr in function_ranges(scan.masked):
            reported = False
            for lineno in range(fr.start, fr.end + 1):
                ln = scan.masked[lineno - 1]
                if reported or not self._TERM_RE.match(ln) or not self._balanced(ln):
                    continue
                term_indent = indent_width(ln)
                k = lineno + 1
                nxt: int | None = None
                while k <= fr.end:
                    if scan.masked[k - 1].strip():
                        nxt = k
                        break
                    k += 1
                if (
                    nxt is not None
                    and indent_width(scan.masked[nxt - 1]) == term_indent
                    and not self._BLOCK_OPENER_RE.match(scan.masked[nxt - 1])
                ):
                    hits.append(
                        self.make_hit(
                            ctx,
                            nxt,
                            nxt,
                            f"第 {nxt} 行的语句位于第 {lineno} 行 `{ln.strip().split()[0]}` 之后且与其同层，"
                            f"永远不会被执行（不可达代码）；请确认是否为遗漏的逻辑或应删除的死代码。",
                        )
                    )
                    reported = True
        return hits


_SHADOW_BUILTINS = frozenset(
    [
        "list", "dict", "str", "int", "float", "bool", "tuple", "set", "frozenset",
        "type", "id", "len", "sum", "min", "max", "abs", "round", "all", "any",
        "filter", "map", "range", "zip", "input", "format", "next", "iter", "vars",
        "locals", "globals", "object", "bytes", "complex", "slice", "super", "hash",
        "hex", "oct", "ord", "chr", "bin", "repr", "getattr", "setattr", "hasattr",
    ]
)


class ShadowBuiltinRule(_PyRule):
    """变量名覆盖 Python 内置名。"""

    id = "PY-SHADOW-BUILTIN"
    category = Category.BUG
    severity = Severity.LOW
    description = "把 list/dict/str 等内置名用作变量名：遮蔽内置函数，后续同作用域调用原始内置将直接报错。"

    _ASSIGN_RE: Pattern[str] = re.compile(r"^(\s*)([A-Za-z_]\w*)\s*=(?!=)")
    _FOR_RE: Pattern[str] = re.compile(r"^\s*for\s+([A-Za-z_]\w*)\s+in\b")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            name: str | None = None
            m = self._ASSIGN_RE.match(ln)
            if m:
                name = m.group(2)
            else:
                m2 = self._FOR_RE.match(ln)
                if m2:
                    name = m2.group(1)
            if name in _SHADOW_BUILTINS:
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行将内置名 `{name}` 用作变量名：遮蔽了同名内置函数/类型，"
                        "同作用域后续对内置 `{name}` 的调用会抛 TypeError；请换一个不冲突的名字。",
                    )
                )
        return hits


class AssertInProductionRule(_PyRule):
    """非测试文件使用 assert 做校验。"""

    id = "PY-ASSERT-IN-PROD"
    category = Category.BUG
    severity = Severity.MEDIUM
    description = "生产代码使用 assert 做输入校验：python -O 运行时 assert 会被整体剥离，校验静默失效。"

    _RE: Pattern[str] = re.compile(r"^\s*assert\b")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        if is_test_file(ctx.rel_path):
            return []
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            if self._RE.match(ln):
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行使用 `assert` 做运行时校验：解释器以 -O 优化模式运行时所有 assert "
                        "会被移除，校验将静默失效；生产校验应使用显式 if + raise（或参数校验库）。",
                    )
                )
        return hits


class IsLiteralComparisonRule(_PyRule):
    """is / is not 与字面量（字符串/数字/空容器）比较。"""

    id = "PY-IS-LITERAL"
    category = Category.BUG
    severity = Severity.LOW
    description = "用 `is` 与字符串/数字等字面量比较：is 是身份比较，字面量身份无保证，结果随解释器实现而异。"

    _RE: Pattern[str] = re.compile(r"\bis\s+not\s+(?=[bB]?['\"]|\d)|\bis\s+(?=[bB]?['\"]|\d)")

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
                        f"第 {idx + 1} 行使用 `is`/`is not` 与字面量（字符串/数字）比较：is 只比较对象身份，"
                        "小整数/短字符串驻留等实现细节使结果不可移植；应使用 `==`/`!=`。",
                    )
                )
        return hits


# ==================================================================== performance 类


class ListMembershipRule(_PyRule):
    """list 变量做 in 成员判断（对应金标 G3）。"""

    id = "PY-LIST-MEMBERSHIP"
    category = Category.PERFORMANCE
    severity = Severity.MEDIUM
    description = "用 list 做 `in` 成员判断是 O(n) 线性扫描，数据量大时应改用 set/dict（O(1)）。"

    _INIT_RE: Pattern[str] = re.compile(r"(\w+)\s*=\s*\[\s*\]")
    _MEMBER_RE: Pattern[str] = re.compile(r"(?<![\w.])(?:not\s+)?in\s+([A-Za-z_]\w*)")
    _FOR_HINT_RE: Pattern[str] = re.compile(r"\bfor\b")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for fr in function_ranges(scan.masked):
            inited: dict[str, int] = {}
            for lineno in range(fr.start, fr.end + 1):
                ln = scan.masked[lineno - 1]
                for m in self._INIT_RE.finditer(ln):
                    inited.setdefault(m.group(1), lineno)
            if not inited:
                continue
            for lineno in range(fr.start, fr.end + 1):
                ln = scan.masked[lineno - 1]
                if self._FOR_HINT_RE.search(ln):
                    continue  # for 头/推导式不属于成员判断
                for m in self._MEMBER_RE.finditer(ln):
                    var = m.group(1)
                    if var in inited and inited[var] < lineno:
                        hits.append(
                            self.make_hit(
                                ctx,
                                lineno,
                                lineno,
                                f"第 {lineno} 行对 list 变量 `{var}`（第 {inited[var]} 行以 `[]` 初始化）做 `in` "
                                "成员判断：线性扫描 O(n)，循环中反复判断时复杂度放大为 O(n²)；应改用 set。",
                                meta={"variable": var},
                            )
                        )
        return hits


class StrConcatInLoopRule(_PyRule):
    """循环内字符串 += / 自拼接（对应金标 G8）。"""

    id = "PY-STR-CONCAT-LOOP"
    category = Category.PERFORMANCE
    severity = Severity.LOW
    description = "循环内对字符串做 +/+= 拼接：每次都生成新字符串对象，总体 O(n²)；应收集到列表后 ''.join()。"

    _SELF_RE: Pattern[str] = re.compile(r"\b([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)\s*\+\s*(.+)$")
    _PLUSEQ_RE: Pattern[str] = re.compile(r"\b([A-Za-z_]\w*)\s*\+=\s*(.+)$")
    _NUM_START_RE: Pattern[str] = re.compile(r"^-?\.?\d")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        loops = loop_ranges(scan.masked)
        if not loops:
            return []
        str_inited = self._collect_str_vars(scan)
        hits: list[RuleHit] = []
        for lineno in range(1, len(scan.masked) + 1):
            if not line_in_loops(loops, lineno):
                continue
            ln = scan.masked[lineno - 1]
            m = self._SELF_RE.match(ln.strip())
            if m and m.group(1) == m.group(2):
                rhs = m.group(3).strip()
                if self._NUM_START_RE.match(rhs) or rhs.startswith((")", "]")):
                    continue  # 纯数字累加，非字符串拼接
                if '"' in rhs or "'" in rhs or m.group(1) in str_inited or rhs.startswith("str("):
                    hits.append(self._hit(ctx, lineno, m.group(1)))
                continue
            m2 = self._PLUSEQ_RE.match(ln.strip())
            if m2:
                var, rhs = m2.group(1), m2.group(2).strip()
                if ('"' in rhs or "'" in rhs or var in str_inited) and not self._NUM_START_RE.match(rhs):
                    hits.append(self._hit(ctx, lineno, var))
        return hits

    @staticmethod
    def _collect_str_vars(scan: PyScan) -> set[str]:
        out: set[str] = set()
        init = re.compile(r"([A-Za-z_]\w*)\s*=\s*(['\"])\s*\2")
        for ln in scan.masked:
            m = init.search(ln)
            if m:
                out.add(m.group(1))
        return out

    def _hit(self, ctx: RuleContext, lineno: int, var: str) -> RuleHit:
        return self.make_hit(
            ctx,
            lineno,
            lineno,
            f"第 {lineno} 行在循环内对字符串变量 `{var}` 做拼接：Python 字符串不可变，每次拼接都复制整串，"
            "大循环下是 O(n²) 性能陷阱；应先把片段收集到列表，最后用 `''.join(parts)` 一次性拼接。",
            meta={"variable": var},
        )


class IoInLoopRule(_PyRule):
    """循环体内出现 open( / .execute( / urlopen(。"""

    id = "PY-IO-IN-LOOP"
    category = Category.PERFORMANCE
    severity = Severity.HIGH
    description = "循环体内执行文件/数据库/网络 IO：每次迭代都付出完整 IO 往返成本，应移出循环或批量处理。"

    _OPEN_RE: Pattern[str] = re.compile(r"(?<![\w.])open\s*\(")
    _EXECUTE_RE: Pattern[str] = re.compile(r"\.\s*execute\s*\(")
    _URLOPEN_RE: Pattern[str] = re.compile(r"\burlopen\s*\(")
    _WITH_RE: Pattern[str] = re.compile(r"^\s*with\b")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        loops = loop_ranges(scan.masked)
        if not loops:
            return []
        kind_map = [("文件打开 open()", self._OPEN_RE), ("数据库执行 execute()", self._EXECUTE_RE), ("网络请求 urlopen()", self._URLOPEN_RE)]
        hits: list[RuleHit] = []
        for lineno in range(1, len(scan.masked) + 1):
            if not line_in_loops(loops, lineno):
                continue
            ln = scan.masked[lineno - 1]
            for kind, pat in kind_map:
                if pat is self._OPEN_RE and self._WITH_RE.match(ln):
                    continue  # with open 已确保逐次关闭，属于受控的逐条 IO
                if pat.search(ln):
                    hits.append(
                        self.make_hit(
                            ctx,
                            lineno,
                            lineno,
                            f"第 {lineno} 行在循环体内执行{kind}：每次迭代都产生一次完整 IO 往返，数据量大时耗时线性放大；"
                            "应把 IO 移出循环、改为批量处理（executemany / 连接复用 / 预取）。",
                            meta={"kind": kind},
                        )
                    )
        return hits


_REPEAT_EXCLUDE_BUILTINS = frozenset(["len", "str", "int", "float", "bool", "isinstance", "type", "print", "range"])


class RepeatInvariantCallRule(_PyRule):
    """同一函数内重复出现完全相同的函数调用表达式。"""

    id = "PY-REPEAT-CALL"
    category = Category.PERFORMANCE
    severity = Severity.LOW
    description = "同一函数内重复执行参数完全相同的函数调用：若调用非幂等或有开销，应提取为局部变量复用。"

    _CALL_RE: Pattern[str] = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\(([^(){}]*)\)")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        ranges = function_ranges(scan.masked)
        if not ranges:
            return []
        seen: dict[tuple[int, str], list[int]] = {}
        for lineno in range(1, len(scan.masked) + 1):
            fr = enclosing_function(ranges, lineno)
            if fr is None:
                continue
            ln = scan.masked[lineno - 1]
            if ln.lstrip().startswith(("def ", "async def", "class ", "@")):
                continue
            for m in self._CALL_RE.finditer(ln):
                name = m.group(1)
                base = name.split(".")[0]
                if _keyword.iskeyword(base) or base in _REPEAT_EXCLUDE_BUILTINS:
                    continue
                args = ",".join(a.strip() for a in m.group(2).split(","))
                expr = f"{name}({args})"
                seen.setdefault((fr.start, expr), []).append(lineno)
        hits: list[RuleHit] = []
        for (start, expr), lines in seen.items():
            if len(lines) < 2:
                continue
            for lineno in lines[1:]:
                hits.append(
                    self.make_hit(
                        ctx,
                        lineno,
                        lineno,
                        f"第 {lineno} 行重复执行了与第 {lines[0]} 行完全相同的调用 `{expr}`："
                        "重复计算/重复 IO，应把结果缓存到局部变量（若调用有副作用则需确认意图）。",
                        meta={"call": expr, "count": len(lines)},
                    )
                )
        return hits


class DeepcopyInLoopRule(_PyRule):
    """循环内 copy.deepcopy。"""

    id = "PY-DEEPCOPY-IN-LOOP"
    category = Category.PERFORMANCE
    severity = Severity.MEDIUM
    description = "循环内调用 copy.deepcopy：深拷贝是极重的递归操作，循环内反复执行会显著拖慢程序。"

    _RE: Pattern[str] = re.compile(r"\b(?:copy\s*\.\s*)?deepcopy\s*\(")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        loops = loop_ranges(scan.masked)
        if not loops:
            return []
        hits: list[RuleHit] = []
        for lineno in range(1, len(scan.masked) + 1):
            if not line_in_loops(loops, lineno):
                continue
            if self._RE.search(scan.masked[lineno - 1]):
                hits.append(
                    self.make_hit(
                        ctx,
                        lineno,
                        lineno,
                        f"第 {lineno} 行在循环体内调用 `deepcopy`：深拷贝需递归复制整个对象图，开销比浅拷贝高 1~2 个数量级；"
                        "应把不变对象拷贝移出循环，或改用不可变数据/写时复制。",
                    )
                )
        return hits


_TIMEOUT_CALL_RE = re.compile(
    r"\b(?:[\w.]*\.)?urlopen\s*\("
    r"|\b(?:requests|httpx)\s*\.\s*(?:get|post|put|delete|head|patch|request|stream)\s*\("
)


class RequestNoTimeoutRule(_PyRule):
    """urlopen / requests.get 等未设置 timeout（对应金标 G11）。"""

    id = "PY-NO-TIMEOUT"
    category = Category.PERFORMANCE
    severity = Severity.MEDIUM
    description = "网络请求未设置 timeout：默认无限等待，远端无响应时调用方将永久挂起。"

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            for col in find_call(ln, _TIMEOUT_CALL_RE):
                span = call_span(scan.masked, idx + 1, col)
                if span is None:
                    continue
                args_text = span[2]
                if re.search(r"\btimeout\s*=", args_text):
                    continue
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        span[0],
                        f"第 {idx + 1} 行发起网络请求未设置 `timeout=` 实参：底层默认无限等待，"
                        "对端挂起时调用线程/协程将被永久占用并放大为服务级联故障；请显式设置合理超时。",
                    )
                )
        return hits


# ==================================================================== style 类


class LongFunctionRule(_PyRule, LongFunctionRuleFamily):
    """函数超过 80 行。"""

    id = "PY-LONG-FUNCTION"

    # W15-D：消息尾注两语言文案本就不同（PY「圈复杂度高」/ JS「分支爆炸」），以参数显式化
    _TAIL_DETAIL = "圈复杂度高"

    # W15-D：绑定 Python 掩码扫描器与缩进口径的函数作用域推断（算法骨架在家族基类）
    _get_scan = staticmethod(get_scan)
    _function_ranges = staticmethod(function_ranges)


class DeepNestingRule(_PyRule, DeepNestingRuleFamily):
    """缩进达到 4 层及以上。"""

    id = "PY-DEEP-NESTING"

    # W15-D：层级口径以钩子显式化——Python 固定 4 空格/层（JS 按文件缩进单位换算）
    def _min_indent(self, masked: list[str]) -> int:
        return self.LEVEL_THRESHOLD * 4

    def _is_deep(self, ln: str, stripped: str, min_indent: int) -> bool:
        # 纯引号行是掩码后残留的字符串壳（Python 掩码保留引号位），不算代码行
        return bool(stripped) and set(stripped) != {'"', "'"} and indent_width(ln) >= min_indent

    # W15-D：绑定 Python 掩码扫描器
    _get_scan = staticmethod(get_scan)


class MagicNumberRule(_PyRule, MagicNumberRuleFamily):
    """与比较/赋值混用的无上下文大数字面量（≥1000）。"""

    id = "PY-MAGIC-NUMBER"

    # W15-D：Python 数字口径——仅十进制（JS 另支持十六进制）；import/from 行整行豁免
    _NUM_RE: Pattern[str] = re.compile(r"\b(\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)\b")
    _IMPORT_RE: Pattern[str] = re.compile(r"^\s*(?:from|import)\b")
    _EXAMPLE_HINT = "THRESHOLD = 10000"

    # W15-D：绑定 Python 掩码扫描器；数字解析/豁免口径按 Python 历史语义实现
    _get_scan = staticmethod(get_scan)

    def _skip_line(self, ln: str) -> bool:
        return bool(self._IMPORT_RE.match(ln))

    @staticmethod
    def _parse_number(text: str) -> float:
        return float(text.replace("_", ""))


class TodoFixmeCommentRule(_PyRule, TodoFixmeCommentRuleFamily):
    """TODO/FIXME/HACK 注释残留。"""

    id = "PY-TODO-FIXME"

    # W15-D：绑定 Python 掩码扫描器（家族 check 与 JS 侧逐字一致）
    _get_scan = staticmethod(get_scan)


_URL_RE = re.compile(r"https?://[^\s'\"<>，。；]+")
_CONST_URL_RE = re.compile(r"^\s*[A-Z][A-Z0-9_]*\s*=\s*['\"]https?://")


class HardcodedUrlRule(_PyRule):
    """非常量位置硬编码 http(s) 地址。"""

    id = "PY-HARDCODED-URL"
    category = Category.STYLE
    severity = Severity.LOW
    description = "在非常量位置硬编码 URL：环境切换（测试/预发/生产）时需改代码，应提取为配置或常量。"

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        spans_by_line: dict[int, list[tuple[int, int]]] = {}
        for lineno, a, b, _p in scan.strings:
            spans_by_line.setdefault(lineno, []).append((a, b))
        hits: list[RuleHit] = []
        for idx, raw in enumerate(ctx.lines):
            spans = spans_by_line.get(idx + 1, [])
            for m in _URL_RE.finditer(raw):
                if not any(a <= m.start() < b for a, b in spans):
                    continue  # 只报字符串字面量里的 URL
                if _CONST_URL_RE.match(raw):
                    continue  # 全大写常量赋值视为可接受位置
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行在非常量位置硬编码了 URL `{m.group(0)[:60]}`：环境迁移或域名变更时需改代码重新发布；"
                        "应提取到配置文件或模块级常量中统一管理。",
                        meta={"url": m.group(0)[:120]},
                    )
                )
                break  # 每行报一条即可
        return hits


class PrintDebugRule(_PyRule):
    """生产代码 print 调试残留。"""

    id = "PY-PRINT-DEBUG"
    category = Category.STYLE
    severity = Severity.LOW
    description = "生产代码中使用 print 输出：绕过日志体系，无级别/时间/上下文，且影响性能与日志采集。"

    _RE: Pattern[str] = re.compile(r"(?<![\w.])print\s*\(")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        if is_test_file(ctx.rel_path):
            return []
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            if self._RE.search(ln):
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行使用 `print` 直接输出：绕过日志系统（无级别、无时间戳、难采集），"
                        "疑似调试残留；请改用 logging 并配上合理日志级别。",
                    )
                )
        return hits


# ==================================================================== security 类


_SQL_KEYWORD_RE = re.compile(
    r"\b(?:SELECT\s+[^;]{0,200}?\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM"
    r"|DROP\s+(?:TABLE|DATABASE)|TRUNCATE\s+TABLE|UNION\s+(?:ALL\s+)?SELECT)\b",
    re.IGNORECASE,
)
_CONCAT_HINT_RE = re.compile(r"['\"]\s*\+|\+\s*['\"]|\.format\s*\(|['\"]\s*%\s*[\w(\[]")


def _is_dynamic_string_op(node: Any, source: bytes) -> bool:
    """AST 判定（P0-3 SQL 佐证）：节点是否为"字符串参与动态构造"的形态。

    覆盖三类 tree-sitter 节点：
    - ``binary_operator``：操作数含字符串字面量（+ 拼接 / % 格式化）；
    - ``string``：f-string 且含 ``interpolation`` 插值子节点；
    - ``call``：函数为 ``.format`` 属性调用。
    """
    ntype = node.type
    if ntype == "binary_operator":
        return any(c.type == "string" for c in node.children)
    if ntype == "string":
        return any(c.type == "interpolation" for c in node.children)
    if ntype == "call":
        func = node.child_by_field_name("function")
        if func is not None and func.type == "attribute":
            children = func.children
            return bool(children) and children[-1].type == "identifier" and ident_text(children[-1], source) == "format"
    return False


def _is_const_concat_use(node: Any, source: bytes, const_name: str) -> bool:
    """AST 判定（P0-3 SQL 二级传播佐证）：行上是否存在以常量为操作数的动态拼接。

    覆盖三形态：``NAME + …`` / ``… + NAME``（binary_operator 的标识符操作数）、
    ``NAME.format(…``（.format 调用的接收者根标识符）、f-string ``{NAME}`` 插值。
    """
    ntype = node.type
    if ntype == "binary_operator":
        return any(
            c.type == "identifier" and ident_text(c, source) == const_name for c in node.children
        )
    if ntype == "string":
        for child in node.children:
            if child.type != "interpolation":
                continue
            if any(
                gc.type == "identifier" and ident_text(gc, source) == const_name
                for gc in child.children
            ):
                return True
        return False
    if ntype == "call":
        func = node.child_by_field_name("function")
        if func is None or func.type != "attribute":
            return False
        children = func.children
        if not children or children[-1].type != "identifier":
            return False
        if ident_text(children[-1], source) != "format":
            return False
        base = root_identifier(func.child_by_field_name("object"))
        return base is not None and ident_text(base, source) == const_name
    return False


class SqlInjectionConcatRule(_PyRule):
    """SQL 语句与 +/f-string/%/.format 拼接（对应金标 G2/G12）。

    W20-D 增强（二级传播：SQL 常量模板的跨行拼接使用，同函数/同文件单步保守口径）：
    - 第一遍收集"SQL 常量变量"：形如 ``NAME = "…SELECT…FROM…"`` 的赋值，右侧为
      纯字符串字面量（含隐式相邻字面量拼接；f-string 前缀不收集），且字面量内容
      含 SQL 关键字（掩码行判定；支持 ``NAME: 注解 = "…"``,排除 ``==``/``+=``）；
    - 第二遍查找这些常量的"直接拼接使用"行并归因使用行：``NAME + …``、
      ``… + NAME``、``NAME.format(…``、f-string ``{NAME}`` 插值四种形态。
    - 保守取舍：
      * 作用域按"同文件"放宽：不做 enclosing-function 精确闭包——模块级常量
        （``PREFIX = "SELECT …"``）在函数内拼接使用本就可见（Python 作用域事实
        如此），函数内常量的跨函数使用极为罕见；代价是极端的跨函数同名遮蔽
        可能误报，换取对模板下沉到模块级这一真实场景的覆盖。
      * 单步限定：只追踪"常量变量直接拼接"，不追踪
        ``sql2 = sql + x; cur.execute(sql2)`` 的多步链——``sql2`` 右侧不是纯
        字面量，不会被收集为常量，传播链在第二级自然截断。
      * 二级判定仍要求"常量字面量含 SQL 关键字 + 使用行存在拼接形态"：
        参数化查询（``cur.execute(sql, (uid,))``）与纯引用（``cur.execute(sql)``）
        的使用行无拼接形态，不会命中；``tmpl % x`` 的 % 用法与跨行三引号模板
        不在单步收集/使用形态内（已知局限）。
      * 常量与常量拼接（两个操作数均为字面量/常量）与单行口径一致，按"存在
        拼接形态即报"处理（本规则家族既有的精度水位）。
    """

    id = "PY-SQL-INJECTION"
    category = Category.SECURITY
    severity = Severity.CRITICAL
    description = "SQL 语句以字符串拼接方式引入外部输入：攻击者可注入任意 SQL，导致数据泄露/篡改/删除。"

    # ---- W20-D 二级传播辅助常量（收敛为类属性，不新增模块级命名，避免与其他窗口冲突） ----
    # SQL 常量赋值头（掩码行上匹配）：分支 1 = ``NAME: 注解 =``，分支 2 = ``NAME =``。
    # 注解段禁止 = 和 :，天然排除 ``==`` 比较；分支 2 的 ``(?!=)`` 排除 ``==``，
    # ``NAME +=`` 因 ``+`` 挡位不匹配 ``\s*=`` 而天然不命中。
    _CONST_ASSIGN_RE: Pattern[str] = re.compile(
        r"^\s*(?:([A-Za-z_]\w*)\s*:\s*[^=:=]+=\s*|([A-Za-z_]\w*)\s*=(?!=)\s*)"
    )
    # 纯字面量右侧允许残留的非引号字符：r/b/u 前缀字母（f-string 不收集）
    _CONST_PREFIX_CHARS = frozenset("rbu")
    # 直接拼接使用形态（{name} 处代入 re.escape 后的常量名；在掩码行上匹配，
    # 字符串内容/注释已被掩码，天然避开字面量与注释内的假名字）
    _USE_PATTERNS: tuple[str, ...] = (
        r"(?<![\w.]){name}\s*\+(?!=)",           # NAME + …（排除 NAME +=）
        r"\+\s*{name}(?![\w.])",                 # … + NAME
        r"(?<![\w.]){name}\s*\.\s*format\s*\(",  # NAME.format(
    )
    # f-string 插值字段 {NAME}（允许转换 !x / 格式说明 :x 后缀；在原始行的
    # f-string 字面量内容上匹配——掩码行看不到插值内容）
    _FSTRING_FIELD_RE: Pattern[str] = re.compile(r"\{\s*([A-Za-z_]\w*)\s*(?:![^{}]*|:[^{}]*)?\}")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        spans_by_line: dict[int, list[tuple[int, int, str]]] = {}
        for lineno, a, b, prefix in scan.strings:
            spans_by_line.setdefault(lineno, []).append((a, b, prefix))
        hits: list[RuleHit] = []
        for idx, raw in enumerate(ctx.lines):
            spans = spans_by_line.get(idx + 1, [])
            if not spans:
                continue
            # SQL 关键字必须出现在字符串字面量内容内
            sql_in_string = any(
                any(a <= km.start() < b for km in _SQL_KEYWORD_RE.finditer(raw))
                for a, b, _p in spans
            )
            if not sql_in_string:
                continue
            masked = scan.masked[idx]
            fstring_interp = any("f" in p and "{" in raw[a:b] for a, b, p in spans)
            if _CONCAT_HINT_RE.search(masked) or fstring_interp:
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行的 SQL 语句通过字符串拼接（+/f-string/%/.format）引入变量："
                        "外部输入可直接改变语句语义，构成 SQL 注入漏洞，可能导致数据泄露或被整库删除；"
                        "请改为参数化查询（占位符 ?/%s + 参数元组）。",
                    )
                )
        # ---- W20-D 二级传播：SQL 常量模板的跨行直接拼接使用（同文件、单步） ----
        hit_lines = {h.line_start for h in hits}
        const_vars: dict[str, int] = {}  # 常量名 -> 定义行号
        for idx, raw in enumerate(ctx.lines):
            masked = scan.masked[idx]
            m = self._CONST_ASSIGN_RE.match(masked)
            if m is None:
                continue
            rhs = masked[m.end():]
            if "'" not in rhs and '"' not in rhs:
                continue  # 右侧无字符串字面量
            # 右侧须为纯字符串字面量：去掉引号/空白后只允许空或 r/b/u 前缀字母
            leftover = re.sub(r"[\s'\"]", "", rhs)
            if leftover and not set(leftover) <= self._CONST_PREFIX_CHARS:
                continue
            name = m.group(1) or m.group(2)
            if name is None:
                continue
            # SQL 关键字必须出现在 RHS 字面量内容内，且该字面量非 f-string
            sql_in_rhs = any(
                a >= m.end() and "f" not in p and _SQL_KEYWORD_RE.search(raw[a:b])
                for a, b, p in spans_by_line.get(idx + 1, [])
            )
            if sql_in_rhs:
                const_vars[name] = idx + 1
        if const_vars:
            for idx, masked in enumerate(scan.masked):
                lineno = idx + 1
                if lineno in hit_lines:
                    continue  # 该行已有单行命中，不重复归因
                raw = ctx.lines[idx]
                used_name: str | None = None
                for name in const_vars:
                    esc = re.escape(name)
                    if any(re.search(p.format(name=esc), masked) for p in self._USE_PATTERNS):
                        used_name = name
                        break
                    for a, b, p in spans_by_line.get(lineno, []):
                        if "f" not in p:
                            continue
                        if any(
                            fm.group(1) == name
                            for fm in self._FSTRING_FIELD_RE.finditer(raw[a:b])
                        ):
                            used_name = name
                            break
                    if used_name is not None:
                        break
                if used_name is None:
                    continue
                hits.append(
                    self.make_hit(
                        ctx,
                        lineno,
                        lineno,
                        f"第 {lineno} 行将 SQL 常量模板 `{used_name}`（第 {const_vars[used_name]} 行定义）"
                        "与外部输入拼接（跨行构造 SQL 语句）：外部输入可直接改变语句语义，风险同单行拼接，"
                        "构成 SQL 注入漏洞，可能导致数据泄露或被篡改；"
                        "请改为参数化查询（占位符 ?/%s + 参数元组）。",
                        meta={"sql_const_var": used_name, "const_line": const_vars[used_name]},
                    )
                )
        # ---- P0-3 AST 佐证：ctx.tree 可用时证实命中行的"动态拼接"形态，提升置信 ----
        self._ast_confirm(ctx, hits)
        hits.sort(key=lambda h: h.line_start)
        return hits

    def _ast_confirm(self, ctx: RuleContext, hits: list[RuleHit]) -> None:
        """P0-3 AST 佐证（保守口径）：只在 AST 证实动态拼接形态时提升置信。

        - ctx.tree 为 None（无解析器/降级/环境关闭）→ 空操作，行级逻辑即兜底路径；
        - 佐证失败 → 命中原样保留（默认置信），**绝不删减既有命中**；
        - 证实形态：单行命中要求行上出现"字符串参与二元运算（+/%）/ f-string 插值 /
          .format( 调用"任一 AST 节点；二级传播命中（meta["sql_const_var"]）要求
          行上出现以该常量为操作数的拼接、常量 .format( 或 f-string ``{常量}`` 插值。
        """
        if ctx.tree is None or not hits:
            return
        source = ctx.source.encode("utf-8", errors="replace")
        nodes_by_line = line_node_map(ctx.tree)
        for hit in hits:
            nodes = nodes_by_line.get(hit.line_start, [])
            const_name = hit.meta.get("sql_const_var")
            if const_name is None:
                if any(_is_dynamic_string_op(n, source) for n in nodes):
                    confirm_hit(hit)
            elif any(_is_const_concat_use(n, source, str(const_name)) for n in nodes):
                confirm_hit(hit)


class EvalExecRule(_PyRule):
    """eval / exec 动态执行。"""

    id = "PY-EVAL-EXEC"
    category = Category.SECURITY
    severity = Severity.CRITICAL
    description = "使用 eval/exec 动态执行代码：输入可被控制时等价于任意代码执行漏洞。"

    _RE: Pattern[str] = re.compile(r"(?<![\w.])\b(?:eval|exec)\s*\(")

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
                        f"第 {idx + 1} 行使用 `eval`/`exec` 动态执行代码：一旦参数受外部输入影响，攻击者可获得任意代码执行能力；"
                        "请改用显式解析（json/ast.literal_eval）或白名单映射。",
                    )
                )
        return hits


_CMD_RE = re.compile(r"\bos\s*\.\s*(?:system|popen)\s*\(")


class CommandInjectionRule(_PyRule):
    """os.system/os.popen 拼接变量执行命令。

    W19-C 增强：补充三类语义等价的注入形态——
    - subprocess.Popen 首参含变量/拼接/f-string 插值，或 kwargs 传 shell=True
      （shell=True 时字符串参数会进入 shell 解释，首参为字面量也报）；
    - os.execv/execve/execvp 参数列表中出现 sh/bash（含 /bin/sh 等路径形态）或
      "-c" 字样字面量，且后续元素含变量；
    - subprocess.run/call/check_output/Popen 参数为列表字面量且含 "sh"/"bash" +
      "-c" 字面量、其后元素含变量/拼接（run 无 shell=True 时列表参数本身安全，
      只有经 -c 中转才危险）。
    """

    id = "PY-COMMAND-INJECTION"
    category = Category.SECURITY
    severity = Severity.HIGH
    description = (
        "通过 os.system/os.popen 执行拼接了变量的命令：输入含 shell 元字符时可被命令注入。"
        "亦覆盖 subprocess.Popen 动态首参或 shell=True（字符串参数会进入 shell）、"
        "os.execv* 以 sh/bash -c 调 shell，以及 run/call/check_output/Popen 列表参数经"
        " `sh -c <动态命令>` 中转的等价注入形态。"
    )

    # W19-C 增强形态的调用起点：只认字面模块名（与 _CMD_RE 同风格，不做别名追踪）
    _SUBPROCESS_CALL_RE: Pattern[str] = re.compile(
        r"\bsubprocess\s*\.\s*(Popen|run|call|check_output)\s*\("
    )
    _OS_EXEC_RE: Pattern[str] = re.compile(r"\bos\s*\.\s*execv(?:e|p)?\s*\(")
    # shell 中转特征：argv 中的 "-c" 标志与 sh/bash 可执行名
    _SHELL_FLAG = "-c"
    _SHELL_PROG_NAMES = frozenset({"sh", "bash"})

    # 命中文案（{line} 由调用处填行号），风格对齐旧 os.system/os.popen 路径
    _MSG_POPEN_SHELL = (
        "第 {line} 行调用 `subprocess.Popen` 时传入 `shell=True`：字符串形式的命令参数会交由 shell 解释，"
        "命令中混入外部输入即可实现命令注入；"
        "请改用列表参数形式并保持 shell=False（默认），必要时对输入做白名单校验。"
    )
    _MSG_POPEN_DYNAMIC = (
        "第 {line} 行通过 `subprocess.Popen` 执行包含变量/拼接的命令："
        "输入中混入 `; rm -rf /` 等 shell 元字符即可实现命令注入；"
        "请改用列表参数形式（shell=False）并对输入做白名单校验。"
    )
    _MSG_SHELL_RELAY = (
        "第 {line} 行通过 shell 中转执行动态命令，等价命令注入："
        "`sh`/`bash -c`（含 os.execv* 调 shell 或 subprocess 列表参数中转）的 `-c` 之后为变量/拼接内容，"
        "将交由 shell 解释，混入 `; rm -rf /` 等元字符即可执行任意命令；"
        "请改用固定参数列表（shell=False）并对动态输入做白名单校验。"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        spans_by_line: dict[int, list[tuple[int, int, str]]] = {}
        for lineno, a, b, prefix in scan.strings:
            spans_by_line.setdefault(lineno, []).append((a, b, prefix))
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            for col in find_call(ln, _CMD_RE):
                span = call_span(scan.masked, idx + 1, col)
                if span is None:
                    continue
                args_text = span[2].strip()
                # 掩码行上字符串内容已置空：剩余字母/加号说明命令由变量或拼接构成
                leftover = args_text.replace('"', "").replace("'", "").strip()
                fstring_interp = any(
                    "f" in p and "{" in ctx.lines[idx][a:b]
                    for a, b, p in spans_by_line.get(idx + 1, [])
                )
                if leftover or fstring_interp:
                    hits.append(
                        self.make_hit(
                            ctx,
                            idx + 1,
                            span[0],
                            f"第 {idx + 1} 行通过 `os.system`/`os.popen` 执行包含变量/拼接的命令："
                            "输入中混入 `; rm -rf /` 等 shell 元字符即可实现命令注入；"
                            "请改用 subprocess 数组参数形式（shell=False）并对输入做白名单校验。",
                        )
                    )
        hits.extend(self._check_extended_forms(ctx, scan, spans_by_line))
        return hits

    # ------------------------------------------------------------ W19-C 增强

    def _check_extended_forms(
        self,
        ctx: RuleContext,
        scan: PyScan,
        spans_by_line: dict[int, list[tuple[int, int, str]]],
    ) -> list[RuleHit]:
        """subprocess 家族 / os.execv* 三形态检测（均在掩码行上匹配调用起点）。"""
        # 行偏移前缀和：把 (行, 列) 映射为全文件偏移，使字符串跨度可与参数文本按下标对齐
        line_offsets = [0]
        for ln in ctx.lines:
            line_offsets.append(line_offsets[-1] + len(ln) + 1)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            for pat in (self._SUBPROCESS_CALL_RE, self._OS_EXEC_RE):
                for m in pat.finditer(ln):
                    col = m.end() - 1
                    if col < 0 or ln[col] != "(":
                        continue
                    span = call_span(scan.masked, idx + 1, col)
                    if span is None:
                        continue
                    end_line, end_col, masked_args = span
                    args_base = line_offsets[idx] + col + 1
                    args_end = line_offsets[end_line - 1] + end_col
                    # 落在本调用参数区间内的字符串字面量：(参数文本内起, 止, 前缀, 原始内容)
                    literals = [
                        (
                            line_offsets[lineno - 1] + a - args_base,
                            line_offsets[lineno - 1] + b - args_base,
                            prefix,
                            ctx.lines[lineno - 1][a:b],
                        )
                        for lineno, a, b, prefix in scan.strings
                        if idx + 1 <= lineno <= end_line
                        and args_base <= line_offsets[lineno - 1] + a
                        and line_offsets[lineno - 1] + b <= args_end
                    ]
                    if pat is self._OS_EXEC_RE:
                        template = self._match_exec_relay(masked_args, literals)
                    else:
                        template = self._match_subprocess(
                            masked_args, m.group(1), literals
                        )
                    if template:
                        hits.append(
                            self.make_hit(
                                ctx, idx + 1, end_line, template.format(line=idx + 1)
                            )
                        )
        return hits

    def _match_subprocess(
        self,
        masked_args: str,
        func: str,
        literals: list[tuple[int, int, str, str]],
    ) -> str | None:
        """run/call/check_output/Popen：列表 -c 中转 / Popen 动态首参或 shell=True。"""
        relay = self._match_list_relay(masked_args, literals)
        if relay:
            return relay
        if func != "Popen":
            return None
        if re.search(r"\bshell\s*=\s*True\b", masked_args):
            # shell=True 时首参即便是字面量也会进入 shell 解释
            return self._MSG_POPEN_SHELL
        comma = self._first_top_level_comma(masked_args)
        first_end = comma if comma >= 0 else len(masked_args)
        fstring_interp = any(
            "f" in prefix and "{" in content and start < first_end
            for start, end, prefix, content in literals
        )
        if self._has_code_leftover(masked_args[:first_end]) or fstring_interp:
            return self._MSG_POPEN_DYNAMIC
        return None

    def _match_exec_relay(
        self,
        masked_args: str,
        literals: list[tuple[int, int, str, str]],
    ) -> str | None:
        """os.execv/execve/execvp：出现 sh/bash 或 "-c" 字样字面量且其后含变量。"""
        marker_end: int | None = None
        for start, end, prefix, content in literals:
            if self._is_shell_prog(content) or content.strip() == self._SHELL_FLAG:
                marker_end = end if marker_end is None else max(marker_end, end)
        if marker_end is None:
            return None
        if self._has_name_leftover(masked_args[marker_end:]):
            return self._MSG_SHELL_RELAY
        return None

    def _match_list_relay(
        self,
        masked_args: str,
        literals: list[tuple[int, int, str, str]],
    ) -> str | None:
        """列表字面量内 "sh"/"bash" + "-c" 且其后（列表内）元素含变量/拼接 → shell 中转。"""
        flag: tuple[int, int] | None = None  # 取最后一个 "-c" 字面量跨度
        for start, end, prefix, content in literals:
            if content.strip() == self._SHELL_FLAG:
                flag = (start, end)
        if flag is None:
            return None
        region = self._enclosing_list_region(masked_args, flag[0])
        if region is None:
            return None
        region_start, region_end = region
        has_prog = any(
            region_start <= start < flag[0] and self._is_shell_prog(content)
            for start, end, prefix, content in literals
        )
        if not has_prog:
            return None
        if self._has_name_leftover(masked_args[flag[1] : region_end]):
            return self._MSG_SHELL_RELAY
        return None

    def _is_shell_prog(self, content: str) -> bool:
        """字面量内容是否为 sh/bash 可执行名（"/bin/sh" 等路径形态归一后判断）。"""
        name = content.strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
        return name in self._SHELL_PROG_NAMES

    @staticmethod
    def _first_top_level_comma(text: str) -> int:
        """掩码参数文本中首个顶层逗号下标（字符串内容已掩码，逗号只来自代码结构）。"""
        depth = 0
        for i, ch in enumerate(text):
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == "," and depth == 0:
                return i
        return -1

    @staticmethod
    def _has_code_leftover(text: str) -> bool:
        """去掉引号后是否残留代码成分（变量/加号等）——命令由动态部分构成。"""
        return bool(text.replace('"', "").replace("'", "").strip())

    @staticmethod
    def _has_name_leftover(text: str) -> bool:
        """是否残留标识符成分（字母/数字/下划线）——存在变量参与。"""
        return bool(re.search(r"\w", text.replace('"', "").replace("'", "")))

    @staticmethod
    def _enclosing_list_region(text: str, inner: int) -> tuple[int, int] | None:
        """返回包含偏移 inner 的最内层 `[...]` 区域（起、止下标）；不在列表内返回 None。"""
        stack: list[int] = []
        for i, ch in enumerate(text):
            if ch == "[":
                stack.append(i)
            elif ch == "]" and stack:
                start = stack.pop()
                if start <= inner <= i:
                    return start, i
        return None


# 敏感词精确匹配集（R1-9）：标识符按 _/驼峰分词并做单数归一（R4-1）后，token 必须
# **精确**命中其一（历史上用子串包含，导致 _FINGERPRINT_KEY 之类的普通常量误报 critical）。
_SECRET_NAME_TOKENS = frozenset(
    {"key", "secret", "password", "passwd", "pwd", "token", "credential", "apikey"}
)

# password 家族（R4-7）：真实口令常是低熵自然词组合（如 mysupersecretkey），
# 高熵闸门会漏报，故对该家族放低随机性闸门（熵 ≥3.0 或字符集 ≥2 类）。
_PASSWORD_FAMILY_TOKENS = frozenset({"password", "passwd", "pwd"})

# 标识符分词：先按下划线切，再对每段按驼峰切（API_KEY → api/key；dbPassword → db/password）
_SECRET_TOKEN_SPLIT_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+")


def _singular(token: str) -> str:
    """复数形态归一（R4-1）：剥离常见英文复数后缀（…ies→y / …es / …s）。

    归一发生在分词之后、比对之前：API_KEYS → api/key、dbPasswords → db/password、
    credentials → credential。仅做后缀剥离，不引入词典，保证确定性。
    """
    if len(token) > 3 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _secret_name_tokens(name: str) -> set[str]:
    return {_singular(t.lower()) for t in _SECRET_TOKEN_SPLIT_RE.findall(name)}


def _shannon_entropy(text: str) -> float:
    """字符串 Shannon 熵（比特/字符）；空串为 0。"""
    if not text:
        return 0.0
    n = len(text)
    return -sum((c / n) * math.log2(c / n) for c in {ch: text.count(ch) for ch in set(text)}.values())


def _charset_diversity(text: str) -> int:
    """字符集类别数：小写 / 大写 / 数字 / 其它（符号）。"""
    return sum(
        (
            any(ch.islower() for ch in text),
            any(ch.isupper() for ch in text),
            any(ch.isdigit() for ch in text),
            any(not ch.isalnum() for ch in text),
        )
    )


class HardcodedSecretRule(_PyRule, HardcodedSecretRuleFamily):
    """硬编码密钥/口令（对应金标 G10）。

    R1-9 判定口径：
    - 标识符按 _/驼峰分词并做单数归一（R4-1：API_KEYS/dbPasswords/credentials 等
      复数形态不再漏报）后精确命中敏感词（key/secret/password/token/passwd/pwd/
      credential/apikey），且值满足随机性校验：ASCII、长度足够、
      Shannon 熵 ≥ 3.5 或字符集多样性 ≥ 3 类；
    - password/passwd/pwd 家族放低闸门（R4-7）：熵 ≥ 3.0 或字符集多样性 ≥ 2 类
      即命中——真实口令常是低熵自然词组合（mysupersecretkey），高熵闸门漏报；
    - 或值为 sk- 前缀的 API key 形态（同样要求随机性校验）。
    低熵占位串（"changeme"、"sk-aaaa..."）与中文提示文案不再误报。

    W30 注释行掩码预检：raw 行命中声明形态而 masked 行不再命中（赋值形态只存在于
    注释/字符串内容，如 docstring/三引号中间行的假声明）时跳过不报；masked 行保留
    引号定界符位置，真实赋值行的 `NAME = "` 形态在 masked 上依旧成立，不受影响。

    W15-D：category/severity/description/mask_snippet 上收至 HardcodedSecretRuleFamily
    （两语言完全一致）；检测强度两语言有意不同（见家族基类决策记录）。
    """

    id = "PY-HARDCODED-SECRET"

    _ASSIGN_RE: Pattern[str] = re.compile(r"^\s*(?P<name>[A-Za-z_]\w*)\s*(?::[^=]+)?=\s*(?P<q>['\"])")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, raw in enumerate(ctx.lines):
            m = self._ASSIGN_RE.match(raw)
            if not m:
                continue
            # W30 注释行掩码预检（判定口径零变化）：masked 行不再命中 ⇒ 跳过
            if self._is_comment_only(raw, scan.masked[idx]):
                continue
            q_col = m.end() - 1
            # W15-D：字符串取值收敛至 _scan_common.string_value；
            # escape_mode="none" 保持 Python 历史语义（反斜杠不转义跳转，见该函数 docstring）
            value = string_value(raw, q_col, escape_mode="none")
            if value is None:
                continue
            name = m.group("name")
            name_tokens = _secret_name_tokens(name) & _SECRET_NAME_TOKENS
            if name_tokens:
                if name_tokens & _PASSWORD_FAMILY_TOKENS:
                    secretish = self._looks_secretish_relaxed(value)  # R4-7：口令家族放低闸门
                else:
                    secretish = self._looks_secretish(value)
                name_hit = len(value) >= 16 and secretish
            else:
                name_hit = False
            sk_hit = value.startswith("sk-") and len(value) >= 20 and self._looks_secretish(value)
            if name_hit or sk_hit:
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行将疑似敏感凭据硬编码在 `{name}` 的字符串字面量中："
                        "密钥随代码库扩散且会进入版本历史，泄露风险极高；应改从环境变量/密钥管理服务读取，"
                        "并立即轮换已泄露的凭据。",
                        meta={"var": name},
                    )
                )
        return hits

    @staticmethod
    def _looks_secretish(value: str) -> bool:
        """随机性校验：真密钥几乎都是 ASCII，且熵高或字符集多样（≥3 类）。"""
        if not value.isascii():
            return False  # 非 ASCII（自然语言提示/文案）不算凭据
        if _shannon_entropy(value) >= 3.5:
            return True
        return _charset_diversity(value) >= 3

    @staticmethod
    def _looks_secretish_relaxed(value: str) -> bool:
        """R4-7：password 家族的放宽随机性闸门——熵 ≥3.0 或字符集多样性 ≥2 类。

        仍要求 ASCII：自然语言提示文案（含中文）不算口令凭据。
        """
        if not value.isascii():
            return False
        if _shannon_entropy(value) >= 3.0:
            return True
        return _charset_diversity(value) >= 2

    def _is_comment_only(self, raw: str, masked: str) -> bool:
        """W30 注释行掩码预检：raw 行命中声明形态而 masked 行不再命中时为 True。

        掩码扫描器把字符串内容与注释置为空格但保留引号定界符位置：真实赋值行
        `NAME = "` 形态在 masked 行依旧成立（预检放行、不误杀）；仅当该形态只
        存在于注释/字符串内容（docstring/三引号中间行的假声明等）时 masked 行
        才会失配，按注释行跳过不报。
        """
        return self._ASSIGN_RE.match(raw) is not None and self._ASSIGN_RE.match(masked) is None


class UnsafeDeserializeRule(_PyRule):
    """不安全反序列化：pickle.loads / yaml.load 无 Loader。"""

    id = "PY-UNSAFE-DESERIALIZE"
    category = Category.SECURITY
    severity = Severity.HIGH
    description = "使用 pickle.loads 或未指定安全 Loader 的 yaml.load 反序列化外部数据：可被构造为任意代码执行。"

    _PICKLE_RE: Pattern[str] = re.compile(r"\b(?:pickle|cPickle|dill|_pickle)\s*\.\s*loads?\s*\(")
    _YAML_LOAD_RE: Pattern[str] = re.compile(r"\byaml\s*\.\s*load\s*\(")
    _YAML_UNSAFE_RE: Pattern[str] = re.compile(r"\byaml\s*\.\s*(?:unsafe_load|full_load|full_load_all)\s*\(")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            if self._PICKLE_RE.search(ln):
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行使用 pickle/dill 反序列化：pickle 数据可携带任意对象构造函数，"
                        "反序列化不可信数据等价于任意代码执行；跨信任边界请改用 JSON 等安全格式。",
                    )
                )
                continue
            m = self._YAML_UNSAFE_RE.search(ln)
            if m:
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行使用 `yaml.unsafe_load/full_load`：允许构造任意 Python 对象，"
                        "反序列化不可信数据可导致代码执行；请改用 `yaml.safe_load`。",
                    )
                )
                continue
            for col in find_call(ln, self._YAML_LOAD_RE):
                span = call_span(scan.masked, idx + 1, col)
                if span is None:
                    continue
                if re.search(r"\bLoader\s*=", span[2]):
                    continue
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        span[0],
                        f"第 {idx + 1} 行 `yaml.load` 未指定 `Loader=` 参数：默认加载器可构造任意 Python 对象，"
                        "处理不可信输入时存在代码执行风险；请显式传入 `Loader=yaml.SafeLoader`。",
                    )
                )
        return hits


# ==================================================================== 注册


def build_python_rules() -> list[Rule]:
    """构建全部内置 Python 规则实例（顺序即默认报告顺序）。"""
    return [
        # bug
        BareExceptRule(),
        ExceptPassRule(),
        EmptyExceptContextRule(),
        MutableDefaultArgRule(),
        EqNoneRule(),
        OpenNoCloseRule(),
        UnreachableCodeRule(),
        ShadowBuiltinRule(),
        AssertInProductionRule(),
        IsLiteralComparisonRule(),
        # performance
        ListMembershipRule(),
        StrConcatInLoopRule(),
        IoInLoopRule(),
        RepeatInvariantCallRule(),
        DeepcopyInLoopRule(),
        RequestNoTimeoutRule(),
        # style
        LongFunctionRule(),
        DeepNestingRule(),
        MagicNumberRule(),
        TodoFixmeCommentRule(),
        HardcodedUrlRule(),
        PrintDebugRule(),
        # security
        SqlInjectionConcatRule(),
        EvalExecRule(),
        CommandInjectionRule(),
        HardcodedSecretRule(),
        UnsafeDeserializeRule(),
    ]
