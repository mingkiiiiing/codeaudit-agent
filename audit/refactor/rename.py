"""safe-rename：确定性重命名原语（W27-C，纯模块 MVP，零 LLM token）。

定位为审计报告 §1.3「确定性重构原型缺失」的最短路径：以 audit.indexer 的
tree-sitter 底座（parsers.parse_source + py_extract.extract_python 的
Symbol 口径）实现 python 单语言的符号重命名 plan / apply 两段式 API。
本模块不落盘除非显式 dry_run=False；不做任何 LLM 调用。

API
---
- ``plan_rename(source_path, old_name, new_name, *, language="python") -> RenamePlan``
  定位符号定义与全部引用，产出涉及文件/行号/替换点清单与每文件 unified diff
  预览（difflib），**不落盘**。source_path 可为单文件或目录（目录时递归扫
  *.py，跳过 DEFAULT_IGNORE_DIRS 与隐藏目录）。计划阶段即拒绝的情形（写入
  plan.errors，patches 为空）：
    * new_name / old_name 非法（须 ``str.isidentifier()`` 且非关键字）；
    * old_name == new_name；
    * language != "python"（MVP 不做其他语言）；
    * 任一被扫描文件非 UTF-8 或 tree-sitter 解析失败（宁拒不改）；
    * 找不到 old_name 定义点；
    * 多定义点（n>1）时逐引用点做作用域归属判定（W29 P0-2「作用域可分则
      分，分不清则拒」+ W30 类型推断三项放宽，见「MVP 边界」段）：任一引
      用点归属不明（如函数体内无直接构造赋值可判定的 ``obj.X`` 形态、
      ``self.X``/``super().X`` 所在类基类链均无同名定义、裸名无模块级定义）
      则整体拒绝并在 errors 给出 file:line 明细（宁拒不改）；全部可归属才
      放行，plan.definitions 列出全部定义点，替换点照常全量替换。
- ``apply_rename(plan, *, dry_run=True) -> RenameResult``
  dry_run=True 只返回 diff 不写文件；False 才写。写前逐文件复检：
    1. 落盘前内容与计划时一致（防并发修改，语义同 audit.fix.applyer）；
    2. 替换后内容必须 ``ast.parse`` 成功；
  任一文件复检失败则**全部不写**（all-or-nothing，同 apply-to-source 语义）
  并在 RenameResult.errors 返回逐文件失败原因。

为什么走 tree-sitter identifier 节点区间、绝不做子串/正则文本替换
----------------------------------------------------------------
1. 子串安全：正则/子串替换 ``user`` 会误伤 ``username``、``user_id``；
   identifier 节点的字节区间精确到 token 边界，天然不会越界。
2. 字符串/注释安全：tree-sitter 语法里字符串字面量解析为 ``string`` 节点
  （内容为 ``string_content``）、注释为 ``comment`` 节点，二者的子树中
   **不存在 identifier 节点**——只遍历 identifier 节点替换，字符串/注释里
   的同名文本物理上不可能被改到（含 docstring、f-string 中的字面段）。
   f-string 插值段 ``f"{name}"`` 中的 identifier 是真实表达式节点，照常
   参与重命名（这是正确语义）。

MVP 边界（明示不做）
--------------------
- 动态引用：getattr/字符串反射/``globals()`` 改写等不可静态见的名字不动；
- 同名不同作用域（W29 P0-2 作用域增强口径「作用域可分则分，分不清则拒」
  + W30 类型推断三项放宽，安全边界不变）：
  - ``self.X``：归属所在类（词法最近 enclosing class，含跳过嵌套函数体）的
    同名 method 定义点；无则沿该类 bases **递归向上**（W30 放宽③：基类链
    传递闭包，不再限一层；仅限基类 class 定义在本次扫描文件集内可见——
    跨文件 import 的基类语义不展开、同名类多定义、检测到继承环均按不可
    解析处理），任一层基类名能对上某定义点的类段（qualified_name 类段字
    面比对）即归属（继承场景）；
  - ``super().X``（W30 放宽②：仅认 function 为裸名 super 的**零参**调用）：
    沿调用点所在类的基类链递归向上（口径同上），任一层命中 X 定义点 →
    归属；所在类无 bases / 链不可解析 / 链上无命中 → 仍歧义（W29 时一律
    歧义，W30 起放宽；``super(C, self).X`` 带参形式不认，仍按通用歧义处理）；
  - ``obj.X``（接收者为裸名、非 self / super 调用）：W30 放宽①——在该
    obj 引用点**所在函数体内**向上回溯最近一次 ``obj = ClassName(...)``
    直接类名构造赋值（仅 Name 左值 + 调用 function 字段为裸类名 ClassName
    的形态；引用点不在任何函数体内——模块级 / 类体直接语句——不做回溯），
    ClassName 与某定义点所属类名（qualified_name 类段）字面一致 → 归属；
    构造赋值不存在 / 引用点先于全部构造赋值 / 类名无匹配定义点 → 仍歧义；
  - 裸名 X（含 ``from m import X`` 导入项、装饰器引用、赋值右值、
    ``X.foo`` 中处于 object 位的 X 本体）：归属模块级 function/class/
    constant 定义点（qualified_name 无类段）；定义点集合中无模块级定义
    即歧义——类体内/方法体内的裸名同此规则；与 old_name 同名的**参数名
    /局部变量绑定**不区分绑定与引用，同样按裸名口径处理（同名参数撞多
    定义点方法名时保守误拒，宁拒不改）；
  - def/class 的名字本体（定义点自身）必然可归属；
  - 任一歧义点即**整体拒绝**（维持「宁拒不改」安全边界）；全部可归属则
    全量替换（替换文本相同，无需分组拼接——归属判定的意义在于确认所有
    引用都确定指向这些定义点之一，替换因此安全）；嵌套类的 bases 只按
    尾段字面比对，不追嵌套类路径；
  - W30 类型推断**仍不做**的形态（如实申报边界）：别名构造（``Alias =
    ClassName`` 后 ``obj = Alias()``）、工厂函数 / 方法链返回值推断
    （``obj = make_x()`` / ``obj = a.b()``）、跨文件的构造赋值回溯与模块级
    构造赋值回溯（W29 的模块级 ``obj = C(); obj.X()`` 拒绝语料语义因此
    保持不变）、``super(C, self).X`` 带参形式、构造赋值的控制流执行性
    分析（按文本位置回溯，不模拟 if/for 分支可达性）、与目标同名的参数/
    局部变量与引用的绑定区分；
- import 重导出 / ``__all__`` 联动：``__all__ = ["old"]`` 是字符串字面量，
  本模块不改（见上，字符串不动）；
- import 语句中的**模块路径**（``import a.b.c`` 的 a/b/c、``from a.b``
  的模块名）不参与替换——改路径等价于改文件名，文件重命名不在本 MVP；
  ``from m import old`` 的导入项属符号引用，照常替换；
- 非 python 语言：language 参数仅支持 "python"；
- 替换点列（col）为 tree-sitter 字节列（0-based），非字符列。
"""

from __future__ import annotations

import ast
import difflib
import keyword
from dataclasses import dataclass, field
from pathlib import Path

from audit.indexer.parsers import node_text, parse_source
from audit.indexer.py_extract import extract_python
from audit.workspace import DEFAULT_IGNORE_DIRS

__all__ = [
    "DefSite",
    "FilePatch",
    "RenamePlan",
    "RenameResult",
    "ReplacePoint",
    "apply_rename",
    "plan_rename",
]


# ---------------------------------------------------------------- 数据结构


@dataclass
class ReplacePoint:
    """单个替换点（一个 identifier 节点）。"""

    file: str  # 相对 base_dir 的 posix 路径
    line: int  # 1-based 起始行
    col: int  # 0-based 字节列（tree-sitter start_point）
    old_text: str
    new_text: str


@dataclass
class DefSite:
    """old_name 的定义点（W29 P0-2：可有多于 1 个，须全部通过作用域归属判定）。"""

    file: str
    qualified_name: str  # py_extract 口径：方法为 Class.method
    kind: str  # function | method | class | constant
    line: int


@dataclass
class FilePatch:
    """单文件补丁：替换后字节串 + 替换点清单 + unified diff 预览。"""

    file: str  # 相对 base_dir 的 posix 路径
    old_bytes: bytes = b""
    new_bytes: bytes = b""
    replace_points: list[ReplacePoint] = field(default_factory=list)
    unified_diff: str = ""

    @property
    def old_source(self) -> str:
        return self.old_bytes.decode("utf-8")

    @property
    def new_source(self) -> str:
        return self.new_bytes.decode("utf-8")


@dataclass
class RenamePlan:
    """重命名计划（不可变产物，apply 只读它）。"""

    source_root: str = ""  # 调用方传入的扫描范围（原样字符串）
    base_dir: str = ""  # 解析 patch.file 的基准目录（单文件时为其父目录）
    language: str = "python"
    old_name: str = ""
    new_name: str = ""
    definitions: list[DefSite] = field(default_factory=list)
    patches: list[FilePatch] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def total_replace_points(self) -> int:
        return sum(len(p.replace_points) for p in self.patches)


@dataclass
class RenameResult:
    """apply 产物。applied=False 且 ok=True 即 dry_run；errors 非空即拒绝。"""

    ok: bool
    applied: bool
    written_files: list[str] = field(default_factory=list)
    diffs: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 内部工具


@dataclass
class _PyUnit:
    """单文件解析产物（plan 内部传递）。"""

    path: Path
    rel: str
    data: bytes
    tree: object  # tree_sitter.Tree


def _iter_nodes(root):
    """深度优先遍历 tree-sitter 节点。"""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def _is_module_path_identifier(node) -> bool:
    """identifier 是否属于 import 语句中的模块路径（这些不做替换）。

    覆盖：``import a.b.c`` 的路径组件、``import a.b as x`` 的被别名路径、
    ``from a.b import ...`` 的模块名（含相对导入 ``from .m import``）。
    ``from m import old`` 导入列表里的名字不属模块路径，照常替换。
    """
    cur = node.parent
    while cur is not None:
        if cur.type in ("dotted_name", "relative_import"):
            gp = cur.parent
            if gp is None:
                return False
            if gp.type == "import_statement":
                return True
            if gp.type == "import_from_statement":
                module_name = gp.child_by_field_name("module_name")
                return module_name is not None and cur.id == module_name.id
            if gp.type == "aliased_import":
                # import a.b as x 的被别名部分是模块路径；
                # from m import old as x 的被别名部分是符号名，不跳过。
                name = gp.child_by_field_name("name")
                in_import_stmt = gp.parent is not None and gp.parent.type == "import_statement"
                return bool(name is not None and cur.id == name.id and in_import_stmt)
            return False
        cur = cur.parent
    return False


def _hit_identifiers(unit: _PyUnit, old_name: str) -> list:
    """unit 内文本==old_name 且非 import 模块路径的 identifier 节点（遍历序）。

    替换点收集与多定义点归属判定共用；调用方需要字节序时自行按
    start_byte 排序（与既有替换核心一致）。
    """
    return [
        n
        for n in _iter_nodes(unit.tree.root_node)
        if n.type == "identifier" and node_text(n, unit.data) == old_name and not _is_module_path_identifier(n)
    ]


def _is_definition_name(node) -> bool:
    """identifier 是否为 def/class 的名字字段（定义点本体，必然可归属）。"""
    parent = node.parent
    if parent is None or parent.type not in ("function_definition", "class_definition"):
        return False
    name = parent.child_by_field_name("name")
    return name is not None and name.id == node.id


def _enclosing_class(node):
    """向上回溯最近 enclosing class_definition（Python 的 self 绑定即词法最近类）。

    中间隔着嵌套 function_definition 不影响（内层函数里的 self 仍绑定到
    最近的外层类）；嵌套类场景自然取最近的内层类。
    """
    cur = node.parent
    while cur is not None:
        if cur.type == "class_definition":
            return cur
        cur = cur.parent
    return None


def _class_part(definition: DefSite) -> str:
    """定义点限定名的类段：``Class.method`` → ``Class``；模块级符号 → ``""``。"""
    return definition.qualified_name.rpartition(".")[0]


def _base_class_names(class_node, data: bytes) -> list[str]:
    """class_definition 的 bases 名字：identifier 直取；``mod.Base`` 取尾段。"""
    supers = class_node.child_by_field_name("superclasses")
    names: list[str] = []
    if supers is None:
        return names
    for child in supers.children:
        if child.type == "identifier":
            names.append(node_text(child, data))
        elif child.type == "attribute":
            tail = child.child_by_field_name("attribute")
            if tail is not None:
                names.append(node_text(tail, data))
    return names


def _enclosing_function(node):
    """向上回溯最近 enclosing function_definition（含方法；嵌套时取最内层）。"""
    cur = node.parent
    while cur is not None:
        if cur.type == "function_definition":
            return cur
        cur = cur.parent
    return None


def _collect_class_definitions(units: list[_PyUnit]) -> dict[str, tuple[_PyUnit, object]]:
    """本次扫描文件集内可见的 class 定义映射：类名 → (unit, class_definition)。

    供基类链传递闭包解析（W30 放宽③）使用；同名类多定义时该类名从映射中
    剔除、按不可解析处理（无法确定 bases 指向哪个同名类，宁拒不改）。
    """
    mapping: dict[str, tuple[_PyUnit, object]] = {}
    duplicated: set[str] = set()
    for unit in units:
        for n in _iter_nodes(unit.tree.root_node):
            if n.type != "class_definition":
                continue
            name_node = n.child_by_field_name("name")
            if name_node is None:
                continue
            name = node_text(name_node, unit.data)
            if name in mapping:
                duplicated.add(name)
                continue
            mapping[name] = (unit, n)
    for name in duplicated:
        mapping.pop(name, None)
    return mapping


def _chain_hits_definition(
    start_names: list[str], class_defs: dict[str, tuple[_PyUnit, object]], class_parts: set[str]
) -> bool:
    """沿基类链递归向上（W30 传递闭包），任一层命中 old_name 定义点类段即 True。

    - 基类定义不在扫描文件集内（``mod.Base`` 的 mod 语义不展开 / 内建基类）
      → 该链按不可解析处理，停止且不命中；
    - 检测到继承环（重复访问同一类名）→ 立即返回 False（整体按不可解析
      处理，宁拒不改）；visited 集合同时保证有限步终止、不挂死。
    """
    visited: set[str] = set()
    stack = list(start_names)
    while stack:
        name = stack.pop()
        if name in visited:
            return False
        visited.add(name)
        if name in class_parts:
            return True
        entry = class_defs.get(name)
        if entry is None:
            continue
        unit, cls_node = entry
        stack.extend(_base_class_names(cls_node, unit.data))
    return False


def _is_zero_arg_super_call(obj, unit: _PyUnit) -> bool:
    """obj 是否 ``super().X`` 的零参裸 ``super()`` 调用形态（W30 放宽②口径）。

    ``super(C, self).X`` 带参形式不认（返回 False，走通用歧义处理）。
    """
    if obj is None or obj.type != "call":
        return False
    func = obj.child_by_field_name("function")
    if func is None or func.type != "identifier" or node_text(func, unit.data) != "super":
        return False
    args = obj.child_by_field_name("arguments")
    return args is not None and not args.named_children


def _nearest_direct_construction_class(obj_name: str, ref_node, unit: _PyUnit) -> str | None:
    """obj 引用点所在函数体内、引用点之前最近一次 ``obj = ClassName(...)`` 的裸类名。

    W30 放宽①口径：仅认 Name 左值 + 调用 function 字段为裸类名（identifier）
    的形态，别名 / 工厂函数 / 方法链 / 解包赋值一概不算；赋值须与引用点同属
    一个函数体（排除更深嵌套函数体内的赋值，不做跨函数闭包回溯）。引用点
    不在任何函数体内（模块级 / 类体直接语句不做回溯）、函数体内无匹配赋值、
    或引用点先于全部匹配赋值 → None（调用方按歧义处理）。
    """
    fn = _enclosing_function(ref_node)
    if fn is None:
        return None
    body = fn.child_by_field_name("body")
    best = None
    if body is not None:
        for n in _iter_nodes(body):
            if n.type != "assignment" or n.start_byte >= ref_node.start_byte:
                continue
            assign_fn = _enclosing_function(n)
            if assign_fn is None or assign_fn.id != fn.id:  # 更深嵌套函数体内的赋值不参与
                continue
            left = n.child_by_field_name("left")
            right = n.child_by_field_name("right")
            if left is None or left.type != "identifier" or node_text(left, unit.data) != obj_name:
                continue
            if right is None or right.type != "call":
                continue
            func = right.child_by_field_name("function")
            if func is None or func.type != "identifier":
                continue
            if best is None or n.start_byte > best.start_byte:
                best = n
    if best is None:
        return None
    return node_text(best.child_by_field_name("right").child_by_field_name("function"), unit.data)


def _bare_name_verdict(definitions: list[DefSite]) -> tuple[bool, str]:
    """裸名引用的归属判定：存在模块级 function/class/constant 定义点即可归属。"""
    if any(
        definition.kind in ("function", "class", "constant") and "." not in definition.qualified_name
        for definition in definitions
    ):
        return True, ""
    return False, "裸名引用但定义点集合中无模块级 function/class/constant 定义点"


def _self_attr_verdict(
    node, unit: _PyUnit, definitions: list[DefSite], class_parts: set[str],
    class_defs: dict[str, tuple[_PyUnit, object]], old_name: str,
) -> tuple[bool, str]:
    """``self.X`` 形态的归属判定：所在类同名方法 → 基类链（W30 传递闭包）→ 否则歧义。"""
    cls = _enclosing_class(node)
    if cls is None:
        return False, "self.X 形态但不在任何类作用域内"
    name_node = cls.child_by_field_name("name")
    cls_name = node_text(name_node, unit.data) if name_node is not None else ""
    # ① 所在类（同文件词法类）有同名 method 定义点 → 归属该类
    if any(
        definition.kind == "method" and definition.file == unit.rel and _class_part(definition) == cls_name
        for definition in definitions
    ):
        return True, ""
    # ② 沿基类链递归向上（W30 放宽③：不再限一层；环/不可见定义按不可解析
    #    处理），任一层基类名能对上某定义点的类段（method/class）→ 归属继承链
    if _chain_hits_definition(_base_class_names(cls, unit.data), class_defs, class_parts):
        return True, ""
    return False, f"self.{old_name} 所在类 {cls_name} 及其基类均无同名定义点"


def _super_attr_verdict(
    node, unit: _PyUnit, class_parts: set[str], class_defs: dict[str, tuple[_PyUnit, object]], old_name: str
) -> tuple[bool, str]:
    """``super().X`` 形态的归属判定（W30 放宽②）：沿所在类基类链递归解析。

    基类链任一层命中 X 定义点 → 归属；所在类无 bases / 链不可解析 / 到顶
    无命中 → 仍歧义（沿用「宁拒不改」）。
    """
    cls = _enclosing_class(node)
    if cls is None:
        return False, "super().X 形态但不在任何类作用域内"
    name_node = cls.child_by_field_name("name")
    cls_name = node_text(name_node, unit.data) if name_node is not None else ""
    if _chain_hits_definition(_base_class_names(cls, unit.data), class_defs, class_parts):
        return True, ""
    return False, f"super.{old_name} 所在类 {cls_name} 的基类链均无同名定义点或不可解析"


def _classify_multi_def_reference(
    node, unit: _PyUnit, definitions: list[DefSite], class_parts: set[str],
    class_defs: dict[str, tuple[_PyUnit, object]], old_name: str,
) -> tuple[bool, str]:
    """多定义点场景下单引用点的作用域归属判定（「作用域可分则分，分不清则拒」）。

    返回 (True, "")：该引用点确定指向定义点集合之一，全量替换安全；
    返回 (False, 原因)：归属不明（歧义点），整体拒绝。
    """
    if _is_definition_name(node):  # def/class 名字本体：定义点自身，必然可归属
        return True, ""
    parent = node.parent
    if parent is not None and parent.type == "attribute":
        attr_field = parent.child_by_field_name("attribute")
        obj = parent.child_by_field_name("object")
        if attr_field is None or attr_field.id != node.id:
            return _bare_name_verdict(definitions)  # ``X.foo`` 中 object 位的 X 本体是裸名
        if obj is not None and obj.type == "identifier" and node_text(obj, unit.data) == "self":
            return _self_attr_verdict(node, unit, definitions, class_parts, class_defs, old_name)
        if _is_zero_arg_super_call(obj, unit):  # W30 放宽②：super().X 沿基类链解析
            return _super_attr_verdict(node, unit, class_parts, class_defs, old_name)
        if obj is not None and obj.type == "identifier":
            # W30 放宽①：obj 为裸名时尝试所在函数体内 ``obj = ClassName(...)``
            # 直接构造赋值回溯；构造不存在 / 类名无匹配定义点 → 落到通用歧义
            cls_name = _nearest_direct_construction_class(node_text(obj, unit.data), node, unit)
            if cls_name is not None and cls_name in class_parts:
                return True, ""
        obj_text = node_text(obj, unit.data) if obj is not None else "?"
        # obj.X / cls.X / mod.X：接收者类型不可静态判定（含工厂/别名构造形态）
        return False, f"{obj_text}.{old_name} 形态（接收者类型不可静态判定）"
    return _bare_name_verdict(definitions)


def _find_scope_ambiguous(units: list[_PyUnit], definitions: list[DefSite], old_name: str) -> list[str]:
    """多定义点场景：逐引用点做归属判定，返回歧义点明细（"file:line(原因)"）。"""
    class_parts = {
        _class_part(definition) for definition in definitions if definition.kind in ("method", "class")
    }
    class_defs = _collect_class_definitions(units)  # W30：基类链传递闭包解析用
    details: list[str] = []
    for unit in units:
        for node in _hit_identifiers(unit, old_name):
            attributable, reason = _classify_multi_def_reference(
                node, unit, definitions, class_parts, class_defs, old_name
            )
            if not attributable:
                details.append(f"{unit.rel}:{node.start_point[0] + 1}({reason})")
    return details


def _collect_python_files(root: Path) -> list[Path]:
    """收集待扫描 .py 文件：单文件原样返回；目录递归并跳过忽略/隐藏目录。"""
    if root.is_file():
        return [root]
    found: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        dir_parts = path.relative_to(root).parts[:-1]
        if any(p in DEFAULT_IGNORE_DIRS or p.startswith(".") for p in dir_parts):
            continue
        found.append(path)
    return found


def _validate_names(old_name: str, new_name: str, errors: list[str]) -> None:
    """计划阶段名字合法性校验（直接向 errors 追加）。"""
    if not isinstance(old_name, str) or not old_name.isidentifier() or keyword.iskeyword(old_name):
        errors.append(f"old_name 不是合法 python 标识符: {old_name!r}")
    if not isinstance(new_name, str) or not new_name.isidentifier() or keyword.iskeyword(new_name):
        errors.append(f"new_name 不是合法 python 标识符: {new_name!r}（计划阶段即拒绝）")
    if old_name == new_name:
        errors.append(f"old_name 与 new_name 相同（{old_name!r}）：无操作重命名拒绝")


# ---------------------------------------------------------------- 公开 API


def plan_rename(source_path: str | Path, old_name: str, new_name: str, *, language: str = "python") -> RenamePlan:
    """定位符号定义与全部引用，产出替换点清单 + 每文件 unified diff 预览（不落盘）。

    见模块 docstring：API 契约、节点区间替换理由、MVP 边界。
    """
    errors: list[str] = []
    root = Path(source_path)
    plan = RenamePlan(source_root=str(root), base_dir=str(root.parent if root.is_file() else root), language=language,
                      old_name=old_name, new_name=new_name)

    if language != "python":
        errors.append(f"language={language!r} 暂不支持：safe-rename MVP 仅支持 python")
    _validate_names(old_name, new_name, errors)
    if not root.exists():
        errors.append(f"source_path 不存在: {root}")
    if errors:
        plan.errors = errors
        return plan

    # 1) 收集并解析文件（非 UTF-8 / 解析失败 → 宁拒不改）
    units: list[_PyUnit] = []
    for path in _collect_python_files(root):
        rel = path.relative_to(Path(plan.base_dir)).as_posix()
        try:
            data = path.read_bytes()
            data.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{rel}: 非 UTF-8 或不可读，拒绝纳入重命名（宁拒不改）: {exc}")
            continue
        tree, parsed_ok = parse_source("python", data)
        if tree is None or not parsed_ok:
            errors.append(f"{rel}: tree-sitter 解析失败，拒绝重命名（宁拒不改）")
            continue
        units.append(_PyUnit(path=path, rel=rel, data=data, tree=tree))

    # 2) 定义点定位：复用 audit.indexer.py_extract 的 Symbol 口径
    #    （函数/方法/类/模块级常量；方法限定名 Class.method，取简名比对）。
    definitions: list[DefSite] = []
    for unit in units:
        for sym in extract_python(unit.data, unit.rel).symbols:
            if sym.name.rsplit(".", 1)[-1] == old_name:
                definitions.append(
                    DefSite(file=unit.rel, qualified_name=sym.name, kind=sym.kind, line=sym.line_start)
                )
    if not definitions:
        errors.append(
            f"未找到 {old_name!r} 的定义点（MVP 符号口径：函数/方法/类/模块级常量，"
            "纯局部变量不支持）"
        )
    plan.definitions = definitions
    if len(definitions) > 1:
        # W29 P0-2 作用域增强：多定义点不再一刀切拒绝，逐引用点做上下文归属
        # 判定（「作用域可分则分，分不清则拒」）；任一歧义点即整体拒绝并给
        # 出位置明细，全部可归属则放行走常规替换（全量替换，文本一致安全）。
        ambiguous = _find_scope_ambiguous(units, definitions, old_name)
        if ambiguous:
            where = ", ".join(f"{d.file}:{d.qualified_name}({d.kind})" for d in definitions)
            errors.append(
                f"检测到 {len(definitions)} 个同名定义点({where})，"
                f"其中 {len(ambiguous)} 个引用点作用域归属不明，宁拒不改：{'; '.join(ambiguous)}"
            )
    if errors:  # 定义点校验失败：计划阶段即拒绝，不产出任何补丁
        plan.errors = errors
        return plan

    # 3) 替换点收集：全部 identifier 节点按字节区间替换（跳过模块路径组件；
    #    字符串/注释子树中不存在 identifier 节点，天然不受伤）。
    new_name_bytes = new_name.encode("utf-8")
    for unit in units:
        hits = _hit_identifiers(unit, old_name)
        if not hits:
            continue
        points = [
            ReplacePoint(
                file=unit.rel,
                line=n.start_point[0] + 1,
                col=n.start_point[1],
                old_text=old_name,
                new_text=new_name,
            )
            for n in sorted(hits, key=lambda n: n.start_byte)
        ]
        new_data = unit.data
        for n in sorted(hits, key=lambda n: n.start_byte, reverse=True):  # 倒序拼接免位移
            new_data = new_data[: n.start_byte] + new_name_bytes + new_data[n.end_byte :]
        diff = "".join(
            difflib.unified_diff(
                unit.data.decode("utf-8").splitlines(keepends=True),
                new_data.decode("utf-8").splitlines(keepends=True),
                fromfile=f"a/{unit.rel}",
                tofile=f"b/{unit.rel}",
            )
        )
        plan.patches.append(
            FilePatch(
                file=unit.rel,
                old_bytes=unit.data,
                new_bytes=new_data,
                replace_points=points,
                unified_diff=diff,
            )
        )

    if not errors and not plan.patches:
        errors.append(f"定义存在但未找到任何替换点（异常状态，拒绝）: {old_name!r}")
    plan.errors = errors
    return plan


def apply_rename(plan: RenamePlan, *, dry_run: bool = True) -> RenameResult:
    """按计划落盘；dry_run=True 只返回 diff。写前逐文件复检，all-or-nothing。

    复检项：①落盘前内容与计划时一致（防并发修改）；②替换后内容 ast.parse
    成功。任一文件失败 → 全部不写，errors 返回逐文件原因（语义同
    audit.fix.applyer 的 apply-to-source all-or-nothing）。
    """
    diffs = {p.file: p.unified_diff for p in plan.patches}
    if plan.errors:
        return RenameResult(ok=False, applied=False, written_files=[], diffs=diffs, errors=list(plan.errors))

    base = Path(plan.base_dir)
    failures: list[str] = []
    staged: list[tuple[Path, bytes, str]] = []
    for patch in plan.patches:
        target = base / patch.file
        try:
            current = target.read_bytes()
        except OSError as exc:
            failures.append(f"{patch.file}: 落盘前读取失败: {exc}")
            continue
        if current != patch.old_bytes:
            failures.append(f"{patch.file}: 落盘前内容与计划时不一致（疑似并发修改），拒绝写入")
            continue
        try:
            ast.parse(patch.new_bytes)
        except (SyntaxError, ValueError) as exc:
            failures.append(f"{patch.file}: 替换后 AST 复检失败（ast.parse）: {exc}")
            continue
        staged.append((target, patch.new_bytes, patch.file))

    if failures:  # all-or-nothing：任一复检失败，全部不写
        return RenameResult(ok=False, applied=False, written_files=[], diffs=diffs, errors=failures)
    if dry_run:
        return RenameResult(ok=True, applied=False, written_files=[], diffs=diffs, errors=[])

    for target, payload, rel in staged:
        try:
            target.write_bytes(payload)
        except OSError as exc:
            failures.append(f"{rel}: 写入失败: {exc}")
    if failures:
        # 复检阶段已保证内容级 all-or-nothing；写入期 OSError 属环境故障，逐个如实上报。
        return RenameResult(ok=False, applied=False, written_files=[], diffs=diffs, errors=failures)
    return RenameResult(
        ok=True,
        applied=True,
        written_files=[rel for _, _, rel in staged],
        diffs=diffs,
        errors=[],
    )
