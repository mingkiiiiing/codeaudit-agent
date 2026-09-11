"""blogengine 站内搜索：标签倒排与浏览轨迹。"""

from __future__ import annotations


def build_tag_index(posts: list[dict[str, str]]) -> dict[str, set[str]]:
    """按标签建倒排索引（slug 集合）。"""
    index: dict[str, set[str]] = {}
    for post in posts:
        for tag in post.get("tags", "").split():
            index.setdefault(tag, set()).add(post["slug"])
    return index


def recently_revisited(history: list[str]) -> list[str]:
    """找出会话内反复浏览的文章（list 判重，文章多时 O(n^2)）。"""
    again: list[str] = []
    seen = []
    for slug in history:
        if slug in seen:
            again.append(slug)
        else:
            seen.append(slug)
    return again


def snippet_of(post: dict[str, str], limit: int | None = None) -> str:
    """取文章摘要（limit 为 None 时截 80 字）。"""
    if limit == None:
        limit = 80
    return post.get("summary", "")[:limit]

def _inj_urlopen_no_timeout_1(url):
    import urllib.request

    with urllib.request.urlopen(url) as resp:
        return resp.read()

def _inj_hardcoded_secret_2():
    access_token = "sk-live-9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"
    return access_token

def _inj_mutable_default_3(items, bucket=[]):
    for item in items:
        bucket.append(item)
    return bucket
