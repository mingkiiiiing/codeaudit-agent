"""Stage2 索引单元测试：符号表 / 调用图 / 切片 / 增量 / 统计（基于 demo_proj 工作区）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from audit.indexer import SqliteIndexStore, create_index
from audit.indexer.base import IndexStore
from audit.workspace import WorkspaceContext

ORDERS = "app/services/orders.py"
USERS = "app/services/users.py"


@pytest.fixture
def store(sample_workspace: WorkspaceContext):
    """建好索引的 store（函数内手动 build 便于增量断言的场景请自建）。"""
    s = create_index(sample_workspace)
    s.build()
    yield s
    s.close()


# ---------------------------------------------------------------- 工厂与契约


def test_create_index_returns_indexstore(sample_workspace: WorkspaceContext):
    s = create_index(sample_workspace)
    assert isinstance(s, IndexStore)
    assert isinstance(s, SqliteIndexStore)
    s.build()
    s.close()


def test_create_index_custom_db_path(sample_workspace: WorkspaceContext, tmp_path: Path):
    db = tmp_path / "custom" / "my.db"
    s = create_index(sample_workspace, db_path=db)
    s.build()
    assert db.is_file()
    s.close()


# ---------------------------------------------------------------- 符号表


def test_get_symbol_hits_users_service(store: SqliteIndexStore):
    sym = store.get_symbol("get_user")
    assert sym is not None
    assert sym.file == USERS
    assert sym.kind == "function"
    assert sym.line_start > 0 and sym.line_end >= sym.line_start
    assert sym.signature.startswith("def get_user")


def test_get_symbol_with_file_hint(store: SqliteIndexStore):
    sym = store.get_symbol("get_user", file_hint=USERS)
    assert sym is not None and sym.file == USERS
    assert store.get_symbol("no_such_symbol_anywhere") is None


def test_symbols_for_file(store: SqliteIndexStore):
    names = {s.name for s in store.symbols_for_file(ORDERS)}
    assert {"get_order_summary", "load_totals", "safe_delete", "create_order"} <= names
    const_names = {s.name for s in store.symbols_for_file("app/config.py")}
    assert "API_KEY" in const_names
    kinds = {s.kind for s in store.symbols_for_file("app/config.py")}
    assert "constant" in kinds


def test_all_symbols_nonempty(store: SqliteIndexStore):
    syms = store.all_symbols()
    assert len(syms) >= 10
    assert all(s.file and s.kind and s.name for s in syms)


def test_signature_exact(store: SqliteIndexStore):
    sym = store.get_symbol("get_order_summary")
    assert sym is not None
    assert sym.signature == "def get_order_summary(order_id, conn):"


# ---------------------------------------------------------------- 函数切片


def test_slices_for_file_contains_sql(store: SqliteIndexStore):
    slices = store.slices_for_file(ORDERS)
    names = {s.symbol for s in slices}
    assert {"get_order_summary", "load_totals", "safe_delete", "create_order"} <= names
    target = next(s for s in slices if s.symbol == "get_order_summary")
    assert "SELECT * FROM orders" in target.code
    assert target.code.startswith("def get_order_summary")
    assert target.kind == "function"
    assert target.line_end >= target.line_start


def test_grouped_slices_within_limit_and_full_coverage(store: SqliteIndexStore):
    slices = store.slices_for_file(ORDERS)
    all_symbols = {s.symbol for s in slices}
    groups = store.grouped_slices(ORDERS, max_lines=400)
    assert groups
    covered: set[str] = set()
    for g in groups:
        span = g.line_end - g.line_start + 1
        assert span <= 400
        covered |= set(g.symbol.split(","))
    assert covered == all_symbols  # 覆盖全文件函数


def test_grouped_slices_small_limit_splits(store: SqliteIndexStore):
    groups = store.grouped_slices(ORDERS, max_lines=10)
    assert len(groups) >= 4  # 每个函数独立成组
    covered: set[str] = set()
    for g in groups:
        assert g.line_end - g.line_start + 1 <= 10
        covered |= set(g.symbol.split(","))
    assert {"get_order_summary", "load_totals", "safe_delete", "create_order"} <= covered


def test_long_function_independent_slice(tmp_path: Path):
    """单函数超过 max_lines 时独立成片，code 为函数原文。"""
    body = "\n".join(f"    step_{i} = {i}" for i in range(50))
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "long_fn.py").write_text(f"def very_long():\n{body}\n    return None\n\ndef tiny():\n    return 1\n", encoding="utf-8")
    ws = WorkspaceContext(audit_id="t", src_root=src, work_root=tmp_path, db_path=tmp_path / "i.db")
    s = create_index(ws)
    s.build()
    groups = s.grouped_slices("long_fn.py", max_lines=10)
    long_groups = [g for g in groups if "very_long" in g.symbol]
    assert len(long_groups) == 1
    assert long_groups[0].line_end - long_groups[0].line_start + 1 > 10  # 超长单函数独立成片
    assert "return None" in long_groups[0].code
    s.close()


# ---------------------------------------------------------------- 调用图


def test_cross_file_call_edge_resolved(store: SqliteIndexStore):
    """orders.py 对 get_user 的调用边：跨文件、resolved=True。"""
    chain = store.call_chain("get_order_summary", "callees")
    assert any("get_user" in c and USERS in c for c in chain)
    # 白盒校验 resolved 标记
    row = store._conn.execute(
        "SELECT resolved, callee_file, callee_symbol FROM call_edges"
        " WHERE caller_file = ? AND callee_name = 'get_user'",
        (ORDERS,),
    ).fetchone()
    assert row is not None
    assert row["resolved"] == 1
    assert row["callee_file"] == USERS
    assert row["callee_symbol"] == "get_user"


def test_same_file_call_no_edge(store: SqliteIndexStore):
    """同文件调用不建 resolved 边（源码直接可见）。"""
    rows = store._conn.execute(
        "SELECT COUNT(*) AS n FROM call_edges WHERE caller_file = ? AND callee_name = 'open'",
        (ORDERS,),
    ).fetchone()
    # open 是内建，解析失败保留 resolved=0；本文件函数互调则完全不建边
    local_rows = store._conn.execute(
        "SELECT COUNT(*) AS n FROM call_edges WHERE caller_file = ? AND callee_name = 'get_user'",
        (USERS,),
    ).fetchone()
    assert local_rows["n"] == 0


def test_call_chain_callers_direction(store: SqliteIndexStore):
    chain = store.call_chain("get_user", direction="callers")
    assert any(ORDERS in c and "get_order_summary" in c for c in chain)
    # 元素格式：file:line sym -> file:line sym
    assert all(" -> " in c for c in chain)


def test_call_chain_depth(store: SqliteIndexStore):
    d1 = store.call_chain("main", "callees", depth=1)
    d2 = store.call_chain("main", "callees", depth=2)
    assert any("load_totals" in c for c in d1)
    assert len(d2) >= len(d1)


# ---------------------------------------------------------------- 引用


def test_references_nonempty(store: SqliteIndexStore):
    refs = store.references("get_user")
    assert refs
    assert any(r.file == ORDERS and r.line > 0 for r in refs)
    assert all(r.snippet for r in refs)


def test_references_text_search_fallback(store: SqliteIndexStore):
    """无调用边的符号：文本搜索兜底（命中定义行）。"""
    refs = store.references("find_by_email")
    assert any(r.file == USERS for r in refs)


# ---------------------------------------------------------------- 依赖


def test_dependencies_imports(store: SqliteIndexStore):
    deps = store.dependencies(ORDERS)
    assert USERS in deps


def test_dependencies_imported_by(store: SqliteIndexStore):
    imported_by = store.dependencies(USERS, direction="imported_by")
    assert ORDERS in imported_by


# ---------------------------------------------------------------- 增量与统计


def test_incremental_build_skips_unchanged(sample_workspace: WorkspaceContext):
    s = create_index(sample_workspace)
    s.build()
    first = s.stats()
    assert first["parsed_this_build"] > 0
    s.build()
    second = s.stats()
    assert second["parsed_this_build"] == 0  # 零重解析
    strip = lambda d: {k: v for k, v in d.items() if k != "parsed_this_build"}
    assert strip(first) == strip(second)  # 其余统计一致
    s.close()


def test_incremental_reparse_on_change(sample_workspace: WorkspaceContext):
    s = create_index(sample_workspace)
    s.build()
    before = s.stats()
    target = sample_workspace.abs_path(USERS)
    target.write_text(
        target.read_text(encoding="utf-8") + "\n\ndef brand_new_func(x):\n    return x\n",
        encoding="utf-8",
    )
    s.build()
    after = s.stats()
    assert after["parsed_this_build"] == 1  # 只重解析变更文件
    assert after["symbols"] == before["symbols"] + 1
    sym = s.get_symbol("brand_new_func")
    assert sym is not None and sym.file == USERS
    s.close()


def test_stale_file_removed_from_index(sample_workspace: WorkspaceContext):
    s = create_index(sample_workspace)
    s.build()
    files_before = s.stats()["files"]
    sample_workspace.abs_path("app/utils/net.py").unlink()
    s.build()
    assert s.stats()["files"] == files_before - 1
    assert s.get_symbol("fetch_json") is None
    s.close()


def test_stats_shape(store: SqliteIndexStore):
    st = store.stats()
    for key in ("files", "symbols", "call_edges", "resolved_ratio", "parsed_failed"):
        assert key in st
    assert st["files"] >= 9
    assert st["symbols"] > 0
    assert st["call_edges"] > 0
    assert 0.0 < st["resolved_ratio"] < 1.0
    assert st["parsed_failed"] == 0
