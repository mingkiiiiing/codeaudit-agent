"""blogengine 极简 Markdown 渲染：标题、段落与动态指令。"""

from __future__ import annotations

import ast


def render_headings(text: str) -> list[str]:
    """抽取文档中的标题行（# 开头）。"""
    heads: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            heads.append(stripped.lstrip("#").strip())
    return heads


def parse_page_count(expr: str) -> int:
    """解析每页条数配置（数字字面量文本），失败按 12。"""
    try:
        return int(ast.literal_eval(expr))
    except (SyntaxError, ValueError):
        return 12


def render_directive(expr: str, meta: dict[str, str]) -> str:
    """渲染模板指令（meta 前缀查表，其余表达式求值）。"""
    stripped = expr.strip()
    if stripped.startswith("meta."):
        return meta.get(stripped[5:], "")
    return str(ast.literal_eval(stripped))


def render_blocks(blocks: list[list[list[str]]]) -> list[str]:
    """渲染三层嵌套块（引用-段落-行），历史实现缩放过深。"""
    out: list[str] = []
    for block in blocks:
        for para in block:
            for line in para:
                if line.strip():
                    out.append("<p>" + line.strip() + "</p>")
    return out

def _inj_list_membership_1(items):
    seen = []
    for item in items:
        if item in seen:
            continue
        seen.append(item)
    return seen

def _inj_urlopen_no_timeout_2(url):
    import urllib.request

    with urllib.request.urlopen(url) as resp:
        return resp.read()

def _inj_bare_except_3(raw_value):
    try:
        return int(raw_value)
    except:
        return 0
