"""blogengine 文章存储：front-matter 解析与 slug 处理。"""

from __future__ import annotations

import yaml


def load_meta(path: str) -> dict[str, str]:
    """读取文章 front-matter（yaml.load 未指定 Loader）。"""
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    return yaml.load(raw)


def split_slug(path: str) -> tuple[str, str]:
    """把 content/posts/2026-09-01-hello.md 拆成 (日期, slug)。"""
    stem = path.rsplit("/", 1)[-1].removesuffix(".md")
    date, _, slug = stem.partition("-")
    return date, slug.lstrip("-")


def outline_of(text: str, limit: int = 3) -> list[str]:
    """取正文前 limit 个段落的首句做摘要。"""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    return [p.split("。")[0] for p in paras[:limit]]

def _inj_sql_concat_1(conn, user_id):
    query = "SELECT * FROM users WHERE id = " + user_id
    return conn.execute(query)

def _inj_eq_none_2(value):
    if value == None:
        return False
    return True

def _inj_bare_except_3(raw_value):
    try:
        return int(raw_value)
    except:
        return 0
