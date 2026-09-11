"""JavaScript / TypeScript 源码提取：Symbol / ImportRecord / 调用点（尽力解析）。

基于 tree-sitter-javascript / tree-sitter-typescript 语法树：
  - function_declaration / 箭头函数赋值 → function
  - method_definition → method（Class.method 限定）
  - class_declaration → class
  - import ... from '...' / export ... from '...' → ImportRecord（module 存原始说明符）
"""

from __future__ import annotations

from tree_sitter import Node

from audit.indexer.parsers import end_line_1b, node_text, parse_source
from audit.indexer.py_extract import FileIndex, RawCall
from audit.models import ImportRecord, Symbol

_FUNCTION_VALUE_TYPES = {"arrow_function", "function_expression", "generator_function_expression"}


def extract_js_ts(source: bytes, file: str, language: str = "javascript") -> FileIndex:
    """解析 JS/TS 源码并提取符号/导入/调用点。"""
    tree, ok = parse_source(language, source)
    result = FileIndex(language=language, parsed_ok=ok)
    if tree is None:
        return result

    def txt(node: Node | None) -> str:
        return node_text(node, source) if node is not None else ""

    def child_of_type(node: Node, ntype: str) -> Node | None:
        return next((c for c in node.children if c.type == ntype), None)

    def signature_to_body(node: Node) -> str:
        """从节点起点到函数体起点的折叠文本作为签名。"""
        body = node.child_by_field_name("body")
        end = body.start_byte if body is not None else node.end_byte
        return " ".join(source[node.start_byte : end].decode("utf-8", errors="replace").split())

    def module_spec(node: Node) -> str:
        """import/export 的 string 源节点 → 去引号说明符。"""
        source_node = node.child_by_field_name("source")
        if source_node is None:
            return ""
        frag = child_of_type(source_node, "string_fragment")
        return txt(frag) if frag is not None else txt(source_node)

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

    def visit(node: Node, cls_stack: list[str], fn_stack: list[str]) -> None:
        ntype = node.type

        if ntype in ("function_declaration", "generator_function_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                short = txt(name_node)
                qualified = ".".join([*cls_stack, *fn_stack, short])
                add_symbol("function", qualified, node, signature_to_body(node))
                for child in node.children:
                    visit(child, cls_stack, [*fn_stack, short])
                return

        if ntype == "class_declaration":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                short = txt(name_node)
                qualified = ".".join([*cls_stack, short])
                add_symbol("class", qualified, node, signature_to_body(node))
                for child in node.children:
                    visit(child, [*cls_stack, short], fn_stack)
                return

        if ntype == "method_definition":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                short = txt(name_node)
                qualified = ".".join([*cls_stack, *fn_stack, short])
                add_symbol("method", qualified, node, signature_to_body(node))
                for child in node.children:
                    visit(child, cls_stack, [*fn_stack, short])
                return

        if ntype in ("lexical_declaration", "variable_declaration"):
            handled = False
            for declarator in node.children:
                if declarator.type != "variable_declarator":
                    continue
                value = declarator.child_by_field_name("value")
                name_node = declarator.child_by_field_name("name")
                if value is None or name_node is None or value.type not in _FUNCTION_VALUE_TYPES:
                    continue
                short = txt(name_node)
                qualified = ".".join([*cls_stack, *fn_stack, short])
                body = value.child_by_field_name("body")
                sig_end = body.start_byte if body is not None else value.end_byte
                signature = " ".join(
                    source[declarator.start_byte : sig_end].decode("utf-8", errors="replace").split()
                )
                add_symbol("function", qualified, declarator, signature)
                handled = True
                visit(value, cls_stack, [*fn_stack, short])
            if handled:
                return

        if ntype == "import_statement":
            spec = module_spec(node)
            _collect_import(node, spec, file, txt, child_of_type, result)
            return

        if ntype == "export_statement":
            if any(c.type == "from" for c in node.children):  # re-export：export {...} from '...'
                _collect_reexport(node, module_spec(node), file, txt, child_of_type, result)

        if ntype == "call_expression":
            func = node.child_by_field_name("function")
            if func is not None:
                caller = ".".join([*cls_stack, *fn_stack])
                result.calls.append(
                    RawCall(caller_symbol=caller, callee_name=txt(func), line=node.start_point[0] + 1)
                )
            for child in node.children:
                visit(child, cls_stack, fn_stack)
            return

        for child in node.children:
            visit(child, cls_stack, fn_stack)

    visit(tree.root_node, [], [])
    return result


def _collect_import(
    node: Node,
    spec: str,
    file: str,
    txt,
    child_of_type,
    result: FileIndex,
) -> None:
    """import 默认/命名/命名空间/副作用导入（尽力解析）。

    注意：import_clause 在 JS 语法中是"子节点类型"而非命名字段，须按类型查找。
    """
    clause = child_of_type(node, "import_clause")
    if clause is None:  # 副作用导入：import 'x';
        result.imports.append(ImportRecord(file=file, module=spec, name="", alias=""))
        return
    for child in clause.children:
        if child.type == "identifier":  # 默认导入：import React from 'react'
            result.imports.append(ImportRecord(file=file, module=spec, name="default", alias=txt(child)))
        elif child.type == "namespace_import":  # import * as ns from 'x'
            name_node = child.child_by_field_name("name") or child_of_type(child, "identifier")
            result.imports.append(ImportRecord(file=file, module=spec, name="*", alias=txt(name_node)))
        elif child.type == "named_imports":  # import { a, b as c } from 'x'
            for specifier in child.children:
                if specifier.type != "import_specifier":
                    continue
                result.imports.append(
                    ImportRecord(
                        file=file,
                        module=spec,
                        name=txt(specifier.child_by_field_name("name")),
                        alias=txt(specifier.child_by_field_name("alias")),
                    )
                )


def _collect_reexport(
    node: Node,
    spec: str,
    file: str,
    txt,
    child_of_type,
    result: FileIndex,
) -> None:
    """export { x } from '...' / export * from '...' 记录为导入（再导出）。"""
    export_clause = child_of_type(node, "export_clause")
    namespace_export = child_of_type(node, "namespace_export")
    if export_clause is not None:
        for specifier in export_clause.children:
            if specifier.type != "export_specifier":
                continue
            result.imports.append(
                ImportRecord(
                    file=file,
                    module=spec,
                    name=txt(specifier.child_by_field_name("name")),
                    alias=txt(specifier.child_by_field_name("alias")),
                )
            )
    elif namespace_export is not None:
        name_node = namespace_export.child_by_field_name("name") or child_of_type(namespace_export, "identifier")
        result.imports.append(ImportRecord(file=file, module=spec, name="*", alias=txt(name_node)))
