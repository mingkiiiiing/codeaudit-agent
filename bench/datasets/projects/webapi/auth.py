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
