"""金标集（Golden Set）：数据结构、JSONL 读写与 Markdown 管道表格解析。

金标条目格式见 docs/04 §2.2：

    {"project": "demo_proj", "file": "app/services/orders.py",
     "line_start": 88, "line_end": 90,
     "category": "bug", "severity": "high",
     "description": "get_user 可能返回 None 未判空",
     "origin": "git-history | bugsinpy | manual | injected"}

category/severity 使用 audit.models 中 Category/Severity 枚举的字符串值
（bug|security|performance|style 与 critical|high|medium|low）。
"""

from __future__ import annotations

import dataclasses
import json
import re
import warnings
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from audit.models import Category, Severity

VALID_CATEGORIES: frozenset[str] = frozenset(c.value for c in Category)
VALID_SEVERITIES: frozenset[str] = frozenset(s.value for s in Severity)
VALID_ORIGINS: frozenset[str] = frozenset({"git-history", "bugsinpy", "manual", "injected"})

# "行"列：单个行号（"11"）或闭区间（"10-12"，容忍全角连字符/波浪号/空格）
_LINE_SPAN_RE = re.compile(r"^\s*(\d+)\s*(?:[-\u2013\u2014~\u2014]\s*(\d+))?\s*$")


@dataclass
class GoldenIssue:
    """一条金标缺陷。category/severity 存枚举字符串值，序列化即明文。"""

    project: str = ""
    file: str = ""  # 相对项目根的 posix 路径
    line_start: int = 0
    line_end: int = 0
    category: str = "bug"
    severity: str = "medium"
    description: str = ""
    origin: str = "manual"  # git-history | bugsinpy | manual | injected

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的 dict（枚举字段即字符串）。"""
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GoldenIssue":
        """从 dict 宽容构建：缺失字段用默认值，多余键忽略。"""
        kwargs = {f.name: data[f.name] for f in fields(cls) if f.name in data}
        return cls(**kwargs)


def _warn(message: str, warnings_out: list[str] | None = None) -> None:
    """收集一条解析警告：追加到调用方列表（如有），并经 warnings 模块外发。"""
    if warnings_out is not None:
        warnings_out.append(message)
    warnings.warn(message, stacklevel=3)


def _to_posix(path: str) -> str:
    """把 Windows 反斜杠路径规范为 posix 风格（匹配侧再做 ./ 归一）。"""
    return path.replace("\\", "/")


def parse_line_span(text: str) -> tuple[int, int] | None:
    """解析"行"列文本："10" → (10, 10)；"10-12" → (10, 12)。无法解析返回 None。"""
    match = _LINE_SPAN_RE.match(text)
    if match is None:
        return None
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else start
    if end < start:
        start, end = end, start
    return start, end


def from_markdown_table(
    path: Path,
    project: str = "demo_proj",
    warnings_out: list[str] | None = None,
) -> list[GoldenIssue]:
    """解析 GOLDEN_ISSUES.md 式的管道表格为金标列表。

    列顺序：# / 文件 / 行 / 类别 / 严重度 / 描述。表头行（首列为 ``#``）
    与分隔行（``---``）自动跳过；行号支持 "10" 或 "10-12"。
    解析失败/字段非法的行跳过并记入 warnings_out。
    """
    items: list[GoldenIssue] = []
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells:
            continue
        first = cells[0]
        if first in {"", "#"} or set(first) <= set("-: "):  # 表头 / 分隔行
            continue
        if len(cells) < 6:
            _warn(f"{Path(path).name}:{lineno} 列数不足 6，跳过：{line[:60]!r}", warnings_out)
            continue
        file_cell = _to_posix(cells[1])
        span = parse_line_span(cells[2])
        category = cells[3].lower()
        severity = cells[4].lower()
        description = "|".join(cells[5:]).strip()  # 描述里若含 "|" 则还原
        problems: list[str] = []
        if not file_cell:
            problems.append("文件为空")
        if span is None:
            problems.append(f"行号无法解析：{cells[2]!r}")
        if category not in VALID_CATEGORIES:
            problems.append(f"类别非法：{cells[3]!r}")
        if severity not in VALID_SEVERITIES:
            problems.append(f"严重度非法：{cells[4]!r}")
        if problems:
            _warn(f"{Path(path).name}:{lineno} { '；'.join(problems) }，跳过该行", warnings_out)
            continue
        assert span is not None  # 供类型检查收窄
        items.append(
            GoldenIssue(
                project=project,
                file=file_cell,
                line_start=span[0],
                line_end=span[1],
                category=category,
                severity=severity,
                description=description,
                origin="manual",
            )
        )
    return items


def load_goldset(path: Path, warnings_out: list[str] | None = None) -> list[GoldenIssue]:
    """从 JSONL 文件加载金标集：逐行 json.loads，坏行跳过并收集 warnings。

    校验规则：file 非空、category/severity 为合法枚举字符串、行号满足
    1 <= line_start <= line_end。不满足的行整体跳过。
    """
    items: list[GoldenIssue] = []
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            _warn(f"{Path(path).name}:{lineno} JSON 解析失败，跳过：{exc}", warnings_out)
            continue
        if not isinstance(data, dict):
            _warn(f"{Path(path).name}:{lineno} 非对象行，跳过", warnings_out)
            continue
        item = GoldenIssue.from_dict(data)
        problems: list[str] = []
        if not item.file:
            problems.append("file 为空")
        if item.category not in VALID_CATEGORIES:
            problems.append(f"category 非法：{item.category!r}")
        if item.severity not in VALID_SEVERITIES:
            problems.append(f"severity 非法：{item.severity!r}")
        if item.line_start < 1 or item.line_end < item.line_start:
            problems.append(f"行号非法：{item.line_start}-{item.line_end}")
        if problems:
            _warn(f"{Path(path).name}:{lineno} { '；'.join(problems) }，跳过该行", warnings_out)
            continue
        items.append(item)
    return items


def save_goldset(items: list[GoldenIssue], path: Path) -> None:
    """把金标列表写为 JSONL（UTF-8，ensure_ascii=False），父目录自动创建。"""
    path = Path(path)
    if path.parent != Path(""):
        path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(item.to_dict(), ensure_ascii=False) for item in items]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
