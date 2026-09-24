"""Go 源码提取：Symbol / ImportRecord / 调用点（W26-C 首轮口径）。

基于 tree-sitter-go 语法树：
  - package 声明 → Symbol(kind="package"，name=包名；id 带 package 段以避免与
    同名函数（func main / package main）在 symbols 表主键上冲突；不入 slices——
    store 只收 function/method)
  - type struct / interface 声明 → Symbol(kind="class"，name=包内简名；Go 类型不嵌套，无限定)
  - 顶层 func 声明 → Symbol(kind="function"，name=简名)
  - 带 receiver 的方法 → Symbol(kind="method"，限定名口径：值接收者 ``Type.Method``、
    指针接收者 ``(*Type).Method``（receiver 类型取声明原文剥空白，与 Go 方法集记法一致）
  - const 声明（单行/圆括号块 const_spec）→ Symbol(kind="constant"，name=简名)。
    常量口径：Go 无全大写命名惯例，所有 const_spec 一律记 constant；「导出与否」按
    名称首字母大小写判定（首字母大写=导出），不改 kind，由消费方按名称自行判断
  - import "x" 单行 / import ( ... ) 圆括号块 → ImportRecord(module=去引号导入路径,
    name="", alias=别名)；``_ "x"`` 匿名导入 alias 记 "_"，普通导入 alias 记 ""
  - 函数内调用点 call_expression → RawCall（callee=function 字段原文：identifier 简名
    或 selector 点链如 pkg.F / obj.M；caller 为最内层 function/method 限定名，包级为 ""）

已知边界（首轮口径，语料按此写）：
  - func_literal（匿名函数 / ``go func(){...}()`` 字面量）体内的调用不做——与 java
    lambda 边界同口径；具名调用的 ``go s.Method()`` 语句本身仍按普通调用收集
  - 内建函数/内建类型转换（len / append / int / string / rune 等，tree-sitter-go
    中与普通调用同为 call_expression，无跨文件解析意义）不记调用点
  - 泛型实例化 ``F[T](x)`` / ``obj.M[T](x)`` 在 tree-sitter-go 中是
    type_conversion_expression（非 call_expression），不做
  - struct 字段、顶层 var、接口方法签名不产 Symbol；跨行函数签名（参数换行）
    行级规则侧同样不做
"""

from __future__ import annotations

from tree_sitter import Node

from audit.indexer.parsers import end_line_1b, node_text, parse_source
from audit.indexer.py_extract import FileIndex, RawCall
from audit.models import ImportRecord, Symbol

__all__ = ["extract_go"]

# go 内建函数与内建类型（可转换形态）：simple-name 调用不记调用点
_GO_BUILTINS = frozenset(
    """append cap close complex copy delete imag len make new panic print println real recover
    bool byte complex64 complex128 error float32 float64 int int8 int16 int32 int64 rune string
    uint uint8 uint16 uint32 uint64 uintptr any comparable""".split()
)


def _qualified_method(receiver: Node | None, name: str, source: bytes) -> str:
    """receiver 参数表 → 方法限定名：值接收者 ``Type.M``，指针接收者 ``(*Type).M``。"""
    if receiver is None:
        return name
    decl = next((c for c in receiver.children if c.type == "parameter_declaration"), None)
    type_node = decl.child_by_field_name("type") if decl is not None else None
    if type_node is None:
        return name
    type_text = node_text(type_node, source).replace(" ", "").replace("\t", "")
    if type_text.startswith("*"):
        return f"(*{type_text[1:]}).{name}"
    return f"{type_text}.{name}"


def extract_go(source: bytes, file: str) -> FileIndex:
    """解析 Go 源码并提取符号/导入/调用点。"""
    tree, ok = parse_source("go", source)
    result = FileIndex(language="go", parsed_ok=ok)
    if tree is None:
        return result

    def head_signature(node: Node) -> str:
        """从节点起点到函数体起点的折叠文本作为签名。"""
        body = node.child_by_field_name("body")
        end = body.start_byte if body is not None else node.end_byte
        return " ".join(source[node.start_byte : end].decode("utf-8", errors="replace").split())

    def add_symbol(kind: str, name: str, node: Node, signature: str, *, symbol_id: str | None = None) -> None:
        result.symbols.append(
            Symbol(
                id=symbol_id or f"{file}::{name}",
                file=file,
                kind=kind,
                name=name,
                line_start=node.start_point[0] + 1,
                line_end=end_line_1b(node),
                signature=signature,
            )
        )

    def collect_import_spec(spec: Node) -> None:
        path_node = spec.child_by_field_name("path")
        if path_node is None:
            return
        path = node_text(path_node, source).strip("\"`")
        name_node = spec.child_by_field_name("name")
        alias = node_text(name_node, source) if name_node is not None else ""
        result.imports.append(ImportRecord(file=file, module=path, name="", alias=alias))

    def visit(node: Node, fn_stack: list[str], collect_calls: bool) -> None:
        ntype = node.type

        if ntype == "package_clause":
            pkg = next((c for c in node.children if c.type == "package_identifier"), None)
            if pkg is not None:
                # id 加 package 段：Go 允许 func main 与 package main 同文件同名，
                # 共用 f"{file}::{name}" 会在 symbols 表主键上互斥
                add_symbol(
                    "package",
                    node_text(pkg, source),
                    node,
                    node_text(node, source),
                    symbol_id=f"{file}::package::{node_text(pkg, source)}",
                )
            return

        if ntype == "import_declaration":
            for child in node.children:
                if child.type == "import_spec":
                    collect_import_spec(child)
                elif child.type == "import_spec_list":
                    for spec in child.children:
                        if spec.type == "import_spec":
                            collect_import_spec(spec)
            return

        if ntype == "type_declaration":
            for child in node.children:
                if child.type == "type_spec":
                    name_node = child.child_by_field_name("name")
                    if name_node is not None:
                        add_symbol("class", node_text(name_node, source), child, node_text(child, source)[:120])
            return

        if ntype == "const_declaration":
            for child in node.children:
                if child.type != "const_spec":
                    continue
                name_node = child.child_by_field_name("name")
                if name_node is None:
                    continue
                add_symbol("constant", node_text(name_node, source), child, node_text(child, source)[:120])
            return

        if ntype == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return
            qualified = node_text(name_node, source)
            add_symbol("function", qualified, node, head_signature(node))
            for child in node.children:
                visit(child, [qualified], collect_calls)
            return

        if ntype == "method_declaration":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return
            qualified = _qualified_method(node.child_by_field_name("receiver"), node_text(name_node, source), source)
            add_symbol("method", qualified, node, head_signature(node))
            for child in node.children:
                visit(child, [qualified], collect_calls)
            return

        if ntype == "call_expression":
            func = node.child_by_field_name("function")
            if (
                collect_calls
                and func is not None
                and func.type != "func_literal"
                and not (func.type == "identifier" and node_text(func, source) in _GO_BUILTINS)
            ):
                # 匿名函数字面量调用（go func(){...}()）无 callee 名、内建转换无解析
                # 意义，均不记录（同 java 无名不记口径）
                result.calls.append(
                    RawCall(
                        caller_symbol=".".join(fn_stack),
                        callee_name=node_text(func, source),
                        line=node.start_point[0] + 1,
                    )
                )
            # 参数内嵌套调用（f(g())）与普通调用同口径收集
            for child in node.children:
                visit(child, fn_stack, collect_calls)
            return

        if ntype == "func_literal":
            # 边界：匿名函数体内调用不做（含 go func(){...}()），与 java lambda 同口径
            return

        for child in node.children:
            visit(child, fn_stack, collect_calls)

    visit(tree.root_node, [], True)
    return result
