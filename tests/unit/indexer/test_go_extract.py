"""Go 索引单元测试（W26-C）：符号/导入/调用点提取、跨文件解析与 resolved_ratio。

语料：tests/corpus/go（cmd + internal 分层布局，导入路径 internal/xxx 与
包目录按路径段后缀匹配），复制到临时工作区后建索引。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from audit.indexer import SqliteIndexStore, create_index
from audit.workspace import WorkspaceContext

CORPUS_SRC = Path(__file__).resolve().parents[2] / "corpus" / "go"

MAIN_GO = "src/cmd/main.go"
USER_DAO_GO = "src/internal/dao/user_dao.go"
USER_SERVICE_GO = "src/internal/service/user_service.go"
CONFIG_GO = "src/internal/config/config.go"
MATH_HELPER_GO = "src/internal/util/math_helper.go"


def _make_go_workspace(tmp_path: Path) -> WorkspaceContext:
    # copytree 自动创建目标目录（含父目录）；src_root 指向工程根，rel 带 src 前缀
    src = tmp_path / "proj" / "src"
    shutil.copytree(CORPUS_SRC, src)
    return WorkspaceContext(
        audit_id="go001",
        src_root=src.parent,
        work_root=tmp_path,
        db_path=tmp_path / "index.db",
    )


@pytest.fixture
def go_store(tmp_path: Path):
    ws = _make_go_workspace(tmp_path)
    s = create_index(ws)
    s.build()
    yield s
    s.close()


@pytest.fixture
def go_workspace(tmp_path: Path):
    return _make_go_workspace(tmp_path)


# ---------------------------------------------------------------- 语言识别


class TestGuessLanguageGo:
    def test_go_extension(self):
        from audit.utils import guess_language

        assert guess_language("a/b/main.go") == "go"

    def test_existing_extensions_unchanged(self):
        from audit.utils import guess_language

        assert guess_language("a.py") == "python"
        assert guess_language("a.java") == "java"
        assert guess_language("a.ts") == "typescript"

    def test_supported_languages_now_include_go(self):
        from audit.indexer.parsers import SUPPORTED_LANGUAGES

        assert SUPPORTED_LANGUAGES == ("python", "javascript", "typescript", "java", "go", "cpp")


# ---------------------------------------------------------------- 符号/导入/调用点提取


class TestGoExtraction:
    def test_build_parses_all_corpus_files(self, go_store: SqliteIndexStore):
        st = go_store.stats()
        assert st["files"] == 13
        assert st["languages"].get("go") == st["files"]
        assert st["parsed_failed"] == 0

    def test_package_symbol(self, go_store: SqliteIndexStore):
        sym = go_store.get_symbol("main")
        assert sym is not None
        assert sym.kind == "package"
        assert sym.file == MAIN_GO

    def test_type_symbol(self, go_store: SqliteIndexStore):
        sym = go_store.get_symbol("UserDao")
        assert sym is not None
        assert sym.kind == "class"
        assert sym.file == USER_DAO_GO
        assert sym.line_start == 8  # type 声明行（包注释/子注释之后）
        assert sym.line_end >= sym.line_start

    def test_function_symbols(self, go_store: SqliteIndexStore):
        names = {s.name for s in go_store.symbols_for_file(MATH_HELPER_GO)}
        assert {"Greet", "Mask", "Round", "Summarize"} <= names
        greet = go_store.get_symbol("Greet")
        assert greet is not None and greet.kind == "function"
        assert greet.signature.startswith("func Greet")

    def test_method_symbols_qualified(self, go_store: SqliteIndexStore):
        # 指针接收者 (*UserDao).Find；值接收者 UserDao.Label（docstring 口径）
        names = {s.name for s in go_store.symbols_for_file(USER_DAO_GO)}
        assert {"(*UserDao).Find", "UserDao.Label", "NewUserDao"} <= names
        find_sym = go_store.get_symbol("(*UserDao).Find")
        assert find_sym is not None and find_sym.kind == "method"
        assert find_sym.signature.startswith("func (u *UserDao) Find")

    def test_const_symbols(self, go_store: SqliteIndexStore):
        # Go 无全大写惯例：const_spec 一律记 constant，导出与否按首字母大小写
        kinds = {}
        for s in go_store.symbols_for_file(CONFIG_GO):
            kinds[s.name] = s.kind
        assert kinds["PortHTTP"] == "constant"  # 导出（首字母大写）
        assert kinds["portDebug"] == "constant"  # 未导出（首字母小写）——同为 constant
        assert kinds["MaxRetries"] == "constant"  # 单行 const 形态

    def test_imports_extracted_both_forms(self, go_store: SqliteIndexStore):
        # user_service.go：圆括号块 import（含 "fmt" 与 internal/xxx 两形态）
        rows = list(
            go_store._conn.execute(
                "SELECT module, imported_name, alias FROM imports WHERE file = ?", (USER_SERVICE_GO,)
            )
        )
        modules = {r["module"] for r in rows}
        assert {"fmt", "internal/dao", "internal/util"} <= modules
        assert all(r["imported_name"] == "" for r in rows)
        # main.go：同样为块 import；user_dao.go：单行 import "internal/model"
        single = list(
            go_store._conn.execute("SELECT module FROM imports WHERE file = ?", (USER_DAO_GO,))
        )
        assert [r["module"] for r in single] == ["internal/model"]

    def test_calls_extracted_with_caller(self, go_store: SqliteIndexStore):
        rows = list(
            go_store._conn.execute(
                "SELECT caller_symbol, callee_name, line FROM calls WHERE file = ? AND callee_name = 'util.Mask'",
                (USER_SERVICE_GO,),
            )
        )
        assert rows and rows[0]["caller_symbol"] == "(*UserService).Load"
        assert rows[0]["line"] > 0
        # 内建函数（len/int/string 等）不记调用点（W26-C 口径）
        builtins = list(
            go_store._conn.execute(
                "SELECT 1 FROM calls WHERE file = ? AND callee_name IN ('len','int','string','append')",
                (MATH_HELPER_GO,),
            )
        )
        assert not builtins

    def test_func_literal_calls_not_collected(self, go_store: SqliteIndexStore):
        # 边界：go func 字面量体内调用不做；字面量调用本身无 callee 名不记
        rows = list(
            go_store._conn.execute("SELECT callee_name FROM calls WHERE file = 'src/internal/web/user_controller.go'")
        )
        assert all("func" not in c["callee_name"] for c in rows)

    def test_slices_contain_go_functions(self, go_store: SqliteIndexStore):
        slices = go_store.slices_for_file(USER_DAO_GO)
        names = {s.symbol for s in slices}
        assert {"(*UserDao).Find", "UserDao.Label", "NewUserDao"} <= names
        assert all(s.kind in ("function", "method") for s in slices)


# ---------------------------------------------------------------- 跨文件解析与统计


class TestGoResolution:
    def test_cross_file_edge_resolved(self, go_store: SqliteIndexStore):
        # main.go 经 import 包名绑定解析到 service 包
        edge = list(
            go_store._conn.execute(
                "SELECT callee_file, callee_symbol FROM call_edges"
                " WHERE caller_file = ? AND callee_name = 'service.NewUserService' AND resolved = 1",
                (MAIN_GO,),
            )
        )
        assert edge and edge[0]["callee_file"] == USER_SERVICE_GO
        assert edge[0]["callee_symbol"] == "NewUserService"

    def test_method_value_edge_unresolved_kept(self, go_store: SqliteIndexStore):
        # 接收者变量调用不做类型推断（首轮边界）：未解析边保留
        edge = list(
            go_store._conn.execute(
                "SELECT 1 FROM call_edges WHERE caller_file = ? AND callee_name = 'c.users.Search' AND resolved = 0",
                ("src/internal/web/user_controller.go",),
            )
        )
        assert edge

    def test_same_file_call_not_edged(self, go_store: SqliteIndexStore):
        rows = list(
            go_store._conn.execute(
                "SELECT 1 FROM call_edges WHERE caller_file = ? AND callee_name LIKE '%itoa%'",
                (CONFIG_GO,),
            )
        )
        assert not rows  # 同文件调用：源码直接可见，不建边

    def test_dependencies_resolves_go_import(self, go_store: SqliteIndexStore):
        deps = go_store.dependencies(MAIN_GO)
        # Go 一目录一包且可多文件：dependencies 指向包内排序首位的代表文件
        assert "src/internal/config/config.go" in deps
        assert any(d.startswith("src/internal/service/") for d in deps)
        assert any(d.startswith("src/internal/dao/") for d in deps)
        assert "fmt" in deps  # 工作副本外的标准库保留原始导入路径

    def test_resolved_ratio_at_least_0_4(self, go_store: SqliteIndexStore):
        st = go_store.stats()
        assert st["call_edges"] > 0
        assert st["resolved_ratio"] >= 0.4

    def test_incremental_build_reuses_unchanged(self, go_workspace: WorkspaceContext, tmp_path: Path):
        s = create_index(go_workspace)
        s.build()
        first = s.stats()
        s.build()
        assert s.stats()["parsed_this_build"] == 0
        assert s.stats()["files"] == first["files"]
        s.close()
