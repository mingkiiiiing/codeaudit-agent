"""PY-NONE-DEREF：同函数内"变量赋值 None → 后续解引用"（intra-file 空值流，P0-3）。

基于 ctx.tree（tree-sitter）做**函数作用域**的保守空值流判定——本规则为 AST-only：
tree=None（无解析器/降级/环境关闭）时返回空清单（新规则无既有命中可回退，宁缺勿滥）。

判定口径（保守优先，控误报）：
- 只追"同函数体内被显式赋值为 None 字面量的局部变量"（``x = None``）；
- 后续对该变量的属性（``x.a``）/下标（``x["k"]``）/调用（``x()``）解引用即报
  （运行时抛 AttributeError / TypeError）；
- 同名重赋值（含 for 循环目标 / with-as / except-as / walrus / 导入绑定 / del）
  之后视为非 None，不再追；
- 参数与 self/cls 不追（只有显式 ``= None`` 赋值才入跟踪集）；
- ``global`` / ``nonlocal`` 声明解除跟踪（来源不可见、可能被别处重绑）；但声明
  之后同函数内若仍有显式 ``= None`` 赋值，赋值后变量确为 None，照常恢复跟踪；
- 嵌套函数 / lambda / 类体视为独立作用域：外层分析跳过其子树（其自身作为
  function_definition 被单独分析，只追自己的赋值）；
- 线性源码序分析：分支内重赋值后（线性序在后）即视为非 None——宁可漏报
  不误报（分支汇合的路径敏感分析不在本规则口径内）。

check() 纯函数式：不修改 ctx、无 IO；severity/category 对齐既有 BUG 类规则
（bug/medium，同 PY-BARE-EXCEPT）。
"""

from __future__ import annotations

from typing import Any

from audit.detect.ast_util import ident_text, root_identifier, walk
from audit.detect.base import Rule, RuleContext
from audit.models import Category, RuleHit, Severity

__all__ = ["NoneDerefRule", "build_py_none_deref_rules"]

# 视为"解引用"的节点类型：属性 / 下标 / 调用
_DEREF_TYPES = ("attribute", "subscript", "call")
# 独立作用域节点：外层分析跳过其子树（其自身若为函数会被单独分析）
_SCOPE_TYPES = ("function_definition", "lambda", "class_definition")
# 解引用形态中文名（用于命中文案）
_KIND_LABELS = {"attribute": "属性访问", "subscript": "下标访问", "call": "函数调用"}


class NoneDerefRule(Rule):
    """同函数内变量先被赋 None、后被解引用（属性/下标/调用）——空指针缺陷。"""

    id = "PY-NONE-DEREF"
    category = Category.BUG
    severity = Severity.MEDIUM
    languages = ("python",)
    description = (
        "变量在同函数内被赋值为 None 后未经重赋值即被解引用（属性/下标/调用）："
        "运行时必然抛出 AttributeError/TypeError；请先判空（if x is not None）、"
        "给变量赋有效值，或重构掉 None 占位状态。"
    )

    bad_example = (
        "def render(user):\n"
        "    cache = None\n"
        "    cache.get(\"k\")  # cache 仍为 None，运行时 AttributeError\n"
    )
    good_example = (
        "def render(user):\n"
        "    cache = load_cache()  # 赋有效值后再使用\n"
        "    cache.get(\"k\")\n"
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
        hits.sort(key=lambda h: (h.line_start, h.meta.get("variable", "")))
        return hits

    # ---------------------------------------------------------------- 单函数分析

    def _check_function(
        self, ctx: RuleContext, fn: Any, source: bytes, hits: list[RuleHit]
    ) -> None:
        """对单个函数作用域做线性源码序空值流分析（嵌套作用域跳过）。"""
        body = fn.child_by_field_name("body")
        if body is None:
            return
        params = fn.child_by_field_name("parameters")
        param_names = (
            {ident_text(n, source) for n in walk(params) if n.type == "identifier"}
            if params is not None
            else set()
        )
        none_vars: dict[str, int] = {}  # 变量名 -> 赋 None 的行号
        reported: set[tuple[str, int]] = set()
        self._visit(ctx, body, source, param_names, none_vars, reported, hits)

    def _visit(
        self,
        ctx: RuleContext,
        node: Any,
        source: bytes,
        param_names: set[str],
        none_vars: dict[str, int],
        reported: set[tuple[str, int]],
        hits: list[RuleHit],
    ) -> None:
        """递归遍历函数体：先求值后绑定，维护 None 跟踪集并收集解引用命中。"""
        ntype = node.type
        if ntype in _SCOPE_TYPES:
            # 嵌套作用域：其中 nonlocal/global 可能重绑外层被追变量 → 外层停止跟踪
            for sub in walk(node):
                if sub.type in ("global_statement", "nonlocal_statement"):
                    for c in sub.children:
                        if c.type == "identifier":
                            none_vars.pop(ident_text(c, source), None)
            return
        if ntype == "assignment":
            right = node.child_by_field_name("right")
            if right is not None:
                self._visit(ctx, right, source, param_names, none_vars, reported, hits)
            is_none = right is not None and right.type == "none"
            for name in self._bind_targets(node.child_by_field_name("left"), source):
                if is_none and name not in param_names:
                    none_vars.setdefault(name, node.start_point[0] + 1)
                else:
                    none_vars.pop(name, None)
            return
        if ntype == "augmented_assignment":
            # 读改写：左目标若为属性/下标须查解引用；纯名字目标不改状态（None 参与
            # 运算运行时即抛错，但不在"属性/下标/调用"口径内，保守不报）
            self._check_deref_and_descend(
                ctx, node.child_by_field_name("left"), source, param_names,
                none_vars, reported, hits,
            )
            right = node.child_by_field_name("right")
            if right is not None:
                self._visit(ctx, right, source, param_names, none_vars, reported, hits)
            return
        if ntype == "named_expression":  # walrus: (x := expr)
            value = node.child_by_field_name("value")
            if value is not None:
                self._visit(ctx, value, source, param_names, none_vars, reported, hits)
            name_node = node.child_by_field_name("name")
            if name_node is not None and name_node.type == "identifier":
                none_vars.pop(ident_text(name_node, source), None)
            return
        if ntype == "for_statement":
            right = node.child_by_field_name("right")
            if right is not None:
                self._visit(ctx, right, source, param_names, none_vars, reported, hits)
            left = node.child_by_field_name("left")
            if left is not None:
                self._check_deref_and_descend(
                    ctx, left, source, param_names, none_vars, reported, hits
                )
                for name in self._bind_targets(left, source):
                    none_vars.pop(name, None)
            body = node.child_by_field_name("body")
            if body is not None:
                self._visit(ctx, body, source, param_names, none_vars, reported, hits)
            for c in node.children:  # for-else：else 体照常分析
                if c.type == "else_clause":
                    self._visit(ctx, c, source, param_names, none_vars, reported, hits)
            return
        if ntype == "with_item":
            # with expr as x：先求值 expr（查解引用，裸名字即 __enter__ 调用），
            # 随后别名绑定视为重赋值
            after_as = False
            for c in node.children:
                if c.type == "as":
                    after_as = True
                    continue
                if not after_as:
                    if c.type == "identifier":
                        self._report_if_tracked(
                            ctx, c, source, none_vars, reported, hits
                        )
                    else:
                        self._visit(ctx, c, source, param_names, none_vars, reported, hits)
                elif c.type == "identifier":
                    none_vars.pop(ident_text(c, source), None)
                elif c.type == "pattern_list":
                    for name in self._bind_targets(c, source):
                        none_vars.pop(name, None)
            return
        if ntype in ("global_statement", "nonlocal_statement"):
            for c in node.children:
                if c.type == "identifier":
                    none_vars.pop(ident_text(c, source), None)
            return
        if ntype == "delete_statement":
            for c in node.children:
                if c.type == "identifier":  # del x：删除绑定 → 之后状态未知
                    none_vars.pop(ident_text(c, source), None)
                else:
                    self._visit(ctx, c, source, param_names, none_vars, reported, hits)
            return
        if ntype == "except_clause":
            # except E as x：别名绑定视为重赋值
            after_as = False
            for c in node.children:
                if c.type == "as":
                    after_as = True
                    continue
                if after_as:
                    if c.type == "identifier":
                        none_vars.pop(ident_text(c, source), None)
                    after_as = False
                    continue
                self._visit(ctx, c, source, param_names, none_vars, reported, hits)
            return
        if ntype in ("import_statement", "import_from_statement"):
            for sub in walk(node):
                if sub.type == "alias":
                    name_node = sub.child_by_field_name("alias")
                    if name_node is None:
                        name_node = sub.child_by_field_name("name")
                    if name_node is not None:
                        none_vars.pop(ident_text(name_node, source), None)
            return
        if ntype in _DEREF_TYPES:
            self._report_if_tracked(ctx, node, source, none_vars, reported, hits)
        for child in node.children:
            self._visit(ctx, child, source, param_names, none_vars, reported, hits)

    def _check_deref_and_descend(
        self,
        ctx: RuleContext,
        node: Any,
        source: bytes,
        param_names: set[str],
        none_vars: dict[str, int],
        reported: set[tuple[str, int]],
        hits: list[RuleHit],
    ) -> None:
        """对表达式节点查解引用并继续下钻（del/for 目标等位置的属性/下标访问）。"""
        if node is None:
            return
        if node.type in _DEREF_TYPES:
            self._report_if_tracked(ctx, node, source, none_vars, reported, hits)
        for child in node.children:
            self._visit(ctx, child, source, param_names, none_vars, reported, hits)

    def _report_if_tracked(
        self,
        ctx: RuleContext,
        node: Any,
        source: bytes,
        none_vars: dict[str, int],
        reported: set[tuple[str, int]],
        hits: list[RuleHit],
    ) -> None:
        """节点为对被追 None 变量的解引用时产出命中（每 (变量, 行) 只报一条）。"""
        base = root_identifier(node)
        if base is None:
            return
        name = ident_text(base, source)
        def_line = none_vars.get(name)
        if def_line is None:
            return
        lineno = node.start_point[0] + 1
        if (name, lineno) in reported:
            return
        reported.add((name, lineno))
        kind = _KIND_LABELS.get(node.type, "解引用")
        hits.append(
            self.make_hit(
                ctx,
                lineno,
                lineno,
                f"第 {lineno} 行对变量 `{name}` 做{kind}：该变量在第 {def_line} 行"
                "被赋值为 None 且此后未经重赋值，运行时必然抛出 AttributeError/TypeError；"
                f"请先判空（`if {name} is not None`）、给 `{name}` 赋有效值后再使用。",
                meta={"variable": name, "none_line": def_line, "kind": node.type},
            )
        )

    @staticmethod
    def _bind_targets(left: Any, source: bytes) -> list[str]:
        """提取赋值/循环目标的绑定名（identifier / 元组解包 / 链式赋值）。"""
        if left is None:
            return []
        if left.type == "identifier":
            return [ident_text(left, source)]
        if left.type in ("pattern_list", "tuple", "star_pattern"):
            names: list[str] = []
            for c in left.children:
                names.extend(NoneDerefRule._bind_targets(c, source))
            return names
        if left.type == "assignment":  # 链式赋值 x = y = …：左侧嵌套
            return NoneDerefRule._bind_targets(left.child_by_field_name("left"), source)
        return []  # 属性/下标目标不是名字绑定（解引用已另行检查）


# ---------------------------------------------------------------- 注册


def build_py_none_deref_rules() -> list[Rule]:
    """构建 P0-3 空值流规则实例（registry 接线由集成方统一完成）。"""
    return [
        NoneDerefRule(),
    ]
