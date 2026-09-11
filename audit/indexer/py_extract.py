"""Python 源码提取：Symbol / ImportRecord / 调用点。

基于 tree-sitter-python 语法树：
  - 函数/方法/类/模块级常量 → Symbol（方法名用 Class.method 限定）
  - import x / from a.b import c as d / 相对导入 → ImportRecord
  - 调用点（call 节点）→ RawCall（caller 为最内层函数限定名，模块级为 ""）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tree_sitter import Node

from audit.indexer.parsers import end_line_1b, node_text, parse_source
from audit.models import ImportRecord, Symbol

_CONST_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# 允许识别为"常量"的右值节点类型（排除函数调用等动态右值）
_CONST_VALUE_TYPES = {
    "string", "concatenated_string", "integer", "float", "true", "false",
    "none", "list", "dictionary", "set", "tuple", "unary_operator",
    "binary_operator", "boolean", "comparison_operator",
}


@dataclass
class RawCall:
    """文件内一次调用点（尚未跨文件解析）。"""

    caller_symbol: str  # 最内层函数/方法限定名；模块级为 ""
    callee_name: str  # 调用目标原文（identifier 或点链，如 conn.execute）
    line: int  # 调用行（1-based）


@dataclass
class FileIndex:
    """单文件提取产物。"""

    language: str = "python"
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[ImportRecord] = field(default_factory=list)
    calls: list[RawCall] = field(default_factory=list)
    parsed_ok: bool = True


def extract_python(source: bytes, file: str) -> FileIndex:
    """解析 Python 源码并提取符号/导入/调用点。"""
    tree, ok = parse_source("python", source)
    result = FileIndex(parsed_ok=ok)
    if tree is None:
        return result

    def line_of(node: Node) -> int:
        return node.start_point[0] + 1

    def def_signature(node: Node, keyword: str) -> tuple[int, str]:
        """从 def/class 关键字到冒号的签名文本 + 起始行（跳过装饰器）。"""
        kw = next((c for c in node.children if c.type == keyword), None)
        colon = next((c for c in node.children if c.type == ":"), None)
        start = kw if kw is not None else node
        end = colon if colon is not None else node
        sig_text = source[start.start_byte : end.end_byte].decode("utf-8", errors="replace")
        return start.start_point[0] + 1, " ".join(sig_text.split())

    def visit(node: Node, cls_stack: list[str], fn_stack: list[str]) -> None:
        ntype = node.type

        if ntype == "decorated_definition":
            for child in node.children:
                visit(child, cls_stack, fn_stack)
            return

        if ntype == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                for child in node.children:
                    visit(child, cls_stack, fn_stack)
                return
            short = node_text(name_node, source)
            is_method = bool(cls_stack) and not fn_stack
            kind = "method" if is_method else "function"
            qualified = ".".join([*cls_stack, *fn_stack, short])
            line_start, signature = def_signature(node, "def")
            result.symbols.append(
                Symbol(
                    id=f"{file}::{qualified}",
                    file=file,
                    kind=kind,
                    name=qualified,
                    line_start=line_start,
                    line_end=end_line_1b(node),
                    signature=signature,
                )
            )
            for child in node.children:
                visit(child, cls_stack, [*(fn_stack if is_method else fn_stack), short])
            return

        if ntype == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                for child in node.children:
                    visit(child, cls_stack, fn_stack)
                return
            short = node_text(name_node, source)
            qualified = ".".join([*cls_stack, short])
            line_start, signature = def_signature(node, "class")
            result.symbols.append(
                Symbol(
                    id=f"{file}::{qualified}",
                    file=file,
                    kind="class",
                    name=qualified,
                    line_start=line_start,
                    line_end=end_line_1b(node),
                    signature=signature,
                )
            )
            for child in node.children:
                visit(child, [*cls_stack, short], fn_stack)
            return

        if ntype == "import_statement":
            _collect_import_statement(node, source, file, result)
            return

        if ntype == "import_from_statement":
            _collect_import_from(node, source, file, result)
            return

        if ntype == "call":
            func = node.child_by_field_name("function")
            if func is not None:
                caller = ".".join([*cls_stack, *fn_stack])
                result.calls.append(
                    RawCall(caller_symbol=caller, callee_name=node_text(func, source), line=line_of(node))
                )
            for child in node.children:
                visit(child, cls_stack, fn_stack)
            return

        if ntype == "expression_statement" and node.parent is not None and node.parent.type == "module":
            assignment = next((c for c in node.children if c.type == "assignment"), None)
            if assignment is not None:
                left = assignment.child_by_field_name("left")
                right = assignment.child_by_field_name("right")
                if (
                    left is not None
                    and left.type == "identifier"
                    and right is not None
                    and right.type in _CONST_VALUE_TYPES
                ):
                    name = node_text(left, source)
                    if _CONST_NAME_RE.match(name):
                        result.symbols.append(
                            Symbol(
                                id=f"{file}::{name}",
                                file=file,
                                kind="constant",
                                name=name,
                                line_start=line_of(assignment),
                                line_end=end_line_1b(assignment),
                                signature=node_text(assignment, source)[:120],
                            )
                        )
                        return

        for child in node.children:
            visit(child, cls_stack, fn_stack)

    visit(tree.root_node, [], [])
    return result


def _collect_import_statement(node: Node, source: bytes, file: str, result: FileIndex) -> None:
    """`import a.b.c` / `import a.b as x`（含逗号分隔多项）。"""
    for child in node.children:
        if child.type == "dotted_name":
            result.imports.append(ImportRecord(file=file, module=node_text(child, source), name="", alias=""))
        elif child.type == "aliased_import":
            module_node = child.child_by_field_name("name")
            alias_node = child.child_by_field_name("alias")
            result.imports.append(
                ImportRecord(
                    file=file,
                    module=node_text(module_node, source) if module_node is not None else "",
                    name="",
                    alias=node_text(alias_node, source) if alias_node is not None else "",
                )
            )


def _collect_import_from(node: Node, source: bytes, file: str, result: FileIndex) -> None:
    """`from a.b import c as d` / `from .mod import x` / `from mod import *`。"""
    module_node = node.child_by_field_name("module_name")
    module_text = node_text(module_node, source) if module_node is not None else ""
    for child in node.children:
        if module_node is not None and child.id == module_node.id:
            continue
        if child.type == "dotted_name":
            result.imports.append(ImportRecord(file=file, module=module_text, name=node_text(child, source), alias=""))
        elif child.type == "aliased_import":
            name_node = child.child_by_field_name("name")
            alias_node = child.child_by_field_name("alias")
            result.imports.append(
                ImportRecord(
                    file=file,
                    module=module_text,
                    name=node_text(name_node, source) if name_node is not None else "",
                    alias=node_text(alias_node, source) if alias_node is not None else "",
                )
            )
        elif child.type == "wildcard_import":
            result.imports.append(ImportRecord(file=file, module=module_text, name="*", alias=""))
