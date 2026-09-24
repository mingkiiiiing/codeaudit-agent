"""PY-TAINT-UNSAFE-SINK：函数内污点传播（外部输入 source → 危险汇点 sink，W30 卡 A）。

基于 ctx.tree（tree-sitter）做**python 单语言、函数内、单文件、两级以上赋值链**的
保守污点传播判定——本规则为 AST-only：tree=None（无解析器 / 降级 / 环境关闭）时
返回空清单（沿 PY-NONE-DEREF 先例：无 AST 宁可漏报，绝不产命中、绝不报错）。

分析口径（逐函数、顺序保守）：
- source 集（保守固定形态，不猜参数名）：
  ``request.args|form|values|cookies|headers.get(...)``、索引形态
  ``request.args["x"]``、``request.get_json(...)`` / ``request.get_data(...)``、
  ``request.form`` 直接赋值、``input(...)``、``sys.argv`` 及其下标 / 属性链；
  命中即把所在赋值左值名入污染集（记 source 行）；source 表达式本身是 sink
  实参时零传播直接命中。
- 传播（赋值链，可多级）：函数体内顺序扫描赋值（普通 / 增广 / 注解赋值）——
  右值表达式含污染集内名字（直接 Name、f-string 插值、str.format 实参、
  ``+`` 拼接、元组 / 列表 / dict 构造元素，统一按"右值引用污染名"判定）→
  左值名入污染集（记传播行）。
- sink 集（函数体内调用）：
  SQL ``<expr>.execute(...)`` / ``<expr>.executemany(...)``；
  动态执行 ``eval(...)`` / ``exec(...)``；
  命令 ``os.system(...)`` / ``os.popen(...)`` /
  ``subprocess.run|call|check_call|check_output|Popen(...)``（实参含污染名即命中，
  不特判 shell=True）。

已知边界（如实申报）：
- 跨函数 / 跨文件不追（被调函数返回值、闭包 / 参数传入的污点均不识别；
  函数参数一律不作 source）；
- 循环 / 分支内再赋值的路径敏感不做：按线性源码序顺序保守口径（同名覆盖只认
  最后一次；分支洗白按"后写为准"处理）；
- 别名导入的 sink 模块名不解析（``import os as o`` 后 ``o.system`` 不识别；
  ``from os import system`` 后裸名 ``system`` 亦不识别；eval/exec 只认裸名）；
- 属性链中间污染不传播（``obj.attr = tainted`` 后 ``obj.attr`` 不视为污染；
  属性 / 下标为赋值目标时整条赋值不入污染集）；
- ``request`` 别名赋值（``data = request`` 后经 ``data.args`` 取值）不识别；
- for 循环变量 / with-as / except-as / walrus 表达式内绑定不入污染集；
- ``execute`` / ``executemany`` 只查第一个实参（SQL 语句本身）：参数元组是
  参数化通道，参数化查询不误报；
- ``<expr>.execute`` 的汇点标签取 expr 根名（``db.session.execute`` 记为
  ``db.execute``），仅影响文案不影响判定。

check() 纯函数式：不修改 ctx、无 IO；同一 sink 行多链去重为一条命中。
"""

from __future__ import annotations

from typing import Any

from audit.detect.ast_util import ident_text, root_identifier, walk
from audit.detect.base import Rule, RuleContext
from audit.models import Category, RuleHit, Severity

__all__ = ["PyTaintUnsafeSinkRule", "build_py_taint_rules"]

# source 形态：request 的输入集合属性名（request.<collection> / .get / 索引）
_REQUEST_COLLECTIONS = frozenset({"args", "form", "values", "cookies", "headers"})
# source 形态：request 上的取值方法
_REQUEST_METHODS = frozenset({"get_json", "get_data"})

# sink 形态：subprocess 命令执行家族（不特判 shell=True）
_SUBPROCESS_FUNCS = frozenset({"run", "call", "check_call", "check_output", "Popen"})

# 嵌套作用域：外层函数分析跳过其子树（其自身作为 function_definition 单独分析）
_SCOPE_TYPES = ("function_definition", "class_definition", "lambda")

# sink 类别 -> 风险说明 / 修复建议（命中文案用）
_KIND_RISK = {
    "SQL": "任意 SQL（SQL 注入，可导致数据泄露或被篡改）",
    "动态执行": "任意代码（代码执行）",
    "命令": "shell 元字符（命令注入，可执行任意系统命令）",
}
_KIND_ADVICE = {
    "SQL": "请改为参数化查询（占位符 + 参数元组），不要把外部输入拼进 SQL 文本。",
    "动态执行": "请改用 ast.literal_eval / 显式白名单映射，不要执行外部输入构造的代码。",
    "命令": "请改用固定参数列表（subprocess 数组形式，shell=False）并对输入做白名单校验。",
}


def _request_collection_name(node: Any, source: bytes) -> str | None:
    """node 形如 ``request.<collection>``（字面根名）时返回 collection 名，否则 None。"""
    if node is None or node.type != "attribute":
        return None
    obj = node.child_by_field_name("object")
    attr = node.child_by_field_name("attribute")
    if (
        obj is not None
        and obj.type == "identifier"
        and ident_text(obj, source) == "request"
        and attr is not None
        and ident_text(attr, source) in _REQUEST_COLLECTIONS
    ):
        return ident_text(attr, source)
    return None


def _source_desc(node: Any, source: bytes) -> str | None:
    """node 本身是否为外部输入 source 形态；是则返回形态描述（如 request.args.get）。

    覆盖：request 集合的 .get / 索引 / 直接赋值、request.get_json / get_data、
    input(...)、sys.argv 及其下标 / 属性链（``sys.argv[1].strip()`` 经接收者链继承）。
    """
    if node is None:
        return None
    ntype = node.type
    if ntype == "call":
        func = node.child_by_field_name("function")
        if func is None:
            return None
        if func.type == "identifier":  # input(...)（裸名）
            return "input" if ident_text(func, source) == "input" else None
        if func.type == "attribute":
            attr = func.child_by_field_name("attribute")
            obj = func.child_by_field_name("object")
            if attr is not None:
                attr_name = ident_text(attr, source)
                if attr_name == "get" and obj is not None:
                    coll = _request_collection_name(obj, source)
                    if coll is not None:
                        return f"request.{coll}.get"
                if attr_name in _REQUEST_METHODS and (
                    obj is not None
                    and obj.type == "identifier"
                    and ident_text(obj, source) == "request"
                ):
                    return f"request.{attr_name}"
        # 其他调用：接收者本身是 source 形态则继承（如 sys.argv[1].strip()）
        return _source_desc(func, source)
    if ntype == "attribute":
        coll = _request_collection_name(node, source)
        if coll is not None:  # request.form 直接赋值 / request.args 裸引用
            return f"request.{coll}"
        obj = node.child_by_field_name("object")
        if obj is not None:
            if obj.type == "identifier" and ident_text(obj, source) == "sys":
                attr = node.child_by_field_name("attribute")
                if attr is not None and ident_text(attr, source) == "argv":
                    return "sys.argv"
            return _source_desc(obj, source)  # 属性链继承（sys.argv.x 等罕见形态）
        return None
    if ntype == "subscript":  # 索引形态：request.args["x"] / sys.argv[1]
        return _source_desc(node.child_by_field_name("value"), source)
    return None


def _find_source_in_expr(node: Any, source: bytes) -> tuple[int, str] | None:
    """表达式内（DFS 先序、最外层优先）首个 source 形态 -> (行号, 形态描述)。"""
    if node is None or node.type in _SCOPE_TYPES:
        return None
    desc = _source_desc(node, source)
    if desc is not None:
        return node.start_point[0] + 1, desc
    for child in node.named_children:
        found = _find_source_in_expr(child, source)
        if found is not None:
            return found
    return None


def _has_tainted_name(node: Any, tainted: dict[str, list], source: bytes) -> str | None:
    """表达式是否在"值位置"引用了污染集内的名字；命中返回该名字。

    值位置口径：属性链只查 object（跳过属性名标识符）、调用只查派生对象与实参
    （裸名被调不视为数据流）、关键字实参只查 value——避免 ``obj.x`` 的属性名 x、
    ``shell=True`` 的参数名等假阳性。
    """
    if node is None or node.type in _SCOPE_TYPES:
        return None
    ntype = node.type
    if ntype == "identifier":
        name = ident_text(node, source)
        return name if name in tainted else None
    if ntype == "attribute":
        return _has_tainted_name(node.child_by_field_name("object"), tainted, source)
    if ntype == "keyword_argument":
        return _has_tainted_name(node.child_by_field_name("value"), tainted, source)
    if ntype == "call":
        func = node.child_by_field_name("function")
        if func is not None and func.type in ("attribute", "subscript"):
            hit = _has_tainted_name(func, tainted, source)
            if hit is not None:
                return hit
        args = node.child_by_field_name("arguments")
        if args is not None:
            for arg in args.named_children:
                hit = _has_tainted_name(arg, tainted, source)
                if hit is not None:
                    return hit
        return None
    for child in node.named_children:
        hit = _has_tainted_name(child, tainted, source)
        if hit is not None:
            return hit
    return None


def _sink_info(func: Any, source: bytes) -> tuple[str, str] | None:
    """调用函数表达式是否为危险汇点形态；是则返回（汇点标签, 类别）。"""
    if func is None:
        return None
    if func.type == "identifier":
        name = ident_text(func, source)
        if name in ("eval", "exec"):
            return name, "动态执行"
        return None
    if func.type == "attribute":
        attr = func.child_by_field_name("attribute")
        obj = func.child_by_field_name("object")
        if attr is None or obj is None:
            return None
        attr_name = ident_text(attr, source)
        if attr_name in ("execute", "executemany"):
            base = root_identifier(obj)
            base_text = ident_text(base, source) if base is not None else "…"
            return f"{base_text}.{attr_name}", "SQL"
        if obj.type == "identifier":
            obj_name = ident_text(obj, source)
            if obj_name == "os" and attr_name in ("system", "popen"):
                return f"os.{attr_name}", "命令"
            if obj_name == "subprocess" and attr_name in _SUBPROCESS_FUNCS:
                return f"subprocess.{attr_name}", "命令"
    return None


def _line_text(ctx: RuleContext, line: int) -> str:
    """传播事件的行文本（去首尾空白，超长截断——仅用于链路展示）。"""
    text = ctx.lines[line - 1].strip() if 0 < line <= len(ctx.lines) else ""
    return text if len(text) <= 60 else text[:57] + "..."


class PyTaintUnsafeSinkRule(Rule):
    """函数内污点传播：外部输入经赋值链流入 SQL/动态执行/命令危险汇点。"""

    id = "PY-TAINT-UNSAFE-SINK"
    category = Category.SECURITY
    severity = Severity.HIGH
    languages = ("python",)
    description = (
        "外部输入（request.* / input / sys.argv）经函数内赋值链传播后流入危险汇点"
        "（execute/executemany、eval/exec、os.system/popen、subprocess 家族）："
        "攻击者可借外部输入注入 SQL / 代码 / 命令；请改用参数化查询、固定参数列表"
        "或白名单映射，不要把外部输入拼接进执行语句。"
    )

    bad_example = (
        "def show_user(request, cursor):\n"
        "    uid = request.args.get(\"uid\")\n"
        "    cursor.execute(\"SELECT * FROM users WHERE id=\" + uid)\n"
    )
    good_example = (
        "def show_user(request, cursor):\n"
        "    uid = request.args.get(\"uid\")\n"
        "    cursor.execute(\"SELECT * FROM users WHERE id=?\", (uid,))\n"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        tree = ctx.tree
        if tree is None or tree.root_node is None:
            return []  # AST-only：无 tree 时不产命中（兜底=无此规则，绝不误报）
        source = ctx.source.encode("utf-8", errors="replace")
        hits: list[RuleHit] = []
        for node in walk(tree.root_node):
            if node.type == "function_definition":
                self._check_function(ctx, node, source, hits)
        hits.sort(key=lambda h: h.line_start)
        return hits

    # ---------------------------------------------------------------- 单函数分析

    def _check_function(
        self, ctx: RuleContext, fn: Any, source: bytes, hits: list[RuleHit]
    ) -> None:
        """对单个函数体做线性源码序污点传播分析（嵌套作用域跳过，单独分析）。"""
        body = fn.child_by_field_name("body")
        if body is None:
            return
        tainted: dict[str, list[tuple[str, int, str]]] = {}  # 变量名 -> 污点链事件
        reported: set[int] = set()  # 已报 sink 行（同行去重）
        self._scan(ctx, body, source, tainted, reported, hits)

    def _scan(
        self,
        ctx: RuleContext,
        node: Any,
        source: bytes,
        tainted: dict[str, list[tuple[str, int, str]]],
        reported: set[int],
        hits: list[RuleHit],
    ) -> None:
        """先序遍历函数体：赋值节点拦截做污点绑定（先查右值 sink 再绑定左值），
        其余节点透传下钻并对 call 节点做 sink 检查。"""
        ntype = node.type
        if ntype in _SCOPE_TYPES:
            return  # 嵌套作用域：由 walk 主循环作为独立函数分析
        if ntype == "assignment":  # 普通 / 注解赋值（注解赋值同为 assignment 节点）
            right = node.child_by_field_name("right")
            if right is not None:
                self._scan(ctx, right, source, tainted, reported, hits)
            self._bind_assignment(ctx, node, source, tainted)
            return
        if ntype == "augmented_assignment":
            right = node.child_by_field_name("right")
            if right is not None:
                self._scan(ctx, right, source, tainted, reported, hits)
            self._bind_augmented(ctx, node, source, tainted)
            return
        if ntype == "call":
            self._check_sink(ctx, node, source, tainted, reported, hits)
        for child in node.named_children:
            self._scan(ctx, child, source, tainted, reported, hits)

    # ---------------------------------------------------------------- 污点绑定

    def _bind_assignment(
        self,
        ctx: RuleContext,
        node: Any,
        source: bytes,
        tainted: dict[str, list[tuple[str, int, str]]],
    ) -> None:
        """普通 / 注解赋值绑定：右值含 source -> 记 source；含污染名 -> 传播；
        两者皆无 -> 同名洗白（同名覆盖只认最后一次）。"""
        right = node.child_by_field_name("right")
        if right is None:
            return
        line = node.start_point[0] + 1
        found = _find_source_in_expr(right, source)
        tname = _has_tainted_name(right, tainted, source)
        if found is not None:
            src_line, desc = found
            chain: list[tuple[str, int, str]] = []
            if tname is not None:  # 既有污点 × 新 source：并链保守（取最长链）
                chain.extend(tainted[tname])
            chain.append(("source", src_line, desc))  # source 事件已含绑定行号，不再记 prop
            for name in self._bind_targets(node.child_by_field_name("left"), source):
                tainted[name] = list(chain)
            return
        if tname is not None:
            chain = tainted[tname] + [("prop", line, _line_text(ctx, line))]
            for name in self._bind_targets(node.child_by_field_name("left"), source):
                tainted[name] = list(chain)
            return
        for name in self._bind_targets(node.child_by_field_name("left"), source):
            tainted.pop(name, None)  # 再赋非污点值 -> 洗白

    def _bind_augmented(
        self,
        ctx: RuleContext,
        node: Any,
        source: bytes,
        tainted: dict[str, list[tuple[str, int, str]]],
    ) -> None:
        """增广赋值（``x += rhs``）：右值含 source / 其他污染名 -> 并入污点链；
        右值无污点时 x 原有污点保持（x 出现在自身右值），未污染则维持。"""
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or left.type != "identifier" or right is None:
            return  # 属性 / 下标目标不传播（已知边界）
        name = ident_text(left, source)
        line = node.start_point[0] + 1
        found = _find_source_in_expr(right, source)
        if found is not None:
            chain = list(tainted.get(name, []))
            chain.append(("source", found[0], found[1]))  # source 事件已含本行号
            tainted[name] = chain
            return
        tname = _has_tainted_name(right, tainted, source)
        if tname is not None and tname != name:
            tainted[name] = tainted[tname] + [("prop", line, _line_text(ctx, line))]
        # tname == name（x += 常量）或无污点：链不变

    @staticmethod
    def _bind_targets(left: Any, source: bytes) -> list[str]:
        """提取赋值目标的名字（identifier / 元组解包 / 链式赋值）；
        属性 / 下标目标不是名字绑定，不入污染集（已知边界）。"""
        if left is None:
            return []
        ltype = left.type
        if ltype == "identifier":
            return [ident_text(left, source)]
        if ltype in ("pattern_list", "tuple", "star_pattern", "list_pattern"):
            names: list[str] = []
            for child in left.named_children:
                names.extend(PyTaintUnsafeSinkRule._bind_targets(child, source))
            return names
        if ltype == "assignment":  # 链式赋值 a = b = expr：左值嵌套
            return PyTaintUnsafeSinkRule._bind_targets(
                left.child_by_field_name("left"), source
            )
        return []

    # ---------------------------------------------------------------- sink 检查

    def _check_sink(
        self,
        ctx: RuleContext,
        node: Any,
        source: bytes,
        tainted: dict[str, list[tuple[str, int, str]]],
        reported: set[int],
        hits: list[RuleHit],
    ) -> None:
        """sink 调用检查：实参（SQL 类只看首个实参=SQL 语句本身）含污染名即命中；
        实参含 source 形态（零传播）亦直接命中；同一 sink 行多链去重为一条。"""
        func = node.child_by_field_name("function")
        info = _sink_info(func, source)
        if info is None:
            return
        label, kind = info
        args_node = node.child_by_field_name("arguments")
        if args_node is None:
            return
        args = list(args_node.named_children)
        if kind == "SQL":
            args = args[:1]  # 参数元组是参数化通道：参数化查询不误报（已知边界）
        line = node.start_point[0] + 1
        if line in reported:
            return
        trigger: list[tuple[str, int, str]] | None = None
        for arg in args:
            tname = _has_tainted_name(arg, tainted, source)
            if tname is not None:
                trigger = list(tainted[tname])
                break
            found = _find_source_in_expr(arg, source)
            if found is not None:  # source 表达式本身是 sink 实参：零传播直接命中
                trigger = [("source", found[0], found[1])]
                break
        if trigger is None:
            return
        reported.add(line)
        sink_line = line
        src_line, src_desc = trigger[0][1], trigger[0][2]
        n_prop = len(trigger) - 1
        chain = trigger + [("sink", sink_line, label)]
        flow = (
            f"经 {n_prop} 级赋值传播后" if n_prop > 0 else "未经中间赋值传播"
        )
        message = (
            f"第 {sink_line} 行危险汇点 `{label}` 接收到污点数据：外部输入"
            f"（{src_desc}，第 {src_line} 行）{flow}流入本调用，"
            f"攻击者可注入{_KIND_RISK[kind]}；{_KIND_ADVICE[kind]}"
        )
        hits.append(
            self.make_hit(
                ctx,
                sink_line,
                sink_line,
                message,
                meta={
                    "chain": self._render_chain(ctx.rel_path, chain),
                    "sink": label,
                    "source_desc": src_desc,
                    "source_line": src_line,
                    "levels": n_prop,
                },
            )
        )

    @staticmethod
    def _render_chain(rel_path: str, chain: list[tuple[str, int, str]]) -> list[str]:
        """污点链事件渲染为报告用字符串（`file:L行 source/prop/sink` 形态）。"""
        out: list[str] = []
        for kind, line, info in chain:
            if kind == "source":
                out.append(f"{rel_path}:L{line} source: {info}")
            elif kind == "prop":
                out.append(f"{rel_path}:L{line} {info}")
            else:
                out.append(f"{rel_path}:L{line} sink: {info}")
        return out


# ---------------------------------------------------------------- 注册


def build_py_taint_rules() -> list[Rule]:
    """构建污点传播规则实例（registry 接线由集成方统一完成）。"""
    return [
        PyTaintUnsafeSinkRule(),
    ]
