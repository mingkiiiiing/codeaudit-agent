"""C++ 源码提取：Symbol / ImportRecord / 调用点（W28-C 首轮口径）。

基于 tree-sitter-cpp 语法树：
  - namespace 声明 → Symbol(kind="namespace"，name=嵌套限定名按声明段拼接
    （``namespace a::b`` → a::b；嵌套 namespace 走栈拼接）；id 带 namespace 段
    以避免与同名函数冲突；不入 slices——store 只收 function/method。
    命名空间下的 class/function/constant 一律以命名空间限定名记录（ns::Name），
    与 C++ 作用域语义一致，供跨文件解析按命名空间匹配
  - class/struct 声明（带类体 {}）→ Symbol(kind="class"，name=命名空间限定简名)；
    前置声明（``class Foo;``）无类体不记
  - 函数定义 function_definition → Symbol(kind="function"/"method"，顶层函数记
    命名空间限定简名；类内 inline 方法定义记 method（ns::Class::method）；类外
    定义按声明原文限定（``Class::method`` → ns::Class::method，同样记 method）。
    同名重载在 symbols 表按 id 去重（保留最后一条）——首轮口径
  - const 口径：``const``/``constexpr`` 的**非函数体内**声明（全局/命名空间级/
    类内 static const 成员）的 init_declarator 记 Symbol(kind="constant")，函数体内
    局部 const 不记（符号只覆盖声明性口径，与 go/java 语言包一致）；
    ``#define`` 对象宏（preproc_def）记 Symbol(kind="constant")——宏常量口径；
    函数式宏（preproc_function_def）不是常量，不记
  - #include → ImportRecord(module=头文件路径, name="")：``"..."`` 引号头剥引号、
    ``<...>`` 系统头剥尖括号（C++ 无导入别名语义，alias 恒 ""）
  - 函数体内调用点 call_expression → RawCall（callee=function 字段原文：identifier
    简名 / ``ns::f`` 限定名 / ``obj.m`` 成员形态；caller 为最内层 function/method
    限定名（:: 连接），全局初始化代码为 ""）

已知边界（首轮口径，语料按此写）：
  - 模板实例化调用 ``f<int>(...)``（function 字段为 template_function）不做——
    与 go 泛型实例化 type_conversion_expression 同口径
  - 宏展开内的调用不做：``#define`` 宏体文本（preproc_arg）不参与语法解析，经宏
    展开引入的调用天然收集不到；宏调用本身 ``MAX(a,b)`` 形如普通函数调用，无法
    与真调用区分，按普通调用收集（不识别宏身份）
  - lambda 体内调用不做（照 go func_literal 口径）：lambda_expression 子树整体跳过
  - 预处理条件编译（#ifdef/#if 分支取舍）不做：按语法树全集处理（各分支内的
    定义/调用都收集），不做分支裁剪
  - 函数指针/操作符等无名调用形态（function 字段非 identifier/qualified_identifier/
    field_expression）不记调用点；函数原型/方法签名（无函数体的 declaration）不产
    Symbol；.h 头文件不在扩展名映射内（C/C++ 共享歧义，暂不纳入）
"""

from __future__ import annotations

from tree_sitter import Node

from audit.indexer.parsers import end_line_1b, node_text, parse_source
from audit.indexer.py_extract import FileIndex, RawCall
from audit.models import ImportRecord, Symbol

__all__ = ["extract_cpp"]

# 可作 call_expression callee 的 function 字段形态：简名 / ns::f 限定名 / obj.m 成员
_CPP_CALLABLE_TYPES = ("identifier", "qualified_identifier", "field_expression")

# const 常量口径的修饰词（type_qualifier 原文；static/extern 是 storage_class_specifier，不算）
_CONST_QUALIFIERS = ("const", "constexpr")


def _join(parts: list[str]) -> str:
    """非空段以 :: 连接（C++ 限定名记法）；全空返回 ""。"""
    return "::".join(p for p in parts if p)


def extract_cpp(source: bytes, file: str) -> FileIndex:
    """解析 C++ 源码并提取符号/导入/调用点。"""
    tree, ok = parse_source("cpp", source)
    result = FileIndex(language="cpp", parsed_ok=ok)
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

    def decl_name(declarator: Node | None) -> str:
        """declarator 子树 → 声明名原文：identifier/field_identifier 简名或
        qualified_identifier 限定名（Class::method）；指针/数组包装逐层剥开，
        形态未知返回原文兜底（析构名/操作符名等）。"""
        if declarator is None:
            return ""
        if declarator.type in ("identifier", "field_identifier", "qualified_identifier"):
            return node_text(declarator, source)
        inner = declarator.child_by_field_name("declarator")
        if inner is not None:
            return decl_name(inner)
        return node_text(declarator, source)

    def visit(node: Node, ns: str, classes: list[str], fn_stack: list[str], in_func: bool) -> None:
        ntype = node.type

        if ntype == "preproc_include":
            path_node = node.child_by_field_name("path")
            if path_node is not None:
                path = node_text(path_node, source).strip()
                # 引号头剥 "..."、系统头剥 <...>；module 统一为纯路径
                path = path.strip("\"<>")
                result.imports.append(ImportRecord(file=file, module=path, name="", alias=""))
            return

        if ntype == "preproc_def":
            # 对象宏 → 宏常量口径（docstring 明示）；id 带 macro 段避免同名冲突
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = node_text(name_node, source)
                add_symbol(
                    "constant",
                    name,
                    node,
                    node_text(node, source)[:120],
                    symbol_id=f"{file}::macro::{name}",
                )
            return

        if ntype == "preproc_function_def":
            return  # 函数式宏不是常量（docstring 口径），宏体文本也无解析意义

        if ntype == "namespace_definition":
            name_node = node.child_by_field_name("name")
            if name_node is not None and name_node.type == "namespace_identifier":
                segs = [node_text(name_node, source)]
            elif name_node is not None:
                # C++17 嵌套限定：namespace a::b → nested_namespace_specifier
                segs = [node_text(c, source) for c in name_node.children if c.type == "namespace_identifier"]
            else:
                segs = []  # 匿名 namespace：不记符号，成员沿用外层命名空间前缀
            declared = "::".join(segs)
            # 命名空间符号记栈上全名（app::config），跨文件解析按全名精确/段后缀匹配
            qname = _join([ns, declared]) if declared else ns
            if declared:
                add_symbol(
                    "namespace",
                    qname,
                    node,
                    head_signature(node),
                    symbol_id=f"{file}::namespace::{qname}",
                )
            for child in node.children:
                if child.type != "namespace_identifier":
                    visit(child, qname, classes, fn_stack, in_func)
            return

        if ntype in ("class_specifier", "struct_specifier"):
            name_node = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name_node is None or body is None:
                return  # 前置声明（无类体）不记
            name = _join([ns, *classes, node_text(name_node, source)])
            add_symbol("class", name, node, node_text(node, source)[:120])
            for child in node.children:
                if child is not name_node:
                    visit(child, ns, classes + [node_text(name_node, source)], fn_stack, in_func)
            return

        if ntype == "function_definition":
            declarator = node.child_by_field_name("declarator")
            name = decl_name(declarator.child_by_field_name("declarator")) if declarator is not None else ""
            if not name:
                return  # 形态未知（函数指针声明等）不记，也不入作用域
            if "::" in name or classes:  # 类外定义 Class::method / 类内 inline 均记 method
                kind = "method"
            else:
                kind = "function"
            qualified = _join([ns, *classes, name])
            add_symbol(kind, qualified, node, head_signature(node))
            for child in node.children:
                visit(child, ns, classes, fn_stack + [qualified], True)
            return

        if ntype in ("declaration", "field_declaration") and not in_func:
            # const 口径：const/constexpr 的非函数体内声明记 constant（宏常量见 preproc_def）。
            # 只看直接子节点上的 type_qualifier——const 成员函数的尾置 const 在
            # function_declarator 内部，不会被误判；static 是 storage_class_specifier
            quals = {node_text(c, source) for c in node.children if c.type == "type_qualifier"}
            if quals & set(_CONST_QUALIFIERS):
                for child in node.children:
                    if child.type == "init_declarator":
                        # 命名空间级/全局形态（可多个声明符）：const int a = 1, b = 2;
                        name = decl_name(child.child_by_field_name("declarator"))
                        if name:
                            add_symbol(
                                "constant",
                                _join([ns, *classes, name]),
                                child,
                                node_text(node, source)[:120],
                            )
                    elif child.type == "field_identifier":
                        # 类内成员形态：static const int kMax = 10;（名直挂，无 init_declarator）
                        add_symbol(
                            "constant",
                            _join([ns, *classes, node_text(child, source)]),
                            child,
                            node_text(node, source)[:120],
                        )
            # 不 return：声明内还可能内嵌带类体的 class_specifier（struct X {...} x;）

        if ntype == "call_expression":
            func = node.child_by_field_name("function")
            if in_func and func is not None and func.type in _CPP_CALLABLE_TYPES:
                # 模板实例化（template_function）与无名形态不记；嵌套实参调用照常下钻
                result.calls.append(
                    RawCall(
                        caller_symbol=_join(fn_stack),
                        callee_name=node_text(func, source),
                        line=node.start_point[0] + 1,
                    )
                )
            for child in node.children:
                visit(child, ns, classes, fn_stack, in_func)
            return

        if ntype == "lambda_expression":
            # 边界：lambda 体内调用不做（照 go func_literal 口径）
            return

        for child in node.children:
            visit(child, ns, classes, fn_stack, in_func)

    visit(tree.root_node, "", [], [], False)
    return result
