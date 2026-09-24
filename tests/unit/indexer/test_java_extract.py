"""Java 索引单元测试（W24-A）：符号/导入/调用点提取、跨文件解析与 resolved_ratio。

语料：tests/corpus/java/src（maven 布局，com.corpus 包），复制到临时工作区后建索引。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from audit.indexer import SqliteIndexStore, create_index
from audit.workspace import WorkspaceContext

CORPUS_SRC = Path(__file__).resolve().parents[2] / "corpus" / "java" / "src"

USER_DAO = "src/main/java/com/corpus/dao/UserDao.java"
USER_SERVICE = "src/main/java/com/corpus/service/UserService.java"


def _make_java_workspace(tmp_path: Path) -> WorkspaceContext:
    # copytree 自动创建目标目录（含父目录）；src_root 指向工程根，rel 带 src/main/java 前缀
    src = tmp_path / "proj" / "src"
    shutil.copytree(CORPUS_SRC, src)
    return WorkspaceContext(
        audit_id="java001",
        src_root=src.parent,
        work_root=tmp_path,
        db_path=tmp_path / "index.db",
    )


@pytest.fixture
def java_store(tmp_path: Path):
    ws = _make_java_workspace(tmp_path)
    s = create_index(ws)
    s.build()
    yield s
    s.close()


@pytest.fixture
def java_workspace(tmp_path: Path):
    return _make_java_workspace(tmp_path)


# ---------------------------------------------------------------- 语言识别


class TestGuessLanguageJava:
    def test_java_extension(self):
        from audit.utils import guess_language

        assert guess_language("a/b/Foo.java") == "java"

    def test_existing_extensions_unchanged(self):
        from audit.utils import guess_language

        assert guess_language("a.py") == "python"
        assert guess_language("a.ts") == "typescript"

    def test_unknown_extension_content_probe(self, tmp_path: Path):
        from audit.utils import guess_language

        p = tmp_path / "Snippet.xyz"
        p.write_text("package com.demo;\n\npublic class Snippet {\n}\n", encoding="utf-8")
        assert guess_language(p) == "java"

    def test_unknown_extension_without_java_features(self, tmp_path: Path):
        from audit.utils import guess_language

        p = tmp_path / "notes.xyz"
        p.write_text("just some plain notes\n", encoding="utf-8")
        assert guess_language(p) is None

    def test_missing_file_returns_none(self, tmp_path: Path):
        from audit.utils import guess_language

        assert guess_language(tmp_path / "nope.xyz") is None

    def test_supported_languages_now_include_java(self):
        from audit.indexer.parsers import SUPPORTED_LANGUAGES

        # W26-C：Go 语言包并入后为五语言
        assert SUPPORTED_LANGUAGES == ("python", "javascript", "typescript", "java", "go", "cpp")


# ---------------------------------------------------------------- 符号/导入/调用点提取


class TestJavaExtraction:
    def test_build_parses_all_corpus_files(self, java_store: SqliteIndexStore):
        st = java_store.stats()
        assert st["files"] >= 10
        assert st["languages"].get("java") == st["files"]
        assert st["parsed_failed"] == 0

    def test_class_symbol(self, java_store: SqliteIndexStore):
        sym = java_store.get_symbol("UserDao")
        assert sym is not None
        assert sym.kind == "class"
        assert sym.file == USER_DAO
        assert sym.line_start == 9  # 语料中 class 声明行（javadoc 之后）
        assert sym.line_end >= sym.line_start

    def test_method_symbols_qualified(self, java_store: SqliteIndexStore):
        names = {s.name for s in java_store.symbols_for_file(USER_DAO)}
        assert {"UserDao", "UserDao.find", "UserDao.save", "UserDao.writeLog"} <= names
        find_sym = java_store.get_symbol("UserDao.find")
        assert find_sym is not None and find_sym.kind == "method"
        assert find_sym.signature.startswith("public Optional<String> find")

    def test_field_and_constant_symbols(self, java_store: SqliteIndexStore):
        kinds = {}
        for s in java_store.symbols_for_file("src/main/java/com/corpus/config/AppConfig.java"):
            kinds[s.name] = s.kind
        assert kinds["AppConfig.API_KEY"] == "constant"  # 全大写按常量口径
        assert kinds["AppConfig.dbPassword"] == "field"

    def test_constructor_symbol(self, java_store: SqliteIndexStore):
        sym = java_store.get_symbol("UserService.UserService")
        assert sym is not None and sym.kind == "method"

    def test_imports_extracted(self, java_store: SqliteIndexStore):
        modules = {
            r["module"]: r["imported_name"]
            for r in java_store._conn.execute("SELECT module, imported_name FROM imports WHERE file = ?", (USER_SERVICE,))
        }
        assert modules["com.corpus.dao.UserDao"] == ""
        assert modules["com.corpus.dao.AuditLogDao"] == ""

    def test_calls_extracted_with_caller(self, java_store: SqliteIndexStore):
        rows = list(
            java_store._conn.execute(
                "SELECT caller_symbol, callee_name, line FROM calls WHERE file = ? AND callee_name = 'userDao.find'",
                (USER_SERVICE,),
            )
        )
        assert rows and rows[0]["caller_symbol"] == "UserService.load"
        assert rows[0]["line"] > 0

    def test_slices_contain_java_methods(self, java_store: SqliteIndexStore):
        slices = java_store.slices_for_file(USER_DAO)
        names = {s.symbol for s in slices}
        assert {"UserDao.find", "UserDao.save"} <= names
        assert all(s.kind == "method" for s in slices)


# ---------------------------------------------------------------- 跨文件解析与统计


class TestJavaResolution:
    def test_cross_file_edge_resolved(self, java_store: SqliteIndexStore):
        edge = list(
            java_store._conn.execute(
                "SELECT callee_file, callee_symbol FROM call_edges"
                " WHERE caller_file = ? AND callee_name = 'userDao.find' AND resolved = 1",
                (USER_SERVICE,),
            )
        )
        assert edge and edge[0]["callee_file"] == USER_DAO
        assert edge[0]["callee_symbol"] == "find"

    def test_unresolved_edge_kept(self, java_store: SqliteIndexStore):
        edge = list(
            java_store._conn.execute(
                "SELECT 1 FROM call_edges WHERE caller_file = ? AND callee_name = 'System.out.println' AND resolved = 0",
                (USER_SERVICE,),
            )
        )
        assert edge  # 未解析边保留（尽力解析口径）

    def test_same_file_call_not_edged(self, java_store: SqliteIndexStore):
        rows = list(
            java_store._conn.execute(
                "SELECT 1 FROM call_edges WHERE caller_file = ? AND callee_name LIKE '%writeLog%'",
                (USER_DAO,),
            )
        )
        assert not rows  # 同文件调用：源码直接可见，不建边

    def test_dependencies_resolves_java_import(self, java_store: SqliteIndexStore):
        deps = java_store.dependencies(USER_SERVICE)
        assert USER_DAO.replace("UserDao.java", "AuditLogDao.java") in deps

    def test_resolved_ratio_at_least_0_4(self, java_store: SqliteIndexStore):
        st = java_store.stats()
        assert st["call_edges"] > 0
        assert st["resolved_ratio"] >= 0.4

    def test_incremental_build_reuses_unchanged(self, java_workspace: WorkspaceContext, tmp_path: Path):
        s = create_index(java_workspace)
        s.build()
        first = s.stats()
        s.build()
        assert s.stats()["parsed_this_build"] == 0
        assert s.stats()["files"] == first["files"]
        s.close()
