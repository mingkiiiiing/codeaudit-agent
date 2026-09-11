"""公共工具函数：哈希、截断、ID 生成、JSON 抽取等。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_EXTENSION_LANGUAGE = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
}


def guess_language(path: str | Path) -> str | None:
    """按扩展名粗判语言（T2 可做内容级二次确认）。"""
    return _EXTENSION_LANGUAGE.get(Path(path).suffix.lower())


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def count_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def truncate(text: str, max_chars: int = 2000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n...[截断，共 {len(text)} 字符]"


def truncate_lines(lines: list[str], max_lines: int = 200) -> list[str]:
    if len(lines) <= max_lines:
        return lines
    return lines[:max_lines] + [f"...[截断，共 {len(lines)} 行]"]


def make_id(prefix: str, n: int) -> str:
    """生成形如 ISS-0042 的稳定编号。"""
    return f"{prefix}-{n:04d}"


def issue_fingerprint(issue: Any) -> str:
    """问题指纹（契约 v1.4）：基线匹配的唯一依据。

    由 文件|行区间|类别|标题前 60 字 决定，与描述措辞/置信度无关——
    同一位置同一类问题的指纹在多次审计间保持稳定。
    """
    category = getattr(issue.category, "value", issue.category)
    key = f"{issue.file}|{issue.line_start}|{issue.line_end}|{category}|{(issue.title or '')[:60]}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def new_audit_id() -> str:
    return uuid.uuid4().hex[:12]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json(text: str) -> Any:
    """从 LLM 返回文本中尽力抽取 JSON（容忍代码围栏/前后杂文）。

    解析失败抛 ValueError，由调用方决定重试或降级。
    """
    if text is None:
        raise ValueError("empty response")
    candidates: list[str] = []
    fenced = _CODE_FENCE_RE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text.strip())
    # 尝试截取首个 { 或 [ 到最后一个 } 或 ]
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = text.find(opener), text.rfind(closer)
        if 0 <= i < j:
            candidates.append(text[i : j + 1])
    for cand in candidates:
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
    raise ValueError(f"no parsable JSON in response: {truncate(text, 200)!r}")
