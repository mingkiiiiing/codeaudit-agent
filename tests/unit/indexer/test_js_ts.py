"""JS/TS 索引单元测试：解析不崩、能提取 function 符号、跨文件调用尽力解析。"""

from __future__ import annotations

from pathlib import Path

import pytest

from audit.ingest import ingest
from audit.indexer import SqliteIndexStore, create_index
from audit.workspace import WorkspaceContext

HELPERS_TS = """export function helper(a: number): number {
  return a + 1;
}

export interface Opts {
  name: string;
}
"""

INDEX_TS = """import { helper } from './helpers';
import * as ns from './helpers';

export function compute(a: number, b: string): string {
  return String(helper(a)) + b;
}

export class Calc {
  add(x: number): number {
    return helper(x);
  }
}

const lambdaFn = (v: number) => v * 2;

export function runAll(): void {
  compute(1, "s");
  ns.helper(2);
  mysteryCall(9);
}
"""


def _make_ts_project(tmp_path: Path) -> WorkspaceContext:
    src = tmp_path / "proj"
    (src / "src").mkdir(parents=True)
    (src / "src" / "helpers.ts").write_text(HELPERS_TS, encoding="utf-8")
    (src / "src" / "index.ts").write_text(INDEX_TS, encoding="utf-8")
    return ingest(src, tmp_path, "ts1")


@pytest.fixture
def ts_store(tmp_path: Path):
    ws = _make_ts_project(tmp_path)
    s = create_index(ws)
    s.build()
    yield s
    s.close()


def test_ts_parse_no_crash_and_language_detected(ts_store: SqliteIndexStore):
    st = ts_store.stats()
    assert st["files"] == 2
    assert st["parsed_failed"] == 0
    assert set(st["languages"]) == {"typescript"}


def test_ts_extract_function_symbols(ts_store: SqliteIndexStore):
    sym = ts_store.get_symbol("compute")
    assert sym is not None
    assert sym.kind == "function"
    assert sym.file == "src/index.ts"
    assert sym.signature.startswith("function compute")

    method = ts_store.get_symbol("Calc.add")
    assert method is not None
    assert method.kind == "method"
    assert method.name == "Calc.add"


def test_ts_lambda_and_class_symbols(ts_store: SqliteIndexStore):
    kinds = {(s.name, s.kind) for s in ts_store.all_symbols()}
    assert ("lambdaFn", "function") in kinds
    assert ("Calc", "class") in kinds
    assert any(n == "helper" and k == "function" for n, k in kinds)


def test_ts_cross_file_call_resolved(ts_store: SqliteIndexStore):
    chain = ts_store.call_chain("compute", "callees")
    assert any("src/helpers.ts" in c and "helper" in c for c in chain)
    row = ts_store._conn.execute(
        "SELECT resolved FROM call_edges WHERE caller_symbol = 'compute' AND callee_name = 'helper'"
    ).fetchone()
    assert row is not None and row["resolved"] == 1


def test_ts_unresolved_call_kept(ts_store: SqliteIndexStore):
    """mysteryCall 未导入未定义 → 保留 resolved=0 的调用边。"""
    row = ts_store._conn.execute(
        "SELECT resolved, callee_file FROM call_edges WHERE callee_name = 'mysteryCall'"
    ).fetchone()
    assert row is not None
    assert row["resolved"] == 0
    assert row["callee_file"] == ""


def test_ts_references(ts_store: SqliteIndexStore):
    refs = ts_store.references("helper")
    assert any(r.file == "src/index.ts" for r in refs)


def test_ts_dependencies(ts_store: SqliteIndexStore):
    assert ts_store.dependencies("src/index.ts") == ["src/helpers.ts"]
    assert ts_store.dependencies("src/helpers.ts", direction="imported_by") == ["src/index.ts"]


def test_ts_incremental(tmp_path: Path):
    ws = _make_ts_project(tmp_path)
    s = create_index(ws)
    s.build()
    first = s.stats()
    assert first["parsed_this_build"] == 2
    s.build()
    assert s.stats()["parsed_this_build"] == 0
    s.close()


def test_js_parse_minimal(tmp_path: Path):
    """纯 JS 文件：function 声明 + 箭头函数赋值，解析与符号提取不崩。"""
    src = tmp_path / "jsproj"
    src.mkdir()
    (src / "util.js").write_text(
        "function addOne(x) { return x + 1; }\n"
        "const mul = (a, b) => a * b;\n"
        "class Box {\n  open() { return addOne(1); }\n}\n"
        "module.exports = { addOne, mul };\n",
        encoding="utf-8",
    )
    ws = ingest(src, tmp_path, "js1")
    assert ws.language == "javascript"
    s = create_index(ws)
    s.build()
    st = s.stats()
    assert st["parsed_failed"] == 0
    names = {(sym.name, sym.kind) for sym in s.all_symbols()}
    assert ("addOne", "function") in names
    assert ("mul", "function") in names
    assert ("Box", "class") in names
    assert ("Box.open", "method") in names
    s.close()
