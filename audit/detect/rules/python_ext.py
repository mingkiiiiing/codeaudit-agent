"""Python 静态规则库·扩充第二期（W6-A3，8 条）。

与 python.py 同一实现思路：基于 `_python_common` 的逐行掩码扫描 + 作用域启发式
（不依赖 tree-sitter），保证行号精确落在真实代码行上；所有规则 `check()` 纯函数式，
不修改 ctx、无 IO。新规则一律放本文件（`*_ext.py`），不触碰 python.py（W6-A2 所有权）。

每条规则均带 good_example/bad_example 类属性（契约 v1.6），供
scripts/gen_rule_docs.py 在规则手册中渲染正反示例。
"""

from __future__ import annotations

import re
from typing import Pattern

from audit.detect.base import Rule, RuleContext
from audit.detect.rules._python_common import (
    call_span,
    enclosing_function,
    find_call,
    function_ranges,
    get_scan,
)
from audit.models import Category, RuleHit, Severity

__all__ = ["build_python_ext_rules"]


class _PyExtRule(Rule):
    """Python 扩充规则公共基类：声明语言。"""

    languages = ("python",)


# ==================================================================== bug 类


_SUBPROCESS_IMPORT_RE = re.compile(r"^\s*(?:import\s+(?P<mod>\w+)(?:\s+as\s+(?P<alias>\w+))?|from\s+subprocess\s+import\s+(?P<names>[\w\s,]+?))\s*(?:#.*)?$")
_SUBPROCESS_FUNCS = ("run", "call")


class SubprocessWithoutCheckRule(_PyExtRule):
    """subprocess.run/call 未传 check=True：命令失败被静默忽略。"""

    id = "PY-SUBPROCESS-WITHOUT-CHECK"
    category = Category.BUG
    severity = Severity.HIGH
    description = (
        "subprocess.run/call 未设置 check=True：命令非零退出不抛异常，失败被静默吞掉，"
        "后续步骤在错误前提下继续执行；应显式传 check=True 或自行检查 returncode。"
    )

    bad_example = (
        "import subprocess\n"
        "\n"
        "subprocess.run(['git', 'push', 'origin', 'main'])  # 推送失败也无感知\n"
    )
    good_example = (
        "import subprocess\n"
        "\n"
        "subprocess.run(['git', 'push', 'origin', 'main'], check=True)  # 失败即抛 CalledProcessError\n"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        # 收集可调用名：模块别名（subprocess / sp）与 from-import 的直接名
        module_aliases: list[str] = []
        plain_names: set[str] = set()
        for ln in scan.masked:
            m = _SUBPROCESS_IMPORT_RE.match(ln)
            if not m:
                continue
            if m.group("names"):
                for part in m.group("names").split(","):
                    name = part.strip()
                    if name in _SUBPROCESS_FUNCS:
                        plain_names.add(name)
                    elif " as " in name:
                        local, _, src = name.partition(" as ")
                        if src.strip() in _SUBPROCESS_FUNCS:
                            plain_names.add(local.strip())
            elif m.group("mod") == "subprocess":
                module_aliases.append(m.group("alias") or "subprocess")
        if not module_aliases and not plain_names:
            return []
        patterns = [re.compile(rf"\b{re.escape(a)}\s*\.\s*(?:run|call)\s*\(") for a in module_aliases]
        patterns += [re.compile(rf"(?<![\w.]){re.escape(n)}\s*\(") for n in sorted(plain_names)]
        hits: list[RuleHit] = []
        seen: set[tuple[int, str]] = set()
        for idx, ln in enumerate(scan.masked):
            stripped = ln.strip()
            if stripped.startswith(("def ", "class ")):
                continue
            for pat in patterns:
                for col in find_call(ln, pat):
                    span = call_span(scan.masked, idx + 1, col)
                    if span is None:
                        continue
                    key = (idx + 1, pat.pattern)
                    if key in seen:
                        continue
                    if re.search(r"\bcheck\s*=", span[2]):
                        continue
                    seen.add(key)
                    hits.append(
                        self.make_hit(
                            ctx,
                            idx + 1,
                            span[0],
                            f"第 {idx + 1} 行调用 `subprocess.run/call` 未设置 `check=True`："
                            "命令以非零状态码退出时不会抛出 CalledProcessError，失败被静默忽略，"
                            "后续逻辑会在错误前提下继续执行；请显式传 `check=True` 或检查 `returncode`。",
                        )
                    )
        return hits


_BINARY_MODE_RE = re.compile(r"^[rwxaUtU+]*b[rwxaUtU+]*$")


class OpenWithoutEncodingRule(_PyExtRule):
    """open() 未显式指定 encoding：跨平台默认编码不一致导致乱码/DecodeError。"""

    id = "PY-OPEN-WITHOUT-ENCODING"
    category = Category.BUG
    severity = Severity.LOW
    description = (
        "open() 打开文本文件未显式传 encoding=：实际编码随平台与 locale 变化"
        "（Windows 常为 GBK/cp936，Linux 为 UTF-8），同一份代码跨平台读写中文即乱码或抛 DecodeError。"
    )

    bad_example = (
        "with open('config.json') as f:  # Windows 上默认 GBK，遇 UTF-8 中文即崩\n"
        "    data = f.read()\n"
    )
    good_example = (
        "with open('config.json', encoding='utf-8') as f:\n"
        "    data = f.read()\n"
    )

    _OPEN_RE: Pattern[str] = re.compile(r"(?<![\w.])open\s*\(")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        spans_by_line: dict[int, list[tuple[int, int]]] = {}
        for lineno, a, b, _prefix in scan.strings:
            spans_by_line.setdefault(lineno, []).append((a, b))
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            for col in find_call(ln, self._OPEN_RE):
                span = call_span(scan.masked, idx + 1, col)
                if span is None:
                    continue
                if re.search(r"\bencoding\s*=", span[2]):
                    continue
                # 二进制模式（'rb'/'wb+' 等）无编码概念，跳过
                if self._is_binary(ctx, spans_by_line, idx + 1, span[0]):
                    continue
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        span[0],
                        f"第 {idx + 1} 行 `open()` 未显式传 `encoding=` 实参：文本编码取决于平台 locale"
                        "（Windows 默认 GBK/cp936，Linux 默认 UTF-8），跨平台运行时中文内容会乱码"
                        "或抛 UnicodeDecodeError；请显式指定 `encoding='utf-8'`（二进制模式除外）。",
                    )
                )
        return hits

    @staticmethod
    def _is_binary(
        ctx: RuleContext,
        spans_by_line: dict[int, list[tuple[int, int]]],
        start: int,
        end: int,
    ) -> bool:
        for lineno in range(start, end + 1):
            for a, b in spans_by_line.get(lineno, []):
                content = ctx.lines[lineno - 1][a:b].strip().strip("'\"")
                if _BINARY_MODE_RE.match(content):
                    return True
        return False


class AssertTupleRule(_PyExtRule):
    """if/while 条件为恒真元组：if (a, b): 永远为 True。"""

    id = "PY-ASSERT-TUPLE"
    category = Category.BUG
    severity = Severity.HIGH
    description = (
        "if/while 的条件是带括号的元组（如 `if (a, b):`）：非空元组恒为真，"
        "分支永远执行，作者多半想写 `if a and b:` 或 `if a == (x, y):`，属必现逻辑错误。"
    )

    bad_example = (
        "def check(a, b):\n"
        "    if (a, b):  # 恒为 True，b 的校验从未生效\n"
        "        return True\n"
        "    return False\n"
    )
    good_example = (
        "def check(a, b):\n"
        "    if a and b:\n"
        "        return True\n"
        "    return False\n"
    )

    _HEAD_RE: Pattern[str] = re.compile(r"^\s*(?:if|elif|while)\s*\(")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            m = self._HEAD_RE.match(ln)
            if not m:
                continue
            open_col = m.end() - 1
            close_col = self._matching_paren(ln, open_col)
            if close_col is None:
                continue
            tail = ln[close_col + 1 :].strip()
            if tail not in ("", ":"):
                continue  # 条件不止这个括号组（如 `if (a, b) == x:`），非恒真元组
            if self._has_top_level_comma(ln, open_col + 1, close_col):
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行的条件是括号包裹的元组（元素间含逗号）：非空元组恒为真，"
                        "该分支无条件执行，与作者意图（逗号常为 and/or/== 的笔误）不符；"
                        "请确认应写 `and`/`or` 还是与元组字面量比较。",
                    )
                )
        return hits

    @staticmethod
    def _matching_paren(ln: str, open_col: int) -> int | None:
        depth = 0
        for k in range(open_col, len(ln)):
            ch = ln[k]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
                if depth == 0:
                    # 开括号必为 '('（由 _HEAD_RE 保证），配对收口也必为 ')'
                    return k if ch == ")" else None
        return None

    @staticmethod
    def _has_top_level_comma(ln: str, start: int, end: int) -> bool:
        depth = 0
        for k in range(start, end):
            ch = ln[k]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == "," and depth == 0:
                return True
        return False


class MutableClassAttrRule(_PyExtRule):
    """类体直接以 []/{}/set() 等可变字面量定义类属性：实例间共享。"""

    id = "PY-MUTABLE-CLASS-ATTR"
    category = Category.BUG
    severity = Severity.MEDIUM
    description = (
        "类体中以 []/{}/set() 等可变字面量定义类属性：该对象挂在类上、全部实例共享，"
        "任一实例原地修改会波及其他实例；应在 __init__ 中为每个实例创建新对象。"
    )

    bad_example = (
        "class Basket:\n"
        "    items = []  # 所有 Basket 实例共享同一个 list\n"
        "\n"
        "    def add(self, item):\n"
        "        self.items.append(item)\n"
    )
    good_example = (
        "class Basket:\n"
        "    def __init__(self):\n"
        "        self.items = []  # 每个实例独立\n"
        "\n"
        "    def add(self, item):\n"
        "        self.items.append(item)\n"
    )

    _CLASS_RE: Pattern[str] = re.compile(r"^\s*class\s+\w+[^:]*:\s*$")
    _MUTABLE_ASSIGN_RE: Pattern[str] = re.compile(
        r"^\s+(?P<name>\w+)\s*(?::[^=]+)?=\s*(?P<val>\[\s*\]|\{\s*\}|set\s*\(\s*\)|list\s*\(\s*\)|dict\s*\(\s*\))\s*$"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        ranges = function_ranges(scan.masked)
        hits: list[RuleHit] = []
        class_end = 0
        class_indent = -1
        for idx, ln in enumerate(scan.masked):
            if self._CLASS_RE.match(ln):
                class_indent = self._indent(ln)
                class_end = self._class_body_end(scan.masked, idx, class_indent)
                continue
            if not (class_indent >= 0 and idx + 1 <= class_end):
                continue
            if self._indent(ln) <= class_indent:
                continue  # 已离开类体
            m = self._MUTABLE_ASSIGN_RE.match(ln)
            if not m:
                continue
            if enclosing_function(ranges, idx + 1) is not None:
                continue  # 方法体内的 self.x = [] 是实例属性，不在本规则范围
            hits.append(
                self.make_hit(
                    ctx,
                    idx + 1,
                    idx + 1,
                    f"第 {idx + 1} 行类属性 `{m.group('name')}` 以可变字面量 `{m.group('val').strip()}` 初始化："
                    "该对象挂在类上并被全部实例共享，任一实例的原地修改（append/update）会波及其他实例；"
                    "请在 `__init__` 中为每个实例创建新对象。",
                    meta={"variable": m.group("name")},
                )
            )
        return hits

    @staticmethod
    def _indent(ln: str) -> int:
        expanded = ln.expandtabs(4)
        return len(expanded) - len(expanded.lstrip(" "))

    @staticmethod
    def _class_body_end(masked: list[str], idx0: int, class_indent: int) -> int:
        last = idx0
        k = idx0 + 1
        while k < len(masked):
            ln = masked[k]
            if ln.strip():
                if MutableClassAttrRule._indent(ln) <= class_indent:
                    break
                last = k
            k += 1
        return last + 1


class TypeCompareRule(_PyExtRule):
    """type(x) ==/is type(y)：应改用 isinstance。"""

    id = "PY-TYPE-COMPARE"
    category = Category.STYLE
    severity = Severity.LOW
    description = (
        "用 `type(x) ==/is ...` 做类型判断：绕开继承体系（子类实例判为不等），"
        "且绕过 `__eq__` 语义；应改用 `isinstance(x, T)`。"
    )

    bad_example = "if type(payload) == dict:  # 子类实例会判为 False\n    handle(payload)\n"
    good_example = "if isinstance(payload, dict):\n    handle(payload)\n"

    _RE: Pattern[str] = re.compile(r"\btype\s*\([^()]*\)\s*(?:==|!=|is\s+not|is\b)")

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
                        f"第 {idx + 1} 行使用 `type(...) ==/is ...` 做类型判断：type 相等性不含继承关系，"
                        "子类实例会被判为 False，且失去多态语义；请改用 `isinstance(x, T)`。",
                    )
                )
        return hits


_LOGGING_CALL_RE = re.compile(
    r"\b(?:logging|logger|log)\s*\.\s*(?:debug|info|warning|warn|error|exception|critical|log)\s*\("
)
_EAGER_FORMAT_RE = re.compile(r"['\"]\s*%\s*[\w(\[]|\.format\s*\(")


class StringFormatInLoggingRule(_PyExtRule):
    """logging 调用内做 %/.format/f-string 格式化：应改惰性参数化。"""

    id = "PY-STRING-FORMAT-IN-LOGGING"
    category = Category.PERFORMANCE
    severity = Severity.LOW
    description = (
        "在 logging 调用里用 %/.format/f-string 预先拼好消息：即使该级别被过滤也会付出格式化成本，"
        "且丢失日志聚合字段；应改用惰性参数化 `logger.info('value: %s', x)`。"
    )

    bad_example = (
        "import logging\n"
        "\n"
        "logger = logging.getLogger(__name__)\n"
        "logger.debug('processed %d items' % n)      # 关闭 debug 也执行格式化\n"
        "logger.info('user {}'.format(user))\n"
        "logger.warning(f'latency {latency}ms')\n"
    )
    good_example = (
        "import logging\n"
        "\n"
        "logger = logging.getLogger(__name__)\n"
        "logger.debug('processed %d items', n)\n"
        "logger.info('user %s', user)\n"
        "logger.warning('latency %sms', latency)\n"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            for col in find_call(ln, _LOGGING_CALL_RE):
                span = call_span(scan.masked, idx + 1, col)
                if span is None:
                    continue
                args_masked = span[2]
                if _EAGER_FORMAT_RE.search(args_masked):
                    hits.append(self._hit(ctx, idx + 1, span[0], "%/.format"))
                    continue
                if self._has_fstring(ctx, scan, idx + 1, span[0]):
                    hits.append(self._hit(ctx, idx + 1, span[0], "f-string"))
        return hits

    @staticmethod
    def _has_fstring(ctx: RuleContext, scan, start: int, end: int) -> bool:
        for lineno, a, b, prefix in scan.strings:
            if not (start <= lineno <= end) or "f" not in prefix:
                continue
            if "{" in ctx.lines[lineno - 1][a:b]:
                return True
        return False

    def _hit(self, ctx: RuleContext, line: int, end_line: int, kind: str) -> RuleHit:
        return self.make_hit(
            ctx,
            line,
            end_line,
            f"第 {line} 行的 logging 调用使用{kind}在传参前完成字符串格式化："
            "即使日志级别被过滤也会付出格式化开销，且丢失结构化聚合能力；"
            "请改为惰性参数化形式 `logger.info('value: %s', x)`。",
            meta={"kind": kind},
        )


class ReturnInInitRule(_PyExtRule):
    """__init__ 中 return 非空值：运行时抛 TypeError。"""

    id = "PY-RETURN-IN-INIT"
    category = Category.BUG
    severity = Severity.HIGH
    description = (
        "__init__ 中 `return` 了非 None 值：Python 规定构造器必须返回 None，"
        "实例化时直接抛 `TypeError: __init__() should return None`，属必现运行时错误。"
    )

    bad_example = (
        "class Client:\n"
        "    def __init__(self, cfg):\n"
        "        self.cfg = cfg\n"
        "        return self  # TypeError: __init__() should return None\n"
    )
    good_example = (
        "class Client:\n"
        "    def __init__(self, cfg):\n"
        "        self.cfg = cfg\n"
    )

    _RETURN_RE: Pattern[str] = re.compile(r"^\s*return\b(.*)$")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for fr in function_ranges(scan.masked):
            if fr.name != "__init__":
                continue
            for lineno in range(fr.start + 1, fr.end + 1):
                m = self._RETURN_RE.match(scan.masked[lineno - 1])
                if not m:
                    continue
                value = m.group(1).strip().rstrip(";").strip()
                if value and value != "None":
                    hits.append(
                        self.make_hit(
                            ctx,
                            lineno,
                            lineno,
                            f"第 {lineno} 行在 `__init__` 中 `return` 了非 None 值：构造器返回非 None 会在"
                            "实例化时抛出 `TypeError: __init__() should return None, not ...`；"
                            "如需返回实例请改用类方法工厂（`__new__` 或 classmethod 构造器）。",
                        )
                    )
        return hits


class GlobalStateMutateRule(_PyExtRule):
    """函数内 global 声明并对模块级可变状态赋值。"""

    id = "PY-GLOBAL-STATE-MUTATE"
    category = Category.STYLE
    severity = Severity.LOW
    description = (
        "函数内通过 `global` 声明并对模块级变量赋值：写操作散落在函数间、执行顺序隐式耦合，"
        "测试互相污染且并发不安全；应改为显式传参/返回值，或收敛到类/单例中管理。"
    )

    bad_example = (
        "cache = {}\n"
        "\n"
        "def reset():\n"
        "    global cache\n"
        "    cache = {}  # 模块状态被函数隐式改写\n"
    )
    good_example = (
        "class Cache:\n"
        "    def __init__(self) -> None:\n"
        "        self.data: dict = {}\n"
        "\n"
        "    def reset(self) -> None:\n"
        "        self.data = {}\n"
    )

    _GLOBAL_RE: Pattern[str] = re.compile(r"^\s*global\s+([\w\s,]+?)\s*(?:#.*)?$")
    _MODULE_ASSIGN_RE: Pattern[str] = re.compile(r"^([A-Za-z_]\w*)\s*(?::[^=]+)?=")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        module_vars = {
            m.group(1) for ln in scan.masked if (m := self._MODULE_ASSIGN_RE.match(ln))
        }
        if not module_vars:
            return []
        hits: list[RuleHit] = []
        for fr in function_ranges(scan.masked):
            declared: dict[str, int] = {}
            for lineno in range(fr.start + 1, fr.end + 1):
                ln = scan.masked[lineno - 1]
                m = self._GLOBAL_RE.match(ln)
                if m:
                    for name in m.group(1).split(","):
                        name = name.strip()
                        if name in module_vars and not name.isupper():
                            declared.setdefault(name, lineno)
                    continue
                if not declared:
                    continue
                for name, decl_line in declared.items():
                    assign = re.compile(
                        rf"(?<![\w.]){re.escape(name)}\s*(?:[+\-*/%&|^]|<<|>>|\*\*|//)?=(?!=)"
                    )
                    if assign.search(ln):
                        hits.append(
                            self.make_hit(
                                ctx,
                                lineno,
                                lineno,
                                f"第 {lineno} 行对第 {decl_line} 行 `global` 声明的模块级变量 `{name}` 赋值："
                                "模块可变状态被函数隐式改写，调用顺序耦合、测试互相污染且并发不安全；"
                                "请改为显式传参/返回值，或将状态收敛到类实例中管理。",
                                meta={"variable": name},
                            )
                        )
        return hits


# ==================================================================== 注册


def build_python_ext_rules() -> list[Rule]:
    """构建扩充第二期全部 Python 规则实例（顺序即默认报告顺序）。"""
    return [
        SubprocessWithoutCheckRule(),
        OpenWithoutEncodingRule(),
        AssertTupleRule(),
        MutableClassAttrRule(),
        TypeCompareRule(),
        StringFormatInLoggingRule(),
        ReturnInInitRule(),
        GlobalStateMutateRule(),
    ]
