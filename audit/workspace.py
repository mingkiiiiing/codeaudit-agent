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
    ".codeaudit",  # 本工具自身的默认工作区（复跑时防止副本嵌套副本，R3-1 根因修复）
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

# W19-F3：package-lock.json / poetry.lock 自本集合摘出（改为受控放行）——
# depcheck 的传递依赖 CVE 扫描需要它们进工作副本；巨型 lock 的性能防护由
# 复制阶段的 5MB 上限承担（见 is_ignored 与 ingest 复制循环）。
DEFAULT_IGNORE_FILES = {".DS_Store"}
# W19-F3：放行进工作副本的 lock 清单文件（depcheck 传递依赖扫描的数据源）
LOCK_MANIFEST_NAMES = {"package-lock.json", "poetry.lock"}
LOCK_COPY_MAX_BYTES = 5 * 1024 * 1024  # 与 depcheck.scanner 的 LOCK_MAX_BYTES 同口径

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


def is_ignored_copy(rel_path: Path, src_file: Path | None = None) -> bool:
    """工作副本**复制阶段**的忽略判定（W19-F3）。

    与 is_ignored 的差异：package-lock.json / poetry.lock 不再一刀切忽略——
    depcheck 传递依赖扫描需要它们；≤ LOCK_COPY_MAX_BYTES 放行，超限仍忽略
    （性能防护，与 depcheck.scanner 的解析上限同口径）。其余语义逐字一致。
    """
    name = rel_path.parts[-1] if rel_path.parts else ""
    if name in LOCK_MANIFEST_NAMES:
        if src_file is None:
            try:
                return rel_path.stat().st_size > LOCK_COPY_MAX_BYTES
            except OSError:
                return True
        try:
            return src_file.stat().st_size > LOCK_COPY_MAX_BYTES
        except OSError:
            return True
    return is_ignored(rel_path)


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
