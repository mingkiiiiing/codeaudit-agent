"""接入过滤规则：内置忽略 + .gitignore 简易解析。

复用 audit/workspace.py 的 is_ignored / DEFAULT_IGNORE_DIRS（契约），
在其上叠加：
  - 额外目录（__MACOSX 等 zip 垃圾目录）
  - 文件名 glob（*.min.js 等）
  - .gitignore 的"简单实现"：目录名与 glob 行，不做完整 gitignore 语义
    （不支持 ! 否定、不支持 ** 与锚定语义的精确复刻）。
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from audit.workspace import is_ignored

# zip 包常见垃圾目录
EXTRA_IGNORE_DIRS = {"__MACOSX"}

# 额外按文件名 glob 忽略的模式（docs/02 Stage1：*.min.js 等）
IGNORE_FILE_GLOBS = ("*.min.js", "*.min.css", "*.map")

GITIGNORE_NAME = ".gitignore"


class GitignoreSet:
    """一个工作副本内全部 .gitignore 文件的"简单"合并视图。

    简化语义（够用即可）：
      - `name/` 结尾斜杠 → 按目录名匹配任意层级
      - 含 `/` 的模式（如 `build/out`）→ 对相对路径做 fnmatch（含前缀目录匹配）
      - 纯名字/glob（如 `*.log`、`data`）→ 匹配文件名；无通配符时还匹配任意层级同名目录
      - `!` 否定行直接忽略（本实现不做否定）
      - 注释与空行跳过
    """

    def __init__(self) -> None:
        self._names: set[str] = set()  # 纯名字（匹配任意层级的文件名/目录名）
        self._globs: list[str] = []  # 含通配符的名字 glob，如 *.log
        self._path_globs: list[str] = []  # 含 / 的路径 glob

    @classmethod
    def collect(cls, src_root: Path) -> "GitignoreSet":
        """收集 src_root 下所有 .gitignore 的规则。

        W19-F4 作用域修复：嵌套 .gitignore 的规则只作用于**其所在目录子树**
        （对齐 git 语义）。此前"统一视为相对根目录"——`bench/stress/data/.gitignore`
        这类全排除型嵌套文件（`*` + `!.gitignore`）会把整库全部文件误判为忽略，
        导致 ingest 空副本、pr-audit 自审计静默失效。
        """
        gs = cls()
        for ignore_file in sorted(src_root.rglob(GITIGNORE_NAME)):
            if not ignore_file.is_file():
                continue
            try:
                text = ignore_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scope = ""
            try:
                parent = ignore_file.parent.relative_to(src_root)
                scope = "" if parent.as_posix() == "." else parent.as_posix()
            except ValueError:
                scope = ""
            for raw in text.splitlines():
                gs.add_line(raw, scope=scope)
        return gs

    def add_line(self, raw: str, scope: str = "") -> None:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            return
        line = line.rstrip("/")  # 尾部斜杠仅表示"目录"，简化为名字匹配
        if not line:
            return
        if scope:
            # W19-F4：嵌套规则改写为相对根的路径 glob（限定在所在目录子树）。
            # fnmatch 的 `*` 可跨 `/`，故 `scope/**/name` 能同时覆盖直接子项与
            # 深层子孙；`!` 否定行维持既有"忽略"语义（不处理否定）。
            if "/" in line:
                self._path_globs.append(f"{scope}/{line.strip('/')}")
            elif any(ch in line for ch in "*?["):
                self._path_globs.append(f"{scope}/**/{line}")
            else:
                self._path_globs.append(f"{scope}/{line}")
                self._path_globs.append(f"{scope}/**/{line}")
            return
        if "/" in line:
            self._path_globs.append(line.strip("/"))
        elif any(ch in line for ch in "*?["):
            self._globs.append(line)
        else:
            self._names.add(line)

    def matches(self, rel_path: Path) -> bool:
        """判断相对路径是否被任一 .gitignore 规则命中。"""
        parts = rel_path.parts
        if not parts:
            return False
        name = parts[-1]
        # 纯名字：文件名或任意层级目录名
        if any(p in self._names for p in parts):
            return True
        # 文件名 glob
        for g in self._globs:
            if fnmatch.fnmatch(name, g):
                return True
        # 路径 glob：整路径或任一父目录前缀
        posix = rel_path.as_posix()
        for g in self._path_globs:
            if fnmatch.fnmatch(posix, g):
                return True
            if posix.startswith(g + "/"):
                return True
            # 目录段匹配：a/build/... 命中 build/out 之类模式时按段比对
            for i in range(1, len(parts)):
                if fnmatch.fnmatch("/".join(parts[:i]), g):
                    return True
        return False


def should_ignore(rel_path: Path, gitignore: GitignoreSet | None = None) -> bool:
    """工作副本内文件的最终忽略判定（契约 is_ignored + 本模块扩展）。"""
    if is_ignored(rel_path):
        return True
    parts = rel_path.parts
    if any(p in EXTRA_IGNORE_DIRS for p in parts[:-1]):
        return True
    name = parts[-1] if parts else ""
    for g in IGNORE_FILE_GLOBS:
        if fnmatch.fnmatch(name, g):
            return True
    if gitignore is not None and gitignore.matches(rel_path):
        return True
    return False
