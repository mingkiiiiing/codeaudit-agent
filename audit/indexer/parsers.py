"""tree-sitter 解析器工厂：按语言名缓存 Language / Parser 实例。

支持 python / javascript / typescript（ts/tsx 共用 typescript 语法）/ java（W24-A）/
go（W26-C）/ cpp（W28-C）。
"""

from __future__ import annotations

from functools import lru_cache

from tree_sitter import Language, Parser

import tree_sitter_cpp
import tree_sitter_go
import tree_sitter_java
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript

SUPPORTED_LANGUAGES = ("python", "javascript", "typescript", "java", "go", "cpp")


@lru_cache(maxsize=None)
def get_language(language: str) -> Language | None:
    """按语言名取 tree-sitter Language；不支持返回 None。"""
    if language == "python":
        return Language(tree_sitter_python.language())
    if language == "javascript":
        return Language(tree_sitter_javascript.language())
    if language == "typescript":
        return Language(tree_sitter_typescript.language_typescript())
    if language == "java":
        return Language(tree_sitter_java.language())
    if language == "go":
        return Language(tree_sitter_go.language())
    if language == "cpp":
        return Language(tree_sitter_cpp.language())
    return None


@lru_cache(maxsize=None)
def get_parser(language: str) -> Parser | None:
    """按语言名取（缓存的）Parser；不支持返回 None。"""
    lang = get_language(language)
    if lang is None:
        return None
    return Parser(lang)


def parse_source(language: str, source: bytes):
    """解析源码，返回 (tree, parsed_ok)。不支持的语言返回 (None, False)。"""
    parser = get_parser(language)
    if parser is None:
        return None, False
    try:
        tree = parser.parse(source)
    except Exception:
        return None, False
    return tree, not tree.root_node.has_error


def node_text(node, source: bytes) -> str:
    """按字节区间取节点原文。"""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def end_line_1b(node) -> int:
    """节点结束行（1-based 闭区间）。"""
    row, col = node.end_point
    return row + 1 if col > 0 else row
