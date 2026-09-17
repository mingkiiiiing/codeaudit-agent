"""Python 并发缺陷规则库（W19-B）：无锁共享状态变异、async 内同步 sleep。

与 python_ext.py 同一实现思路：基于 `_python_common` 的逐行掩码扫描 + 缩进作用域
启发式（不依赖 tree-sitter），保证行号精确落在真实代码行上；所有规则 `check()`
纯函数式，不修改 ctx、无 IO。

P0-3 AST 佐证（保守口径）：ctx.tree 可用时用 AST 证实命中形态（无锁变异的
augmented_assignment/容器变异调用、async def 函数体上下文），证实→置信度
0.7→0.8（meta["ast_confirmed"]=True）；佐证失败或 tree=None（行级逻辑即兜底
路径）时命中原样保留，绝不删减既有命中。

两条规则均为"疑似级"启发：静态分析无法证明函数确实被多线程并发调用、也无法证明
协程的实际调度时序，故以多条件形态收敛误报（同时满足才报），并在各类 docstring
声明口径。registry 接线由集成人统一完成；本文件只提供 build_py_concurrency_rules()。
"""

from __future__ import annotations

import re
from typing import Any, Pattern

from audit.detect.ast_util import confirm_hit, end_line, ident_text, line_node_map, root_identifier, walk
from audit.detect.base import Rule, RuleContext
from audit.detect.rules._python_common import (
    enclosing_function,
    function_ranges,
    get_scan,
    indent_width,
)
from audit.models import Category, RuleHit, Severity

__all__ = ["SleepInAsyncRule", "UnsyncedSharedMutationRule", "build_py_concurrency_rules"]


class _PyConcurrencyRule(Rule):
    """Python 并发规则公共基类：声明语言。"""

    languages = ("python",)


# ================================================================ 公共扫描件

_ASYNC_DEF_RE: Pattern[str] = re.compile(r"^\s*async\s+def\b")
_ACQUIRE_RE: Pattern[str] = re.compile(r"\.\s*acquire\s*\(")
_WITH_RE: Pattern[str] = re.compile(r"^\s*with\b[^:]*:\s*$")
_IDENT_RE: Pattern[str] = re.compile(r"[A-Za-z_]\w*")


# ========================================== 规则 1：无锁共享可变状态变异

# 条件①：文件导入 threading / multiprocessing（含 `as` 别名与 from-import）
_THREADING_IMPORT_RE: Pattern[str] = re.compile(
    r"^\s*(?:import\s+(?:threading|multiprocessing)\b|from\s+(?:threading|multiprocessing)\s+import\b)"
)
# 条件②：模块级（缩进 0）可变变量初始化：[] / {} / set() / 0（含类型注解形式）
_MODULE_MUTABLE_RE: Pattern[str] = re.compile(
    r"^([A-Za-z_]\w*)\s*(?::[^=]+)?=\s*(?:\[\s*\]|\{\s*\}|set\s*\(\s*\)|0)\s*$"
)


def _is_unsynced_mutation(node: Any, source: bytes, var: str) -> bool:
    """AST 判定（P0-3 佐证）：节点是否为对 var 的增强赋值或 append/add/update 容器变异。"""
    if node.type == "augmented_assignment":
        left = node.child_by_field_name("left")
        return left is not None and left.type == "identifier" and ident_text(left, source) == var
    if node.type == "call":
        func = node.child_by_field_name("function")
        if func is None or func.type != "attribute":
            return False
        children = func.children
        if not children or children[-1].type != "identifier":
            return False
        if ident_text(children[-1], source) not in {"append", "add", "update"}:
            return False
        base = root_identifier(func.child_by_field_name("object"))
        return base is not None and ident_text(base, source) == var
    return False


class UnsyncedSharedMutationRule(_PyConcurrencyRule):
    """模块级可变状态被函数无锁读改写（疑似多线程竞态，丢失更新）。

    疑似级启发：三条件同时满足才报——①文件导入 threading/multiprocessing；
    ②存在模块级可变变量（顶层 `name = []/{}/set()` 或 `= 0`）；③函数体内对该
    变量做增强赋值（+=/-=）或容器变异（append/add/update），且不在 `with <lock>`
    上下文、所在函数内无 `lock.acquire()`。静态分析无法证明这些函数确实被多线程
    并发调用——单线程脚本中的同形代码属可解释的误报方向，故定 bug/medium。
    """

    id = "PY-UNSYNCED-SHARED-MUTATION"
    category = Category.BUG
    severity = Severity.MEDIUM
    description = (
        "文件导入 threading/multiprocessing 且存在模块级可变变量（[]/{}/set()/0），"
        "函数内对其做增强赋值或 append/add/update 变异却无锁保护：多线程并发时"
        "读-改-写交错会丢失更新；应用 Lock 保护临界区、改用 queue.Queue 或原子操作。"
        "疑似级启发：静态无法证明多线程实际调用。"
    )

    bad_example = (
        "import threading\n"
        "\n"
        "counter = 0\n"
        "\n"
        "def worker():\n"
        "    global counter\n"
        "    counter += 1  # 多线程同时执行时丢失更新\n"
    )
    good_example = (
        "import threading\n"
        "\n"
        "counter = 0\n"
        "lock = threading.Lock()\n"
        "\n"
        "def worker():\n"
        "    global counter\n"
        "    with lock:  # 临界区受锁保护\n"
        "        counter += 1\n"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        # 条件①：文件级别先看导入，无 threading/multiprocessing 直接放弃（最大头
        # 的误报来源——普通单线程脚本的模块级计数器——在这里被整文件排除）
        if not any(_THREADING_IMPORT_RE.match(ln) for ln in scan.masked):
            return []
        # 条件②：模块级（缩进 0）可变变量集合
        mutables = {m.group(1) for ln in scan.masked if (m := _MODULE_MUTABLE_RE.match(ln))}
        if not mutables:
            return []
        # 条件③：每个变量的"增强赋值 | 容器变异"合一匹配式（sorted 保证确定性）
        patterns = {
            name: re.compile(
                rf"(?<![\w.]){re.escape(name)}\s*(?:\+=|-=)"
                rf"|(?<![\w.]){re.escape(name)}\s*\.\s*(?:append|add|update)\s*\("
            )
            for name in mutables
        }
        ranges = function_ranges(scan.masked)
        lock_bodies = self._lock_with_bodies(scan.masked)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            lineno = idx + 1
            fr = enclosing_function(ranges, lineno)
            if fr is None:
                continue  # 条件③限定"函数体内"：模块顶层的同形语句不归本规则
            matched = sorted(n for n, pat in patterns.items() if pat.search(ln))
            if not matched:
                continue
            if any(a <= lineno <= b for a, b in lock_bodies):
                continue  # 处于 `with <lock>:` 临界区内，已有保护
            # 排除：所在函数体内出现过 lock.acquire()（手动加解锁风格）——整函数
            # 豁免。按最内层函数判定；acquire 在外层、变异在内层函数的罕见写法
            # 不覆盖（疑似级口径，见 docstring）。
            body = scan.masked[fr.start : fr.end]
            if any(_ACQUIRE_RE.search(b) for b in body):
                continue
            name = matched[0]
            hits.append(
                self.make_hit(
                    ctx,
                    lineno,
                    lineno,
                    f"第 {lineno} 行在函数 `{fr.name}` 中对模块级可变变量 `{name}` 做无锁读改写"
                    "（增强赋值或 append/add/update）：文件已导入 threading/multiprocessing，"
                    "多线程并发调用时读-改-写交错会丢失更新（竞态）；请用 `threading.Lock` "
                    "保护临界区、改用 `queue.Queue` 传递数据，或使用原子操作。",
                    meta={"variable": name, "func": fr.name},
                )
            )
        # P0-3 AST 佐证：证实"模块级可变变量 + 无锁变异语句"后提升置信（不删命中）
        self._ast_confirm(ctx, hits)
        return hits

    def _ast_confirm(self, ctx: RuleContext, hits: list[RuleHit]) -> None:
        """P0-3 AST 佐证（保守口径）：AST 证实变异形态才提升置信，失败原样保留。

        证实两个 AST 事实：①变量确为模块级（根直接子级 assignment 左值）；
        ②命中行确有对该变量的增强赋值或 append/add/update 调用节点。
        ctx.tree 为 None → 空操作（行级逻辑即兜底路径）。
        """
        if ctx.tree is None or ctx.tree.root_node is None or not hits:
            return
        source = ctx.source.encode("utf-8", errors="replace")
        # 模块级（AST 根的直接子级，含 expression_statement 包裹）assignment 左值名字
        module_names: set[str] = set()
        for top in ctx.tree.root_node.children:
            for node in top.children if top.type == "expression_statement" else (top,):
                if node.type != "assignment":
                    continue
                left = node.child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    module_names.add(ident_text(left, source))
        nodes_by_line = line_node_map(ctx.tree)
        for hit in hits:
            var = str(hit.meta.get("variable", ""))
            if not var or var not in module_names:
                continue
            if any(_is_unsynced_mutation(n, source, var) for n in nodes_by_line.get(hit.line_start, [])):
                confirm_hit(hit)

    @staticmethod
    def _lock_with_bodies(masked: list[str]) -> list[tuple[int, int]]:
        """收集 `with <含 lock 字样的表达式>:` 的 body 行区间（1-based 闭区间）。

        with 头的任一标识符含 "lock"（不区分大小写，如 lock/self._lock/Lock()）
        即视为锁上下文；`with block:` 之类命名会向漏报方向偏移，不向误报偏移，
        符合疑似级口径。
        """
        spans: list[tuple[int, int]] = []
        for idx, ln in enumerate(masked):
            if not _WITH_RE.match(ln):
                continue
            if not any("lock" in i.lower() for i in _IDENT_RE.findall(ln)):
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
                spans.append((idx + 2, last + 1))
        return spans


# ========================================== 规则 2：async def 内同步 sleep

# `import time` / `import time as t`
_IMPORT_TIME_RE: Pattern[str] = re.compile(r"^\s*import\s+time(?:\s+as\s+(\w+))?\s*$")
# `from time import sleep[, ...]`（含 `sleep as nap`）
_FROM_TIME_IMPORT_RE: Pattern[str] = re.compile(r"^\s*from\s+time\s+import\s+([\w\s,]+?)\s*$")


def _ast_async_spans(tree: Any) -> list[tuple[int, int]]:
    """AST 收集 async def 的行区间（1-based 闭区间；function_definition 含 async 子节点）。"""
    spans: list[tuple[int, int]] = []
    for node in walk(tree.root_node):
        if node.type == "function_definition" and any(c.type == "async" for c in node.children):
            spans.append((node.start_point[0] + 1, end_line(node)))
    return spans


class SleepInAsyncRule(_PyConcurrencyRule):
    """async def 内调用同步 time.sleep：阻塞整个事件循环。

    口径说明：`_python_common.function_ranges` 的 `_DEF_RE` 覆盖 `async def`
    （FuncRange 不区分同步/异步，此处回读函数头行判定 async）；行落在任一
    async def 范围内即报——协程内经同步 helper 间接执行 sleep 同样阻塞事件
    循环。静态无法证明该协程会被并发调度，单协程脚本属可解释的误报方向。
    """

    id = "PY-SLEEP-IN-ASYNC"
    category = Category.PERFORMANCE
    severity = Severity.MEDIUM
    description = (
        "async def 体内调用同步 time.sleep：阻塞整个事件循环，期间所有协程（含"
        "其他请求）都无法调度，异步吞吐骤降；应改用 `await asyncio.sleep(...)`。"
    )

    bad_example = (
        "import time\n"
        "\n"
        "async def poll():\n"
        "    while True:\n"
        "        time.sleep(1)  # 事件循环被阻塞 1 秒，其他协程全部停摆\n"
        "        await tick()\n"
    )
    good_example = (
        "import asyncio\n"
        "\n"
        "async def poll():\n"
        "    while True:\n"
        "        await asyncio.sleep(1)  # 异步让出，事件循环可调度其他协程\n"
        "        await tick()\n"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        # 收集 time 模块别名与 from-import 的直接调用名
        aliases = {"time"}
        plain: set[str] = set()
        for ln in scan.masked:
            m = _IMPORT_TIME_RE.match(ln)
            if m:
                if m.group(1):
                    aliases.add(m.group(1))
                continue
            m = _FROM_TIME_IMPORT_RE.match(ln)
            if m:
                for part in m.group(1).split(","):
                    part = part.strip()
                    if part == "sleep":
                        plain.add("sleep")
                    elif part.startswith("sleep"):
                        local = part.split("as", 1)[1].strip() if "as" in part else ""
                        if local:
                            plain.add(local)
        # async def 作用域（函数头行回读判定 async，见类 docstring）
        async_spans = [
            (fr.start, fr.end)
            for fr in function_ranges(scan.masked)
            if _ASYNC_DEF_RE.match(scan.masked[fr.start - 1])
        ]
        if not async_spans:
            return []
        alias_patterns = [
            re.compile(rf"\b{re.escape(a)}\s*\.\s*sleep\s*\(") for a in sorted(aliases)
        ]
        plain_patterns = [
            re.compile(rf"(?<![\w.]){re.escape(n)}\s*\(") for n in sorted(plain)
        ]
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            lineno = idx + 1
            if not any(a <= lineno <= b for a, b in async_spans):
                continue
            pat = next((p for p in alias_patterns if p.search(ln)), None)
            called = "time.sleep" if pat else None
            if pat is None and plain_patterns:
                pat = next((p for p in plain_patterns if p.search(ln)), None)
                called = "sleep" if pat else None
            if pat is None:
                continue
            hits.append(
                self.make_hit(
                    ctx,
                    lineno,
                    lineno,
                    f"第 {lineno} 行在 `async def` 内调用同步 `{called}`：会阻塞整个事件循环，"
                    "期间所有协程（含其他请求）无法调度，异步并发优势尽失；"
                    "请改用 `await asyncio.sleep(...)`。",
                    meta={"call": called},
                )
            )
        # P0-3 AST 佐证：AST 精确证实命中行位于 async def 函数体后提升置信（不删命中）
        self._ast_confirm(ctx, hits)
        return hits

    def _ast_confirm(self, ctx: RuleContext, hits: list[RuleHit]) -> None:
        """P0-3 AST 佐证（保守口径）：AST 判定命中行在 async def 作用域内才提升置信。

        行级缩进作用域启发式的"async def 函数体上下文"由 AST 精确证实；佐证失败
        （嵌套作用域歧义等）命中原样保留；ctx.tree 为 None → 空操作（行级兜底）。
        """
        if ctx.tree is None or ctx.tree.root_node is None or not hits:
            return
        spans = _ast_async_spans(ctx.tree)
        if not spans:
            return
        for hit in hits:
            if any(a <= hit.line_start <= b for a, b in spans):
                confirm_hit(hit)


# ---------------------------------------------------------------- 注册


def build_py_concurrency_rules() -> list[Rule]:
    """构建 W19-B 并发规则实例（顺序即默认报告顺序；registry 接线由集成人完成）。"""
    return [
        UnsyncedSharedMutationRule(),
        SleepInAsyncRule(),
    ]
