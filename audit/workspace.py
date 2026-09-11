"""工作区上下文（契约文件，勿改）：审计任务的工作副本视图。

WorkspaceContext 本身不负责创建副本（那是 Stage1 ingest 的职责），
只提供"给定工作副本后的统一读写视图"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

DEFAULT_IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
    "env",
    ".idea",
    ".vscode",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
    "coverage",
}

DEFAULT_IGNORE_FILES = {".DS_Store", "package-lock.json", "poetry.lock"}

BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".zip",
    ".gz", ".tar", ".7z", ".exe", ".dll", ".so", ".dylib", ".woff",
    ".woff2", ".ttf", ".eot", ".mp4", ".mp3", ".class", ".jar",
}


def is_ignored(rel_path: Path) -> bool:
    parts = rel_path.parts
    if not parts:
        return True
    if any(p in DEFAULT_IGNORE_DIRS for p in parts[:-1]):
        return True
    name = parts[-1]
    if name in DEFAULT_IGNORE_FILES:
        return True
    if rel_path.suffix.lower() in BINARY_SUFFIXES:
        return True
    return False


@dataclass
class WorkspaceContext:
    audit_id: str
    src_root: Path  # 工作副本根目录（构造时统一归一为绝对路径）
    work_root: Path  # 本任务工作区根（src_root 的父目录）
    db_path: Path  # SQLite 索引库路径
    index: Any = None  # IndexStore | None，由编排层在 build 后注入
    language: str = ""  # 主语言，ingest 填充
    manifests: list = field(default_factory=list)  # list[FileManifest]

    def __post_init__(self) -> None:
        # 契约 v1.1：根路径一律绝对化。source_files() 产出绝对路径，任何
        # 相对 src_root 的行为都会让下游 rel()/read_file_text() 产生路径翻倍。
        self.src_root = Path(self.src_root).resolve()
        self.work_root = Path(self.work_root).resolve()
        self.db_path = Path(self.db_path).resolve()

    # -------- 路径视图

    def abs_path(self, rel_path: str | Path) -> Path:
        return self.src_root / rel_path

    def rel(self, path: str | Path) -> str:
        """把绝对/相对路径规范化为相对 src_root 的 posix 字符串。"""
        p = Path(path)
        if not p.is_absolute():
            p = self.src_root / p
        return p.resolve().relative_to(self.src_root.resolve()).as_posix()

    # -------- 读文件

    def source_files(self, languages: list[str] | None = None) -> Iterator[Path]:
        """遍历源文件（已过滤忽略项与二进制），产出绝对路径；可按语言过滤。"""
        for path in sorted(self.src_root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.src_root)
            if is_ignored(rel):
                continue
            if languages:
                from audit.utils import guess_language

                if guess_language(rel) not in languages:
                    continue
            yield path

    def read_lines(self, rel_path: str | Path, start: int = 1, end: int | None = None) -> list[str]:
        """按 1-based 闭区间读行（去换行符）。文件不存在抛 FileNotFoundError。"""
        text = self.read_file_text(rel_path)
        lines = text.splitlines()
        s = max(1, start)
        e = len(lines) if end is None else min(len(lines), max(start, end))
        return lines[s - 1 : e]

    def read_file_text(self, rel_path: str | Path) -> str:
        path = self.abs_path(rel_path)
        for encoding in ("utf-8", "gbk"):
            try:
                return path.read_text(encoding=encoding)
            except UnicodeDecodeError:
                continue
        return path.read_text(encoding="utf-8", errors="replace")

    def line_count(self, rel_path: str | Path) -> int:
        return len(self.read_file_text(rel_path).splitlines())
