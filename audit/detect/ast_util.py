"""AST 接线工具（P0-3，AST 断供修复）：RuleContext.tree 的解析闸门与轻量遍历助手。

设计口径（任务卡 P0-3）：
- 引擎构建 RuleContext 时对 python 文件用 tree-sitter 解析 AST 传入 ``ctx.tree``；
- 解析失败 / 超时 / 无解析器 / 环境关闭 → 返回 None 继续扫描，**绝不阻断**；
- 性能护栏：单文件解析耗时超预算即对本文件降级并熔断同批次后续解析
  （tree-sitter 正常毫秒级，超预算即病态输入，宁可无 AST 不可拖垮扫描）；
- 环境开关：``CODEAUDIT_DISABLE_AST=1`` 整体关闭接线（性能对照与应急降级通道）。

解析器复用 audit.indexer.parsers（延迟导入）：tree-sitter 为项目硬依赖，
导入失败只降级为 None，不抛异常。遍历助手（walk / line_nodes 等）供各规则做
AST 佐证（提升置信 / 降误报），全部为纯函数、不修改 ctx、无 IO。
"""

from __future__ import annotations

import os
import time
from typing import Any, Iterator

__all__ = [
    "AST_CONFIRM_CONFIDENCE",
    "AstParseGate",
    "ast_enabled",
    "attr_chain_names",
    "confirm_hit",
    "end_line",
    "ident_text",
    "line_node_map",
    "line_nodes",
    "root_identifier",
    "walk",
]

# AST 佐证通过后的 Issue 置信度（基线 0.7 + 0.1；hits_to_issues 读 meta["confidence"]）
AST_CONFIRM_CONFIDENCE = 0.8

# 环境开关：置 "1" 整体关闭 AST 接线（tree 恒为 None，规则走行级兜底路径）
_ENV_DISABLE = "CODEAUDIT_DISABLE_AST"
# 单文件解析时间预算（秒）：tree-sitter 对万行级文件也是毫秒~十毫秒级，
# 超过预算视为病态输入——本文件降级为 None 并熔断同批次后续解析。
DEFAULT_PER_FILE_BUDGET_S = 2.0


def ast_enabled() -> bool:
    """读取环境开关：``CODEAUDIT_DISABLE_AST=1`` 时返回 False（其余值均视为开启）。"""
    return os.environ.get(_ENV_DISABLE, "").strip() != "1"


class AstParseGate:
    """单次扫描批次的 AST 解析闸门：任何失败都降级为 None，绝不阻断扫描。

    - 实例为每次 build_rule_contexts / 每个 worker 私有，无共享可变状态
      （Windows spawn 安全；熔断状态不跨进程传播，代价只是重复尝试，无害）；
    - ``stats`` 记录 parsed / degraded 计数，``disabled_reason`` 记录熔断原因，
      由引擎写入 ctx.extra["ast_wiring"] 供观测。
    """

    def __init__(self, per_file_budget_s: float = DEFAULT_PER_FILE_BUDGET_S) -> None:
        self.per_file_budget_s = per_file_budget_s
        self.disabled_reason: str | None = None
        self.stats: dict[str, int] = {"parsed": 0, "degraded": 0}

    def parse(self, language: str, source: str) -> Any:
        """解析源码为 tree-sitter Tree；任何失败路径都返回 None（绝不抛异常）。

        tree-sitter 为容错恢复式解析：源码含语法错误时仍返回可用部分树
        （has_error 置位），规则侧以保守口径消费即可，无需按 has_error 降级。
        """
        if self.disabled_reason is not None or not ast_enabled():
            self.stats["degraded"] += 1
            return None
        try:  # 延迟导入 + 工厂复用（Language/Parser 已按语言缓存）
            from audit.indexer.parsers import get_parser

            parser = get_parser(language)
        except Exception:  # 依赖缺失等：无解析器 → 降级
            parser = None
        if parser is None:
            self.stats["degraded"] += 1
            return None
        start = time.perf_counter()
        try:
            tree = parser.parse(source.encode("utf-8", errors="replace"))
        except Exception:
            self.stats["degraded"] += 1
            return None
        elapsed = time.perf_counter() - start
        if elapsed > self.per_file_budget_s:
            self.disabled_reason = (
                f"解析 {language} 文件耗时 {elapsed:.2f}s 超预算 {self.per_file_budget_s:.2f}s，"
                "已熔断本批次后续 AST 解析（降级为行级扫描）"
            )
            self.stats["degraded"] += 1
            return None
        self.stats["parsed"] += 1
        return tree


# ---------------------------------------------------------------- 遍历助手


def walk(root: Any) -> Iterator[Any]:
    """DFS 先序遍历（children 顺序 = 源码顺序），产出全部节点。"""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        children = node.children
        for i in range(len(children) - 1, -1, -1):
            stack.append(children[i])


def line_nodes(tree: Any, lineno: int) -> list[Any]:
    """起点落在指定行（1-based）的全部节点（DFS 序）；tree 为 None 返回空表。"""
    if tree is None or tree.root_node is None:
        return []
    return [n for n in walk(tree.root_node) if n.start_point[0] + 1 == lineno]


def line_node_map(tree: Any) -> dict[int, list[Any]]:
    """一次遍历产出 行号 -> 起点在该行的节点列表（供逐命中行佐证，避免重复走树）。"""
    if tree is None or tree.root_node is None:
        return {}
    out: dict[int, list[Any]] = {}
    for n in walk(tree.root_node):
        out.setdefault(n.start_point[0] + 1, []).append(n)
    return out


def end_line(node: Any) -> int:
    """节点结束行（1-based 闭区间；end col==0 归属前一行，与 indexer 同口径）。"""
    row, col = node.end_point
    return row + 1 if col > 0 else row


def confirm_hit(hit: Any, confidence: float = AST_CONFIRM_CONFIDENCE) -> None:
    """AST 佐证通过后的命中注记：置 ``ast_confirmed`` 并提升置信度。

    只加不改：行号 / 文案 / severity / category 一律不动（金标不回退硬约束——
    佐证失败时不调用本函数，行级结果原样保留）。
    """
    meta = hit.meta
    meta["ast_confirmed"] = True
    # hits_to_issues 以 meta["confidence"]（缺省 0.7）作为 Issue 置信度
    meta["confidence"] = max(float(meta.get("confidence", 0.7)), confidence)


def ident_text(node: Any, source: bytes) -> str:
    """按字节区间取 identifier 节点原文。"""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def root_identifier(node: Any) -> Any | None:
    """取表达式链最左侧的根 identifier 节点（attribute/subscript/call 递归左展）。

    ``a.b.c`` → a；``a[i].b`` → a；纯 identifier → 自身；无法解析返回 None。
    注：tree-sitter 节点包装对象是临时的，身份判定须用字节区间而非 id()。
    """
    seen: set[tuple[int, int]] = set()
    cur = node
    while cur is not None:
        key = (cur.start_byte, cur.end_byte)
        if key in seen:
            return None  # 防御：正常左展严格缩小，不会回头
        seen.add(key)
        if cur.type == "identifier":
            return cur
        if cur.type == "attribute":
            cur = cur.child_by_field_name("object")
            continue
        if cur.type == "subscript":
            cur = cur.child_by_field_name("value")
            continue
        if cur.type == "call":
            cur = cur.child_by_field_name("function")
            continue
        return None
    return None


def attr_chain_names(node: Any, source: bytes) -> list[str]:
    """收集调用/属性链上的属性名（保持源码从左到右的顺序）。

    ``session.query(U).get(1)`` → ["query", "get"]；``M.objects.get(1)`` →
    ["objects", "get"]。非 attribute/call 链返回空表。供 ORM 等规则做链式
    形态的 AST 佐证。链的每次展开都严格走向更小的子树，天然无环。
    """
    names: list[str] = []
    cur = node
    while cur is not None:
        if cur.type == "call":
            cur = cur.child_by_field_name("function")
            continue
        if cur.type == "attribute":
            children = cur.children
            if children and children[-1].type == "identifier":
                names.append(ident_text(children[-1], source))
            cur = cur.child_by_field_name("object")
            continue
        break
    names.reverse()
    return names
