"""Patch 接口兼容性比对（W19，审计 P1 清偿）：apply 前后公开函数签名 diff。

职责（契约字段：Patch.compat_notes，audit/models.py——本模块只写清单不改契约）：
- extract_python_signatures：提取模块级与一级类方法的公开函数签名（key=限定名
  ``func`` / ``Class.method``，value=规范化参数串）；实现 tree-sitter 优先（复用
  audit.indexer.parsers 既有设施，与 verifier.syntax_ok 同源），解析器不可用或
  解析失败（root has_error）时回退正则 ``^[ \\t]*def\\s+(\\w+)\\s*\\(([^)]*)\\)``。
- diff_signatures：比对 before/after 签名，输出中文破坏性变更清单——"移除函数
  foo"、"移除方法 Class.method"、"变更签名 bar(a, b) -> bar(a, b, c)"（参数
  增/减/改名均算破坏）；新增带默认值的参数不算破坏（不计入）；类型注解差异
  不算破坏（规范化时直接忽略注解）。
- collect_compat_notes：对 diff 涉及的 .py 文件做组合比对（before=apply 前留底
  字节，即 stage._IssueRun.backups；after=当前工作副本），供 stage._finalize
  统一写入 patch.compat_notes。

安全边界：纯只读函数集合，不做任何写入；任何输入（坏源码/缺失文件）都不抛
异常路径之外的错误——比对结果只作提示，绝不阻断 fix 主流程（stage 侧另有
try/except 兜底记录 ctx.extra["compat_errors"]）。

已知限制：
- 仅覆盖 Python 文件；JS/TS 函数参数语义灵活（arguments 对象/rest 参数/默认
  参数）且项目无对应提取器，暂不比对（JS/TS 补丁 compat_notes 恒为空）。
- 正则回退路径不支持默认值内含逗号的参数（如 ``def f(a=(1, 2))`` 会按逗号
  误拆）；tree-sitter 主路径按语法树切分，无此问题。
- 比对范围限于模块级函数与一级类方法：嵌套函数/闭包不属于公开 API 面，
  不参与比对。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from audit.indexer.parsers import node_text, parse_source

__all__ = ["extract_python_signatures", "diff_signatures", "collect_compat_notes"]

# 正则回退（W19）：用 [ \t]* 锚定行首缩进（\s* 会贪婪吃掉换行导致缩进误判）
_FALLBACK_DEF_RE = re.compile(r"^([ \t]*)def\s+(\w+)\s*\(([^)]*)\)", re.MULTILINE)
_FALLBACK_CLASS_RE = re.compile(r"^([ \t]*)class\s+(\w+)")
# 带默认值/可变参数的后缀标记：参数名后追加 "=" 表示有默认值（与 tree-sitter
# 主路径共用同一规范化表示，保证 before/after 两侧口径一致）


# ---------------------------------------------------------------- 签名规范化


def _splat_label(inner: Any, source: bytes, prefix: str) -> str:
    """*args / **kwargs 类节点 → prefix + 内层标识符名（内层缺失只留 prefix）。"""
    name_node = next((c for c in inner.children if c.type == "identifier"), None)
    return prefix + (node_text(name_node, source) if name_node is not None else "")


def _param_label(node: Any, source: bytes) -> str:
    """单个参数节点 → 规范化标签：``name`` / ``name=``（带默认值）/ ``*args`` / ``**kw`` / ``*``。

    类型注解不算破坏性变更，规范化时直接忽略（typed_* 节点只取参数名部分）。
    """
    ntype = node.type
    if ntype == "identifier":
        return node_text(node, source)
    if ntype == "typed_parameter":
        # 三种形态：name: type / *args: type / **kw: type——内层复用 splat/identifier 规则
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            return node_text(name_node, source)
        inner = next(
            (c for c in node.children if c.type in ("identifier", "list_splat_pattern", "dictionary_splat_pattern")),
            None,
        )
        return _param_label(inner, source) if inner is not None else ""
    if ntype in ("default_parameter", "typed_default_parameter"):
        name_node = node.child_by_field_name("name")
        return (node_text(name_node, source) if name_node is not None else "") + "="
    if ntype == "list_splat_pattern":
        return _splat_label(node, source, "*")
    if ntype == "dictionary_splat_pattern":
        return _splat_label(node, source, "**")
    text = node_text(node, source).strip()
    return text if text in ("*", "**") else ""  # 裸 * 分隔符（keyword-only 标记）


def _params_signature(params_node: Any, source: bytes) -> str:
    """parameters 节点 → 规范化参数串（逗号分隔、保序），如 ``a, b=, *args``。"""
    labels: list[str] = []
    for child in params_node.children:
        if child.type == ",":
            continue
        label = _param_label(child, source)
        if label:
            labels.append(label)
    return ", ".join(labels)


# ---------------------------------------------------------------- tree-sitter 主路径


def _unwrap_decorated(node: Any) -> Any:
    """剥掉装饰器层：decorated_definition → 内层 function/class_definition。"""
    if node.type != "decorated_definition":
        return node
    for child in node.children:
        if child.type in ("function_definition", "class_definition"):
            return child
    return None


def _extract_by_tree(root: Any, source: bytes) -> dict[str, str]:
    """遍历语法树：只收模块级函数与一级类方法（含装饰器形态），私有符号排除。"""
    signatures: dict[str, str] = {}

    def fn_name(node: Any) -> str:
        name_node = node.child_by_field_name("name")
        return node_text(name_node, source) if name_node is not None else ""

    def fn_params(node: Any) -> str:
        params = node.child_by_field_name("parameters")
        return _params_signature(params, source) if params is not None else ""

    def visit_body(body: Any, cls_name: str | None) -> None:
        for child in body.children:
            definition = _unwrap_decorated(child)
            if definition is None or definition.type != "function_definition":
                continue
            name = fn_name(definition)
            if not name or name.startswith("_"):  # 仅公开符号（非下划线开头）
                continue
            key = name if cls_name is None else f"{cls_name}.{name}"
            signatures[key] = fn_params(definition)

    visit_body(root, None)
    for child in root.children:
        definition = _unwrap_decorated(child)
        if definition is None or definition.type != "class_definition":
            continue
        cls_name = fn_name(definition)
        if not cls_name or cls_name.startswith("_"):  # 私有类整体排除（_Foo.method 不算公开 API）
            continue
        body = definition.child_by_field_name("body")
        if body is not None:
            visit_body(body, cls_name)
    return signatures


# ---------------------------------------------------------------- 正则回退路径


def _normalize_param_text(param_text: str) -> str:
    """正则路径的参数原文 → 规范化标签（与 tree-sitter 路径同一表示）。

    已知限制：默认值内含逗号（如 a=(1, 2)）会按逗号误拆（见模块 docstring）。
    """
    labels: list[str] = []
    for raw in param_text.split(","):
        token = raw.strip()
        if not token:
            continue
        if token == "*" or token == "**":  # 裸分隔符（** 单独出现不合法，防御性保留）
            labels.append(token)
        elif token.startswith("**"):
            labels.append("**" + token[2:].split(":", 1)[0].strip())
        elif token.startswith("*"):
            labels.append("*" + token[1:].split(":", 1)[0].strip())
        else:
            has_default = "=" in token
            base = token.split("=", 1)[0]
            name = base.split(":", 1)[0].strip()
            labels.append(name + ("=" if has_default else ""))
    return ", ".join(labels)


def _extract_by_regex(source: str) -> dict[str, str]:
    """正则回退：逐行扫 def/class，按缩进维护一级类栈（无法感知语法树）。"""
    signatures: dict[str, str] = {}
    class_stack: list[tuple[int, str]] = []  # (缩进列, 类名)
    for line in source.splitlines():
        cls_match = _FALLBACK_CLASS_RE.match(line)
        if cls_match:
            indent = len(cls_match.group(1).expandtabs(4))
            while class_stack and indent <= class_stack[-1][0]:
                class_stack.pop()
            class_stack.append((indent, cls_match.group(2)))
            continue
        def_match = _FALLBACK_DEF_RE.match(line)
        if not def_match:
            continue
        indent = len(def_match.group(1).expandtabs(4))
        name = def_match.group(2)
        if name.startswith("_"):
            continue
        while class_stack and indent <= class_stack[-1][0]:
            class_stack.pop()
        if class_stack:
            cls_name = class_stack[-1][1]
            if cls_name.startswith("_"):  # 私有类整体排除
                continue
            key = f"{cls_name}.{name}"
        else:
            key = name
        signatures[key] = _normalize_param_text(def_match.group(3))
    return signatures


# ---------------------------------------------------------------- 对外入口


def extract_python_signatures(source: str) -> dict[str, str]:
    """提取公开函数签名（模块级 + 一级类方法）；tree-sitter 失败回退正则，不抛异常。

    Args:
        source: Python 源码文本。

    Returns:
        {限定名: 规范化参数串}；空源码/全私有/解析全失败时为空 dict。
    """
    data = source.encode("utf-8")
    try:
        tree, parsed_ok = parse_source("python", data)
    except Exception:  # noqa: BLE001 —— 解析器异常兜底（tree-sitter 不可用等）
        tree, parsed_ok = None, False
    if tree is not None and parsed_ok:
        return _extract_by_tree(tree.root_node, data)
    return _extract_by_regex(source)


def _is_nonbreaking_extension(old: str, new: str) -> bool:
    """判定签名扩展是否向后兼容：新参列表以旧参列表为前缀，且新增部分全部
    带默认值（name=）或为可变参数（*args / **kw / 裸 *）——旧调用方不受影响。
    """
    old_parts = old.split(", ") if old else []
    new_parts = new.split(", ") if new else []
    if len(new_parts) < len(old_parts):
        return False
    if new_parts[: len(old_parts)] != old_parts:
        return False
    tail = new_parts[len(old_parts) :]
    return all(part == "*" or part.startswith("*") or part.endswith("=") for part in tail)


def diff_signatures(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """比对 before/after 公开签名，输出中文破坏性变更清单（确定性排序）。

    规则：
    - before 有 after 无 → "移除函数 foo" / "移除方法 Class.method"（按限定名
      是否含点区分）；after 新增符号不算破坏，不报；
    - 两侧都有但参数串不同 → "变更签名 bar(a, b) -> bar(a, b, c)"；其中
      新增带默认值参数的向后兼容扩展不算破坏（_is_nonbreaking_extension）。
    """
    notes: list[str] = []
    for name in sorted(before):
        if name in after:
            continue
        kind = "方法" if "." in name else "函数"
        notes.append(f"移除{kind} {name}")
    for name in sorted(set(before) & set(after)):
        old, new = before[name], after[name]
        if old == new or _is_nonbreaking_extension(old, new):
            continue
        notes.append(f"变更签名 {name}({old}) -> {name}({new})")
    return notes


# ---------------------------------------------------------------- stage 接线组合


def _decode_source(data: bytes | None) -> str:
    """留底字节 → 文本（utf-8 优先，gbk 容错，与 patcher._decode_output 同语义）。"""
    if not data:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def collect_compat_notes(backups: Mapping[str, Any], workspace: Any) -> list[str]:
    """对 diff 涉及的 .py 文件做 apply 前后公开签名比对（stage._finalize 统一调用）。

    Args:
        backups: {相对路径: 应用前文件字节}（stage._IssueRun.backups；None 表示
            补丁新建的文件——新文件里的公开函数全是新增，天然不算破坏）。
        workspace: WorkspaceContext（读当前工作副本内容作为 after）。

    Returns:
        中文破坏性变更清单（可能为空）；未涉及 .py 文件、已回滚或无签名变化
        的补丁返回空列表。
    """
    notes: list[str] = []
    for rel_path in sorted(backups):
        rel = str(rel_path).replace("\\", "/")
        if not rel.endswith(".py"):
            continue  # 已知限制：仅 Python 文件参与比对（JS/TS 恒空）
        before_text = _decode_source(backups[rel_path])
        try:
            after_bytes = workspace.abs_path(rel_path).read_bytes()
        except OSError:
            # 文件被补丁删除且未回滚：after 为空，before 的公开函数逐个报"移除"
            # （删除公开文件属真实接口破坏）；回滚路径 after==before 恒无差异。
            after_bytes = None
        after_text = _decode_source(after_bytes)
        if before_text == after_text:
            continue  # 该文件实际未变（如回滚后），跳过
        notes.extend(
            diff_signatures(extract_python_signatures(before_text), extract_python_signatures(after_text))
        )
    return notes
