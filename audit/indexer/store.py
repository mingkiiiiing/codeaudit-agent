"""SQLite 索引存储：audit.indexer.base.IndexStore 的标准实现。

表结构（参考 docs/02 §2，call_edges 增补 line 列用于引用定位）：
  files(path PK, language, loc, sha256, parsed_ok)
  symbols(id PK, file, kind, name, line_start, line_end, signature)
  imports(file, module, imported_name, alias)
  calls(file, caller_symbol, callee_name, line)          -- 解析前原始调用点
  call_edges(caller_file, caller_symbol, callee_name, callee_file, callee_symbol, resolved, line)
  slices(file, symbol, line_start, line_end, signature, kind)

增量：build() 时文件 sha256 未变 → 跳过重解析（调用点/符号等行保留），
每次 build 基于全量 calls 表重建 call_edges（纯字典查找，代价可忽略）。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from audit.indexer.base import IndexStore
from audit.indexer.js_extract import extract_js_ts
from audit.indexer.parsers import SUPPORTED_LANGUAGES, parse_source
from audit.indexer.py_extract import FileIndex, extract_python
from audit.models import FunctionSlice, Reference, Symbol
from audit.utils import count_lines, guess_language, sha256_file
from audit.workspace import WorkspaceContext

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    language TEXT NOT NULL,
    loc INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL DEFAULT '',
    parsed_ok INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS symbols (
    id TEXT PRIMARY KEY,
    file TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    line_start INTEGER NOT NULL DEFAULT 0,
    line_end INTEGER NOT NULL DEFAULT 0,
    signature TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_symbols_file ON symbols(file);
CREATE INDEX IF NOT EXISTS ix_symbols_name ON symbols(name);
CREATE TABLE IF NOT EXISTS imports (
    file TEXT NOT NULL,
    module TEXT NOT NULL,
    imported_name TEXT NOT NULL DEFAULT '',
    alias TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_imports_file ON imports(file);
CREATE TABLE IF NOT EXISTS calls (
    file TEXT NOT NULL,
    caller_symbol TEXT NOT NULL DEFAULT '',
    callee_name TEXT NOT NULL,
    line INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_calls_file ON calls(file);
CREATE TABLE IF NOT EXISTS call_edges (
    caller_file TEXT NOT NULL,
    caller_symbol TEXT NOT NULL DEFAULT '',
    callee_name TEXT NOT NULL,
    callee_file TEXT NOT NULL DEFAULT '',
    callee_symbol TEXT NOT NULL DEFAULT '',
    resolved INTEGER NOT NULL DEFAULT 0,
    line INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_edges_caller ON call_edges(caller_file, caller_symbol);
CREATE INDEX IF NOT EXISTS ix_edges_callee ON call_edges(callee_name);
CREATE TABLE IF NOT EXISTS slices (
    file TEXT NOT NULL,
    symbol TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    signature TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'function'
);
CREATE INDEX IF NOT EXISTS ix_slices_file ON slices(file);
"""

_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_JS_INDEX_BASENAMES = tuple(f"index{ext}" for ext in _JS_EXTS)

# 派生表清单（_clear_derived / _clear_derived_many 共用；顺序无语义）
_DERIVED_TABLES = ("symbols", "imports", "calls", "slices")


@dataclass
class _Target:
    """跨文件解析结果。"""

    file: str
    symbol: str = ""


@dataclass
class _AliasEntry:
    """import 别名绑定：模块绑定或符号绑定。"""

    kind: str  # "module" | "symbol"
    module: str  # 模块名（python 点分 / js 原始说明符）
    name: str = ""  # symbol 绑定时的符号名


def _like_escape(name: str) -> str:
    r"""转义 LIKE 通配符（R1-23）：符号名含 % / _ / \ 时不改变匹配语义。

    配合 SQL 端 ``ESCAPE '\'`` 使用，返回转义后的 pattern 片段。
    """
    return name.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def create_index(workspace: WorkspaceContext, db_path: Path | None = None) -> IndexStore:
    """工厂：为 workspace 创建 SQLite 索引存储（默认路径 workspace.db_path）。"""
    return SqliteIndexStore(workspace, db_path)


class SqliteIndexStore(IndexStore):
    """基于 SQLite 的全库符号/依赖/切片索引。"""

    def __init__(self, workspace: WorkspaceContext, db_path: Path | None = None) -> None:
        self._ws = workspace
        self._db_path = Path(db_path) if db_path is not None else Path(workspace.db_path)
        if self._db_path.parent and not self._db_path.parent.exists():
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._parses_this_build = 0
        # 解析缓存（build 的 _resolve_edges 阶段填充）
        self._py_files_by_module: dict[str, str] = {}
        self._symbols_by_file: dict[str, set[str]] = {}
        self._js_files: set[str] = set()
        # R1-8 修复：预取 language 映射 + 按 caller 缓存 import 别名表，
        # 消除 _resolve_edges 对每行调用边的 2 次 SQL（压测实测 build 省 10.2%）
        self._lang_map: dict[str, str] = {}
        self._alias_cache: dict[str, dict[str, "_AliasEntry"]] = {}
        # R4-5：close 幂等守卫标记
        self._closed = False

    # ---------------------------------------------------------------- build

    def build(self) -> None:
        """扫描 workspace 构建索引（幂等、增量）。"""
        self._parses_this_build = 0
        seen: set[str] = set()
        # Dogfood（PY-IO-IN-LOOP 清偿）：既有 sha 映射一次性预取，
        # 消除 build 循环内逐文件的 SELECT（行为等价：循环期间文件表不再变化）
        existing = {
            row["path"]: (row["sha256"], row["language"])
            for row in self._conn.execute("SELECT path, sha256, language FROM files")
        }
        failed_rows: list[tuple[str, str, int, str]] = []
        failed_rels: list[str] = []
        for path in self._ws.source_files():
            rel = self._ws.rel(path)
            language = guess_language(rel)
            if language not in SUPPORTED_LANGUAGES:
                continue
            seen.add(rel)
            try:
                sha = sha256_file(path)
                data = path.read_bytes()
            except OSError:
                continue
            if existing.get(rel) == (sha, language):
                continue  # 增量：文件未变，跳过重解析
            self._parses_this_build += 1
            tree, parsed_ok = parse_source(language, data)
            if tree is None:
                failed_rels.append(rel)
                failed_rows.append(
                    (rel, language, count_lines(data.decode("utf-8", errors="replace")), sha)
                )
                continue
            if language == "python":
                fx = extract_python(data, rel)
            else:
                js_lang = "typescript" if language == "typescript" else "javascript"
                fx = extract_js_ts(data, rel, language=js_lang)
            self._write_file_index(rel, language, count_lines(data.decode("utf-8", errors="replace")), sha, parsed_ok, fx)
        # 解析失败的文件：派生行清理 + files 行落盘批量写（Dogfood：移出扫描循环）
        if failed_rels:
            self._clear_derived_many(failed_rels)
            self._conn.executemany(
                "INSERT OR REPLACE INTO files(path, language, loc, sha256, parsed_ok) VALUES(?,?,?,?,0)",
                failed_rows,
            )
        self._delete_stale(seen)
        self._resolve_edges()
        self._conn.commit()

    def _clear_derived(self, rel: str) -> None:
        self._clear_derived_many([rel])

    def _clear_derived_many(self, rels: list[str]) -> None:
        """批量清理 rels 的派生行（Dogfood：executemany 化，消除逐行 DELETE 循环）。"""
        if not rels:
            return
        params = [(rel,) for rel in rels]
        for table in _DERIVED_TABLES:
            # 标识符 table 来自模块级内部常量白名单 _DERIVED_TABLES，非外部输入
            self._conn.executemany(f"DELETE FROM {table} WHERE file = ?", params)  # codeaudit: ignore[PY-SQL-INJECTION]  # 标识符为内部常量白名单

    def _write_file_index(self, rel: str, language: str, loc: int, sha: str, parsed_ok: bool, fx: FileIndex) -> None:
        self._clear_derived(rel)
        self._conn.execute(
            "INSERT OR REPLACE INTO files(path, language, loc, sha256, parsed_ok) VALUES(?,?,?,?,?)",
            (rel, language, loc, sha, 1 if parsed_ok else 0),
        )
        self._conn.executemany(
            "INSERT OR REPLACE INTO symbols(id, file, kind, name, line_start, line_end, signature)"
            " VALUES(?,?,?,?,?,?,?)",
            [
                (s.id or f"{rel}::{s.name}", s.file, s.kind, s.name, s.line_start, s.line_end, s.signature)
                for s in fx.symbols
            ],
        )
        self._conn.executemany(
            "INSERT INTO imports(file, module, imported_name, alias) VALUES(?,?,?,?)",
            [(rel, i.module, i.name, i.alias) for i in fx.imports],
        )
        self._conn.executemany(
            "INSERT INTO calls(file, caller_symbol, callee_name, line) VALUES(?,?,?,?)",
            [(rel, c.caller_symbol, c.callee_name, c.line) for c in fx.calls],
        )
        self._conn.executemany(
            "INSERT INTO slices(file, symbol, line_start, line_end, signature, kind) VALUES(?,?,?,?,?,?)",
            [
                (rel, s.name, s.line_start, s.line_end, s.signature, s.kind)
                for s in fx.symbols
                if s.kind in ("function", "method")
            ],
        )

    def _delete_stale(self, seen: set[str]) -> None:
        stale = [r["path"] for r in self._conn.execute("SELECT path FROM files") if r["path"] not in seen]
        if not stale:
            return
        self._conn.executemany("DELETE FROM files WHERE path = ?", [(rel,) for rel in stale])
        self._clear_derived_many(stale)

    # ---------------------------------------------------------------- 跨文件解析

    def _language_of(self, rel: str) -> str:
        try:
            return self._lang_map[rel]
        except KeyError:
            pass
        row = self._conn.execute("SELECT language FROM files WHERE path = ?", (rel,)).fetchone()
        lang = row["language"] if row else ""
        self._lang_map[rel] = lang
        return lang

    def _py_module_of(self, rel: str) -> str:
        p = rel[:-3] if rel.endswith(".py") else rel
        parts = PurePosixPath(p).parts
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)

    def _resolve_relative_module(self, module: str, caller_file: str) -> str:
        """把相对模块名（如 .mod / ..pkg）解析为绝对模块名。"""
        if not module.startswith("."):
            return module
        ndots = len(module) - len(module.lstrip("."))
        rest = module[ndots:]
        parts = PurePosixPath(caller_file).parts
        pkg = parts[:-1] if parts else ()
        up = ndots - 1
        if up > len(pkg):
            return ""
        base = pkg[: len(pkg) - up] if up else pkg
        prefix = ".".join(base)
        if rest:
            return f"{prefix}.{rest}" if prefix else rest
        return prefix

    def _aliases(self, caller_file: str) -> dict[str, _AliasEntry]:
        """caller 文件的 import 别名绑定表（含通配导入展开）。

        结果按 caller_file 缓存：imports 表在 build 期间不变，且调用方只读
        返回值（R1-8：消除逐 call 行的重复 SQL）。
        """
        cached = self._alias_cache.get(caller_file)
        if cached is not None:
            return cached
        entries: dict[str, _AliasEntry] = {}
        for row in self._conn.execute("SELECT module, imported_name, alias FROM imports WHERE file = ?", (caller_file,)):
            module, name, alias = row["module"], row["imported_name"], row["alias"]
            if name == "":  # import a.b [as x]
                if alias:
                    entries[alias] = _AliasEntry("module", module)
                else:
                    # import a.b 绑定顶层包名 a；import os 绑定 os
                    first = module.split(".")[0]
                    entries[first] = _AliasEntry("module", first)
                    if "." in module:
                        entries.setdefault(module, _AliasEntry("module", module))
            elif name == "*":  # from mod import * → 展开 mod 的顶层符号
                mod = self._resolve_relative_module(module, caller_file)
                target = self._module_file(mod)
                if target:
                    for sym in self._symbols_in_file(target):
                        short = sym.split(".")[-1]
                        entries.setdefault(short, _AliasEntry("symbol", mod, short))
            else:
                entries[alias or name] = _AliasEntry("symbol", module, name)
        self._alias_cache[caller_file] = entries
        return entries

    def _module_file(self, module: str) -> str | None:
        return self._py_files_by_module.get(module)

    def _resolve_js_spec(self, spec: str, caller_file: str) -> str | None:
        """解析 js/ts 相对导入说明符为工作副本内文件；非相对导入返回 None。"""
        if not spec.startswith(("./", "../", "/")):
            return None
        caller_dir = PurePosixPath(caller_file).parent
        raw = PurePosixPath(spec)
        parts: list[str] = []
        if raw.is_absolute():
            parts = [p for p in raw.parts[1:]]
        else:
            parts = [p for p in caller_dir.parts if p not in ("", ".")]
            for part in raw.parts:
                if part in ("", "."):
                    continue
                if part == "..":
                    if not parts:
                        return None  # 越出工作副本根
                    parts.pop()
                else:
                    parts.append(part)
        base = "/".join(parts)
        candidates = [base + ext for ext in _JS_EXTS] if not any(base.endswith(e) for e in _JS_EXTS) else [base]
        candidates += [f"{base}/{name}" for name in _JS_INDEX_BASENAMES]
        for cand in candidates:
            if cand in self._js_files:
                return cand
        return None

    def _has_symbol(self, file: str, short: str) -> bool:
        names = self._symbols_by_file.get(file, set())
        return short in names or any(n.endswith("." + short) for n in names)

    def _resolve_py_call(self, callee: str, caller_file: str) -> _Target | None:
        entries = self._aliases(caller_file)
        if "." not in callee:
            entry = entries.get(callee)
            if entry is None:
                return None
            return self._resolve_entry(entry, caller_file)
        head, rest = callee.split(".", 1)
        entry = entries.get(head)
        if entry is None:
            return None
        if entry.kind != "module":
            return self._resolve_entry(entry, caller_file)
        module = self._resolve_relative_module(entry.module, caller_file)
        parts = rest.split(".")
        # rest 前缀逐级尝试作为子模块：a.b.c.f → a.b.c / a.b
        for i in range(len(parts) - 1, 0, -1):
            sub = self._module_file(f"{module}.{'.'.join(parts[:i])}" if module else ".".join(parts[:i]))
            if sub is not None:
                tail = ".".join(parts[i:])
                if self._has_symbol(sub, tail):
                    return _Target(sub, tail)
                return _Target(sub, "")
        target = self._module_file(module)
        if target is not None:
            if self._has_symbol(target, rest):
                return _Target(target, rest)
            return _Target(target, "")
        return None

    def _resolve_entry(self, entry: _AliasEntry, caller_file: str) -> _Target | None:
        module = self._resolve_relative_module(entry.module, caller_file)
        if entry.kind == "module":
            target = self._module_file(module)
            return _Target(target, "") if target else None
        # symbol 绑定：优先文件内符号，其次同名子模块
        target = self._module_file(module)
        if target is not None and self._has_symbol(target, entry.name):
            return _Target(target, entry.name)
        sub = self._module_file(f"{module}.{entry.name}" if module else entry.name)
        if sub is not None:
            return _Target(sub, "")
        if target is not None:
            return _Target(target, entry.name)
        return None

    def _resolve_js_call(self, callee: str, caller_file: str) -> _Target | None:
        entries: dict[str, tuple[str, str]] = {}
        for row in self._conn.execute(
            "SELECT module, imported_name, alias FROM imports WHERE file = ?", (caller_file,)
        ):
            name, alias = row["imported_name"], row["alias"]
            if name in ("", "default"):
                bound = alias or ""
                if bound:
                    entries.setdefault(bound, (row["module"], ""))
                continue
            bound = alias or name
            entries.setdefault(bound, (row["module"], "" if name == "*" else name))
        if "." in callee:
            head, rest = callee.split(".", 1)
            entry = entries.get(head)
            if entry is None:
                return None
            target = self._resolve_js_spec(entry[0], caller_file)
            if target is None:
                return None
            return _Target(target, rest if self._has_symbol(target, rest) else "")
        entry = entries.get(callee)
        if entry is None:
            return None
        target = self._resolve_js_spec(entry[0], caller_file)
        if target is None:
            return None
        sym = entry[1]
        if sym and not self._has_symbol(target, sym):
            sym = ""
        return _Target(target, sym)

    def _resolve_edges(self) -> None:
        """基于全量 calls 表重建 call_edges（增量安全：未变文件行保留）。"""
        # R4-2：_lang_map / _alias_cache 是 build 期预取缓存，跨 build 必须重置，
        # 否则增量 build 会拿上一轮的 imports 别名表解析调用边（陈旧 imports）。
        self._lang_map.clear()
        self._alias_cache.clear()
        self._py_files_by_module = {}
        self._symbols_by_file: dict[str, set[str]] = {}
        self._js_files: set[str] = set()
        for row in self._conn.execute("SELECT path, language FROM files"):
            rel, lang = row["path"], row["language"]
            if lang == "python":
                self._py_files_by_module[self._py_module_of(rel)] = rel
            elif lang in ("javascript", "typescript"):
                self._js_files.add(rel)
        for row in self._conn.execute("SELECT file, name FROM symbols"):
            self._symbols_by_file.setdefault(row["file"], set()).add(row["name"])

        self._conn.execute("DELETE FROM call_edges")
        rows = self._conn.execute("SELECT file, caller_symbol, callee_name, line FROM calls").fetchall()
        edges: list[tuple] = []
        for row in rows:
            caller_file, caller_symbol, callee_name, line = (
                row["file"],
                row["caller_symbol"],
                row["callee_name"],
                row["line"],
            )
            lang = self._language_of(caller_file)
            target: _Target | None = None
            if lang == "python":
                target = self._resolve_py_call(callee_name, caller_file)
            elif lang in ("javascript", "typescript"):
                target = self._resolve_js_call(callee_name, caller_file)
            if target is not None and target.file == caller_file:
                continue  # 同文件调用：源码直接可见，不建边
            if target is not None:
                edges.append((caller_file, caller_symbol, callee_name, target.file, target.symbol, 1, line))
                continue
            # 解析失败：若被调简名在本文件内有定义，视为同文件调用，不建边
            if self._has_symbol(caller_file, callee_name.split(".")[-1]):
                continue
            edges.append((caller_file, caller_symbol, callee_name, "", "", 0, line))
        self._conn.executemany(
            "INSERT INTO call_edges(caller_file, caller_symbol, callee_name, callee_file, callee_symbol, resolved, line)"
            " VALUES(?,?,?,?,?,?,?)",
            edges,
        )

    # ---------------------------------------------------------------- 查询

    def close(self) -> None:
        """提交并关闭连接（R4-5：幂等——重复调用不再抛 sqlite3.ProgrammingError）。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.commit()
        finally:
            self._conn.close()

    def symbols_for_file(self, rel_path: str) -> list[Symbol]:
        rows = self._conn.execute(
            "SELECT * FROM symbols WHERE file = ? ORDER BY line_start, name", (self._norm(rel_path),)
        ).fetchall()
        return [self._row_symbol(r) for r in rows]

    def all_symbols(self) -> list[Symbol]:
        rows = self._conn.execute("SELECT * FROM symbols ORDER BY file, line_start, name").fetchall()
        return [self._row_symbol(r) for r in rows]

    def get_symbol(self, name: str, file_hint: str | None = None) -> Symbol | None:
        hint = self._norm(file_hint) if file_hint else None
        rows = self._conn.execute(
            "SELECT * FROM symbols WHERE name = ? ORDER BY file, line_start", (name,)
        ).fetchall()
        if not rows:
            rows = self._conn.execute(
                r"SELECT * FROM symbols WHERE name LIKE ? ESCAPE '\' ORDER BY file, line_start",
                (f"%.{_like_escape(name)}",),
            ).fetchall()
        if hint is not None:
            hinted = [r for r in rows if r["file"] == hint]
            rows = hinted or rows
        return self._row_symbol(rows[0]) if rows else None

    def references(self, name: str, file_hint: str | None = None) -> list[Reference]:
        """引用点：调用图优先（含未解析边），为空时全文文本搜索兜底。"""
        hint = self._norm(file_hint) if file_hint else None
        sql = (
            r"SELECT caller_file, line FROM call_edges "
            r"WHERE (callee_name = ? OR callee_name LIKE ? ESCAPE '\')"
        )
        params: list = [name, f"%.{_like_escape(name)}"]
        if hint is not None:
            sql += " AND resolved = 1 AND callee_file = ?"
            params.append(hint)
        refs: list[Reference] = []
        seen: set[tuple[str, int]] = set()
        for row in self._conn.execute(sql, params):
            key = (row["caller_file"], row["line"])
            if key in seen:
                continue
            seen.add(key)
            refs.append(Reference(file=key[0], line=key[1], snippet=self._line_snippet(key[0], key[1])))
        if refs:
            return refs
        return self._text_search(name, hint)

    def call_chain(self, symbol_name: str, direction: str = "callees", depth: int = 1) -> list[str]:
        """从符号出发的调用链（每跳一条 'file:line sym -> file:line sym'）。"""
        start_rows = self._conn.execute(
            r"SELECT file, name, line_start FROM symbols "
            r"WHERE name = ? OR name LIKE ? ESCAPE '\' ORDER BY file, name",
            (symbol_name, f"%.{_like_escape(symbol_name)}"),
        ).fetchall()
        out: list[str] = []
        visited: set[tuple[str, str, int]] = set()
        for row in start_rows:
            self._walk_chain(row["file"], row["name"], row["line_start"], direction, depth, out, visited)
        return out

    def _walk_chain(
        self,
        file: str,
        name: str,
        def_line: int,
        direction: str,
        depth: int,
        out: list[str],
        visited: set[tuple[str, str, int]],
    ) -> None:
        if depth <= 0:
            return
        key = (file, name, depth)
        if key in visited:
            return
        visited.add(key)
        if direction == "callers":
            rows = self._conn.execute(
                "SELECT caller_file, caller_symbol, line FROM call_edges"
                " WHERE resolved = 1 AND callee_file = ? AND callee_symbol = ?",
                (file, name),
            ).fetchall()
            for r in rows:
                out.append(f"{r['caller_file']}:{r['line']} {r['caller_symbol']} -> {file}:{def_line} {name}")
                self._walk_chain(r["caller_file"], r["caller_symbol"], r["line"], direction, depth - 1, out, visited)
        else:  # callees
            rows = self._conn.execute(
                "SELECT callee_file, callee_symbol, callee_name, resolved, line FROM call_edges"
                " WHERE caller_file = ? AND caller_symbol = ?",
                (file, name),
            ).fetchall()
            for r in rows:
                if r["resolved"] and r["callee_file"]:
                    callee_line = self._symbol_def_line(r["callee_file"], r["callee_symbol"])
                    out.append(f"{file}:{r['line']} {name} -> {r['callee_file']}:{callee_line} {r['callee_symbol']}")
                    self._walk_chain(
                        r["callee_file"], r["callee_symbol"], callee_line, direction, depth - 1, out, visited
                    )
                else:
                    out.append(f"{file}:{r['line']} {name} -> {r['callee_name']} (unresolved)")

    def dependencies(self, target: str, direction: str = "imports") -> list[str]:
        """target 为文件相对路径或模块名；direction ∈ {imports, imported_by}。"""
        file = self._norm_module_target(target)
        if file is None:
            return []
        if direction == "imports":
            out: list[str] = []
            seen: set[str] = set()
            for row in self._conn.execute("SELECT DISTINCT module FROM imports WHERE file = ?", (file,)):
                module = row["module"]
                lang = self._language_of(file)
                resolved: str | None
                if lang == "python":
                    resolved = self._module_file(self._resolve_relative_module(module, file))
                else:
                    resolved = self._resolve_js_spec(module, file)
                item = resolved if resolved else module
                if item not in seen:
                    seen.add(item)
                    out.append(item)
            return sorted(out)
        # imported_by：全量 import 记录反查
        out = []
        seen = set()
        for row in self._conn.execute("SELECT DISTINCT file, module FROM imports"):
            src, module = row["file"], row["module"]
            if src == file:
                continue
            lang = self._language_of(src)
            if lang == "python":
                resolved = self._module_file(self._resolve_relative_module(module, src))
            else:
                resolved = self._resolve_js_spec(module, src)
            if resolved == file and src not in seen:
                seen.add(src)
                out.append(src)
        return sorted(out)

    # ---------------------------------------------------------------- 切片

    def slices_for_file(self, rel_path: str) -> list[FunctionSlice]:
        rel = self._norm(rel_path)
        rows = self._conn.execute(
            "SELECT * FROM slices WHERE file = ? ORDER BY line_start, symbol", (rel,)
        ).fetchall()
        return [
            FunctionSlice(
                file=rel,
                symbol=r["symbol"],
                line_start=r["line_start"],
                line_end=r["line_end"],
                code=self._read_code(rel, r["line_start"], r["line_end"]),
                signature=r["signature"],
                kind=r["kind"],
            )
            for r in rows
        ]

    def grouped_slices(self, rel_path: str, max_lines: int = 400) -> list[FunctionSlice]:
        """相邻小切片贪心合并为 ≤ max_lines 的组；超长单函数独立成片。"""
        rel = self._norm(rel_path)
        slices = self.slices_for_file(rel)
        if not slices:
            return []
        groups: list[list] = []  # [start, end, [FunctionSlice, ...]]
        for s in slices:
            if not groups:
                groups.append([s.line_start, s.line_end, [s]])
                continue
            cur = groups[-1]
            merged_end = max(cur[1], s.line_end)
            if merged_end - cur[0] + 1 <= max_lines:
                cur[1] = merged_end
                cur[2].append(s)
            else:
                groups.append([s.line_start, s.line_end, [s]])
        out: list[FunctionSlice] = []
        for start, end, members in groups:
            names = ",".join(m.symbol for m in members)
            out.append(
                FunctionSlice(
                    file=rel,
                    symbol=names,
                    line_start=start,
                    line_end=end,
                    code=self._read_code(rel, start, end),
                    signature=members[0].signature,
                    kind="group" if len(members) > 1 else members[0].kind,
                )
            )
        return out

    # ---------------------------------------------------------------- 统计

    def stats(self) -> dict:
        files = self._conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
        parsed_failed = self._conn.execute("SELECT COUNT(*) AS n FROM files WHERE parsed_ok = 0").fetchone()["n"]
        symbols = self._conn.execute("SELECT COUNT(*) AS n FROM symbols").fetchone()["n"]
        slices = self._conn.execute("SELECT COUNT(*) AS n FROM slices").fetchone()["n"]
        edges = self._conn.execute("SELECT COUNT(*) AS n FROM call_edges").fetchone()["n"]
        resolved = self._conn.execute("SELECT COUNT(*) AS n FROM call_edges WHERE resolved = 1").fetchone()["n"]
        languages = {
            r["language"]: r["n"]
            for r in self._conn.execute("SELECT language, COUNT(*) AS n FROM files GROUP BY language")
        }
        return {
            "files": files,
            "symbols": symbols,
            "slices": slices,
            "call_edges": edges,
            "resolved_edges": resolved,
            "resolved_ratio": round(resolved / edges, 4) if edges else 0.0,
            "parsed_failed": parsed_failed,
            "parsed_this_build": self._parses_this_build,
            "languages": languages,
        }

    # ---------------------------------------------------------------- 内部工具

    def _norm(self, rel_path: str | None) -> str:
        """路径归一为 posix 相对串（兼容调用方传入反斜杠）。"""
        if rel_path is None:
            return ""
        return PurePosixPath(rel_path.replace("\\", "/")).as_posix()

    def _norm_module_target(self, target: str) -> str | None:
        t = self._norm(target)
        row = self._conn.execute("SELECT 1 FROM files WHERE path = ?", (t,)).fetchone()
        if row:
            return t
        # 模块名 → 文件：a.b.c → a/b/c.py；以及去扩展名形式 a/b/c
        py = t.replace(".", "/") + ".py"
        if self._conn.execute("SELECT 1 FROM files WHERE path = ?", (py,)).fetchone():
            return py
        dotted = self._module_file(t)
        if dotted:
            return dotted
        noext = re.sub(r"\.(py|js|jsx|ts|tsx|mjs|cjs)$", "", t)
        if self._conn.execute("SELECT 1 FROM files WHERE path = ?", (noext,)).fetchone():
            return noext
        dotted_noext = self._module_file(noext)
        return dotted_noext

    def _symbols_in_file(self, file: str) -> list[str]:
        return sorted(self._symbols_by_file.get(file, set()))

    def _symbol_def_line(self, file: str, name: str) -> int:
        if not name:
            return 0
        row = self._conn.execute(
            "SELECT line_start FROM symbols WHERE file = ? AND name = ?", (file, name)
        ).fetchone()
        return row["line_start"] if row else 0

    def _row_symbol(self, r: sqlite3.Row) -> Symbol:
        return Symbol(
            id=r["id"],
            file=r["file"],
            kind=r["kind"],
            name=r["name"],
            line_start=r["line_start"],
            line_end=r["line_end"],
            signature=r["signature"],
        )

    def _line_snippet(self, rel: str, line: int, max_chars: int = 200) -> str:
        if line <= 0:
            return ""
        try:
            lines = self._ws.read_lines(rel, line, line)
        except (FileNotFoundError, OSError):
            return ""
        return lines[0].strip()[:max_chars] if lines else ""

    def _text_search(self, name: str, file_hint: str | None = None, max_hits: int = 200) -> list[Reference]:
        """简单文本搜索兜底：全词匹配符号名，返回引用行。"""
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        refs: list[Reference] = []
        for path in self._ws.source_files():
            rel = self._ws.rel(path)
            if file_hint is not None and rel != file_hint:
                continue
            try:
                text = self._ws.read_file_text(rel)
            except (FileNotFoundError, OSError):
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    refs.append(Reference(file=rel, line=i, snippet=line.strip()[:200]))
                    if len(refs) >= max_hits:
                        return refs
        return refs

    def _read_code(self, rel: str, start: int, end: int) -> str:
        try:
            return "\n".join(self._ws.read_lines(rel, start, end))
        except (FileNotFoundError, OSError):
            return ""
