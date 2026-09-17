"""Java 源码提取：Symbol / ImportRecord / 调用点（W24-A 首轮口径）。

基于 tree-sitter-java 语法树：
  - class / interface / enum 声明 → Symbol(kind="class"，嵌套类 Outer.Inner 限定)
  - method / constructor 声明 → Symbol(kind="method"，Class.method 限定)
  - field 声明 → Symbol(kind="field"；全大写按 python 常量口径记 kind="constant")
  - import a.b.C / import static a.b.C.m / import a.b.* → ImportRecord
  - 方法调用点（method_invocation）→ RawCall（caller 为最内层方法限定名，字段初始化等
    类体顶层调用为 ""）；泛型/注解/lambda 内调用不做（已知边界，语料按此口径写）。
"""

from __future__ import annotations

import re

from tree_sitter import Node

from audit.indexer.parsers import end_line_1b, node_text, parse_source
from audit.indexer.py_extract import FileIndex, RawCall
from audit.models import ImportRecord, Symbol

__all__ = ["extract_java"]

# 全大写字段按 python 侧 _CONST_NAME_RE 同款口径视为常量
_CONST_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# 类型声明（类/接口/枚举）：name 均为命名字段，body 字段名各不相同
_TYPE_DECLARATIONS = {
    "class_declaration": "body",
    "interface_declaration": "body",
    "enum_declaration": "body",
}

# 方法体内部不再作为"调用归属"下钻的子树：lambda / 注解 / 匿名类内调用不做（首轮口径）
_SKIP_CALL_SUBTREES = frozenset(["lambda_expression", "marker_annotation", "annotation"])


def extract_java(source: bytes, file: str) -> FileIndex:
    """解析 Java 源码并提取符号/导入/调用点。"""
    tree, ok = parse_source("java", source)
    result = FileIndex(language="java", parsed_ok=ok)
    if tree is None:
        return result

    def signature_to_body(node: Node) -> str:
        """从节点起点到方法体起点的折叠文本作为签名。"""
        body = node.child_by_field_name("body")
        end = body.start_byte if body is not None else node.end_byte
        return " ".join(source[node.start_byte : end].decode("utf-8", errors="replace").split())

    def add_symbol(kind: str, qualified: str, node: Node, signature: str) -> None:
        result.symbols.append(
            Symbol(
                id=f"{file}::{qualified}",
                file=file,
                kind=kind,
                name=qualified,
                line_start=node.start_point[0] + 1,
                line_end=end_line_1b(node),
                signature=signature,
            )
        )

    def visit(node: Node, cls_stack: list[str], fn_stack: list[str], collect_calls: bool) -> None:
        ntype = node.type

        if ntype in _TYPE_DECLARATIONS:
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return
            short = node_text(name_node, source)
            qualified = ".".join([*cls_stack, short])
            add_symbol("class", qualified, node, signature_to_body(node))
            for child in node.children:
                visit(child, [*cls_stack, short], fn_stack, collect_calls)
            return

        if ntype in ("method_declaration", "constructor_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return
            short = node_text(name_node, source)
            qualified = ".".join([*cls_stack, *fn_stack, short])
            add_symbol("method", qualified, node, signature_to_body(node))
            for child in node.children:
                visit(child, cls_stack, [*fn_stack, short], collect_calls)
            return

        if ntype == "field_declaration":
            for declarator in node.children:
                if declarator.type != "variable_declarator":
                    continue
                name_node = declarator.child_by_field_name("name")
                if name_node is None:
                    continue
                name = node_text(name_node, source)
                kind = "constant" if _CONST_NAME_RE.match(name) else "field"
                add_symbol(
                    kind,
                    ".".join([*cls_stack, name]),
                    declarator,
                    node_text(declarator, source)[:120],
                )
            # 初始化表达式中的调用点（如 `X x = X.create();`）仍按普通口径下钻收集
            for child in node.children:
                visit(child, cls_stack, fn_stack, collect_calls)
            return

        if ntype == "import_declaration":
            _collect_import(node, source, file, result)
            return

        if ntype == "method_invocation":
            if collect_calls:
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    obj = node.child_by_field_name("object")
                    callee = f"{node_text(obj, source)}.{node_text(name_node, source)}" if obj is not None else node_text(name_node, source)
                    result.calls.append(
                        RawCall(
                            caller_symbol=".".join([*cls_stack, *fn_stack]),
                            callee_name=callee,
                            line=node.start_point[0] + 1,
                        )
                    )
            # 参数内嵌套调用（foo(bar())）与普通调用同口径收集
            for child in node.children:
                visit(child, cls_stack, fn_stack, collect_calls)
            return

        if ntype in _SKIP_CALL_SUBTREES:
            return

        for child in node.children:
            visit(child, cls_stack, fn_stack, collect_calls)

    visit(tree.root_node, [], [], True)
    return result


def _collect_import(node: Node, source: bytes, file: str, result: FileIndex) -> None:
    """``import a.b.C`` / ``import static a.b.C.m`` / ``import a.b.*``（含单行多形态）。

    - 普通导入：module=完整说明符，name=""；
    - 静态导入：module=类型全名（去掉最后一段），name=成员名；
    - 通配导入（asterisk）：module=包名，name="*"。
    """
    is_static = any(c.type == "static" for c in node.children)
    ident = next((c for c in node.children if c.type in ("scoped_identifier", "identifier")), None)
    if ident is None:
        return
    full = node_text(ident, source)
    if any(c.type == "asterisk" for c in node.children):
        result.imports.append(ImportRecord(file=file, module=full, name="*", alias=""))
    elif is_static:
        module, _, member = full.rpartition(".")
        result.imports.append(ImportRecord(file=file, module=module, name=member, alias=""))
    else:
        result.imports.append(ImportRecord(file=file, module=full, name="", alias=""))
