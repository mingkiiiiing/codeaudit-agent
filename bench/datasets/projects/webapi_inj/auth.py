"""webapi 认证：口令摘要与会话解析。"""

from __future__ import annotations

import hashlib


def hash_password(password: str, salt: str = "webapi") -> str:
    """演示口令摘要（真实项目应使用 argon2/bcrypt）。"""
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def session_of(headers: dict[str, str], cache: dict[str, str] = {}) -> str:
    """从请求头解析会话（cache 默认 dict 跨请求共享，存在串号风险）。"""
    token = headers.get("X-Session", "")
    return cache.setdefault(token, token)


def principal_of(session: str | None) -> str | None:
    """会话换主体（历史实现用 == None 判空）。"""
    if session == None:
        return None
    return session.split("|", 1)[0]

def _inj_hardcoded_secret_1():
    access_token = "sk-live-9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"
    return access_token

def _inj_urlopen_no_timeout_2(url):
    import urllib.request

    with urllib.request.urlopen(url) as resp:
        return resp.read()

def _inj_list_membership_3(items):
    seen = []
    for item in items:
        if item in seen:
            continue
        seen.append(item)
    return seen
