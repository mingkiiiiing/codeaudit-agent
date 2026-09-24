"""C++ 索引单元测试（W28-C）：符号/导入/调用点提取、跨文件解析与 resolved_ratio。

语料：tests/corpus/cpp（config / dao / service / util 分层布局，符号按命名空间
限定名记录，调用按命名空间映射 + include 相对路径尽力解析），复制到临时工作区
后建索引。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from audit.indexer import SqliteIndexStore, create_index
from audit.workspace import WorkspaceContext

CORPUS_SRC = Path(__file__).resolve().parents[2] / "corpus" / "cpp"

MAIN_CPP = "src/main.cpp"
USER_DAO_HPP = "src/dao/user_dao.hpp"
USER_DAO_CPP = "src/dao/user_dao.cpp"
USER_SERVICE_CPP = "src/service/user_service.cpp"
APP_CONFIG_HPP = "src/config/app_config.hpp"
CONSTANTS_CPP = "src/config/constants.cpp"
SECRET_CONFIG_CPP = "src/config/secret_config.cpp"
STRING_UTIL_CPP = "src/util/string_util.cpp"


def _make_cpp_workspace(tmp_path: Path) -> WorkspaceContext:
    # copytree 自动创建目标目录（含父目录）；src_root 指向工程根，rel 带 src 前缀
    src = tmp_path / "proj" / "src"
    shutil.copytree(CORPUS_SRC, src)
    return WorkspaceContext(
        audit_id="cpp001",
        src_root=src.parent,
        work_root=tmp_path,
        db_path=tmp_path / "index.db",
    )


@pytest.fixture
def cpp_store(tmp_path: Path):
    ws = _make_cpp_workspace(tmp_path)
    s = create_index(ws)
    s.build()
    yield s
    s.close()


@pytest.fixture
def cpp_workspace(tmp_path: Path):
    return _make_cpp_workspace(tmp_path)


# ---------------------------------------------------------------- 语言识别


class TestGuessLanguageCpp:
    def test_cpp_extensions(self):
        from audit.utils import guess_language

        assert guess_language("a/b/main.cpp") == "cpp"
        assert guess_language("a/b/main.cc") == "cpp"
        assert guess_language("a/b/main.hpp") == "cpp"

    def test_existing_extensions_unchanged(self):
        from audit.utils import guess_language

        assert guess_language("a.py") == "python"
        assert guess_language("a.java") == "java"
        assert guess_language("a.go") == "go"

    def test_supported_languages_now_include_cpp(self):
        from audit.indexer.parsers import SUPPORTED_LANGUAGES

        assert SUPPORTED_LANGUAGES == ("python", "javascript", "typescript", "java", "go", "cpp")


# ---------------------------------------------------------------- 符号/导入/调用点提取


class TestCppExtraction:
    def test_build_parses_all_corpus_files(self, cpp_store: SqliteIndexStore):
        st = cpp_store.stats()
        assert st["files"] == 13
        assert st["languages"].get("cpp") == st["files"]
        assert st["parsed_failed"] == 0

    def test_namespace_symbols_qualified(self, cpp_store: SqliteIndexStore):
        # 嵌套 namespace app { namespace config { ... } } → 栈上全名 app::config
        kinds = {}
        for s in cpp_store.symbols_for_file(APP_CONFIG_HPP):
            kinds[s.name] = s.kind
        assert kinds["app"] == "namespace"
        assert kinds["app::config"] == "namespace"

    def test_class_symbol_in_header(self, cpp_store: SqliteIndexStore):
        sym = cpp_store.get_symbol("app::dao::UserDao")
        assert sym is not None
        assert sym.kind == "class"
        assert sym.file == USER_DAO_HPP
        assert sym.line_start == 10  # class 声明行（头文件保护宏/namespace 之后）
        assert sym.line_end >= sym.line_start

    def test_out_of_class_method_definitions(self, cpp_store: SqliteIndexStore):
        # 类外定义 Class::method 记 method（命名空间限定）；命名空间级函数记 function
        kinds = {}
        for s in cpp_store.symbols_for_file(USER_DAO_CPP):
            kinds[s.name] = s.kind
        assert kinds["app::dao::UserDao::find"] == "method"
        assert kinds["app::dao::UserDao::create"] == "method"
        assert kinds["app::dao::UserDao::table_name"] == "method"
        assert kinds["app::dao::count_users"] == "function"
        assert kinds["app::dao::find_user"] == "function"

    def test_long_method_symbol(self, cpp_store: SqliteIndexStore):
        sym = cpp_store.get_symbol("app::service::UserService::sync_all")
        assert sym is not None
        assert sym.kind == "method"
        assert sym.file == USER_SERVICE_CPP
        assert sym.line_start == 15
        assert sym.line_end - sym.line_start + 1 > 80  # 超长函数语料（>80 行）

    def test_const_symbols(self, cpp_store: SqliteIndexStore):
        # const 口径：const/constexpr 非函数体内声明记 constant
        kinds = {}
        for s in cpp_store.symbols_for_file(APP_CONFIG_HPP):
            kinds[s.name] = s.kind
        assert kinds["app::config::kTimeoutSeconds"] == "constant"  # constexpr
        assert kinds["app::config::kSuccessRatio"] == "constant"  # const

    def test_macro_constants(self, cpp_store: SqliteIndexStore):
        # 宏常量口径：#define 对象宏记 constant（含头文件保护宏）
        kinds = {}
        for s in cpp_store.symbols_for_file(APP_CONFIG_HPP):
            kinds[s.name] = s.kind
        assert kinds["APP_NAME"] == "constant"
        assert kinds["MAX_RETRIES"] == "constant"
        assert kinds["APP_CONFIG_HPP"] == "constant"

    def test_imports_extracted(self, cpp_store: SqliteIndexStore):
        # main.cpp：系统头 <string> 剥尖括号、引号头剥引号；alias/imported_name 恒空
        rows = list(
            cpp_store._conn.execute(
                "SELECT module, imported_name, alias FROM imports WHERE file = ?", (MAIN_CPP,)
            )
        )
        modules = {r["module"] for r in rows}
        assert {"string", "config/app_config.hpp", "dao/user_dao.hpp", "util/string_util.hpp"} <= modules
        assert all(r["imported_name"] == "" and r["alias"] == "" for r in rows)
        # user_dao.cpp：同目录引号头 + 相对路径头
        single = list(
            cpp_store._conn.execute("SELECT module FROM imports WHERE file = ?", (USER_DAO_CPP,))
        )
        assert {r["module"] for r in single} == {"user_dao.hpp", "../config/app_config.hpp"}

    def test_calls_extracted_with_caller(self, cpp_store: SqliteIndexStore):
        # 限定调用 ns::f 的 caller 为最内层 method 限定名
        rows = list(
            cpp_store._conn.execute(
                "SELECT caller_symbol, line FROM calls WHERE file = ? AND callee_name = 'app::util::to_upper'",
                (STRING_UTIL_CPP,),
            )
        )
        assert not rows  # to_upper 的调用点不在 util 自身
        rows = list(
            cpp_store._conn.execute(
                "SELECT caller_symbol, line FROM calls WHERE file = ? AND callee_name = 'app::config::retry_budget'",
                (USER_DAO_CPP,),
            )
        )
        assert rows and rows[0]["caller_symbol"] == "app::dao::find_user"
        assert rows[0]["line"] > 0

    def test_lambda_and_template_calls_not_collected(self, cpp_store: SqliteIndexStore):
        # 边界：lambda 体内调用（audit::write）与模板实例化调用（make_box<int>）不做；
        # lambda 变量本身的调用 task() 与成员调用 boxed.value() 照常收集
        lam = list(cpp_store._conn.execute("SELECT 1 FROM calls WHERE callee_name = 'audit::write'"))
        tpl = list(cpp_store._conn.execute("SELECT 1 FROM calls WHERE callee_name LIKE '%make_box%'"))
        assert not lam and not tpl
        kept = list(
            cpp_store._conn.execute(
                "SELECT callee_name FROM calls WHERE file = ? AND callee_name IN ('task', 'boxed.value')",
                (STRING_UTIL_CPP,),
            )
        )
        assert {r["callee_name"] for r in kept} == {"task", "boxed.value"}

    def test_slices_contain_cpp_functions(self, cpp_store: SqliteIndexStore):
        slices = cpp_store.slices_for_file(USER_DAO_CPP)
        names = {s.symbol for s in slices}
        assert {"app::dao::UserDao::find", "app::dao::UserDao::create", "app::dao::find_user"} <= names
        assert all(s.kind in ("function", "method") for s in slices)


# ---------------------------------------------------------------- 跨文件解析与统计


class TestCppResolution:
    def test_cross_file_edge_resolved(self, cpp_store: SqliteIndexStore):
        # main.cpp 经命名空间映射解析到 service 层
        edge = list(
            cpp_store._conn.execute(
                "SELECT callee_file, callee_symbol FROM call_edges"
                " WHERE caller_file = ? AND callee_name = 'app::service::bootstrap' AND resolved = 1",
                (MAIN_CPP,),
            )
        )
        assert edge and edge[0]["callee_file"] == USER_SERVICE_CPP
        assert edge[0]["callee_symbol"] == "bootstrap"

    def test_static_method_edge_resolved(self, cpp_store: SqliteIndexStore):
        # app::dao::UserDao::warm_cache()：最长 :: 前缀落到 app::dao 命名空间
        edge = list(
            cpp_store._conn.execute(
                "SELECT callee_file FROM call_edges"
                " WHERE caller_file = ? AND callee_name = 'app::dao::UserDao::warm_cache' AND resolved = 1",
                (MAIN_CPP,),
            )
        )
        assert edge and edge[0]["callee_file"] == USER_DAO_CPP

    def test_member_call_unresolved_kept(self, cpp_store: SqliteIndexStore):
        # 接收者变量调用不做类型推断（首轮边界）：未解析边保留
        edge = list(
            cpp_store._conn.execute(
                "SELECT 1 FROM call_edges WHERE caller_file = ? AND callee_name = 'service.refresh' AND resolved = 0",
                (MAIN_CPP,),
            )
        )
        assert edge

    def test_std_call_unresolved_kept(self, cpp_store: SqliteIndexStore):
        # 标准库调用（std:: 前缀，工作副本外）落未解析边
        edge = list(
            cpp_store._conn.execute(
                "SELECT 1 FROM call_edges WHERE caller_file = ? AND callee_name = 'std::to_string' AND resolved = 0",
                ("src/service/report_service.cpp",),
            )
        )
        assert edge

    def test_same_file_call_not_edged(self, cpp_store: SqliteIndexStore):
        # constants.cpp 内 config::retry_budget(...) 解析到本文件：不建边
        rows = list(
            cpp_store._conn.execute(
                "SELECT 1 FROM call_edges WHERE caller_file = ? AND callee_name LIKE '%retry_budget%'",
                (CONSTANTS_CPP,),
            )
        )
        assert not rows

    def test_dependencies_resolves_cpp_include(self, cpp_store: SqliteIndexStore):
        deps = cpp_store.dependencies(MAIN_CPP)
        # include 相对路径解析到工作副本内头文件；系统头 <string> 保留原始模块名
        assert "src/config/app_config.hpp" in deps
        assert "src/dao/user_dao.hpp" in deps
        assert "src/util/string_util.hpp" in deps
        assert "string" in deps

    def test_dependencies_imported_by(self, cpp_store: SqliteIndexStore):
        # 反查：include 了 app_config.hpp 的 cpp 文件
        imported_by = cpp_store.dependencies(APP_CONFIG_HPP, direction="imported_by")
        assert USER_DAO_CPP in imported_by
        assert MAIN_CPP in imported_by

    def test_resolved_ratio_at_least_0_4(self, cpp_store: SqliteIndexStore):
        st = cpp_store.stats()
        assert st["call_edges"] > 0
        assert st["resolved_ratio"] >= 0.4

    def test_incremental_build_reuses_unchanged(self, cpp_workspace: WorkspaceContext, tmp_path: Path):
        s = create_index(cpp_workspace)
        s.build()
        first = s.stats()
        s.build()
        assert s.stats()["parsed_this_build"] == 0
        assert s.stats()["files"] == first["files"]
        s.close()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
