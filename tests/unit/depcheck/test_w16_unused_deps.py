"""W16 遗留清偿：冗余依赖检出（DEP-UNUSED）单测——tmp 项目正反例。

覆盖：import 名映射表命中、零引用命中、dash/underscore 互换、子路径
require、@types 豁免、node 内置豁免、devDependencies 豁免、无源码防误报
红线、Issue 契约与 DEPCHECK_OFF 开关。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from audit.depcheck import scanner
from audit.models import Category, Severity


def make_ctx(src_root) -> SimpleNamespace:
    return SimpleNamespace(workspace=SimpleNamespace(src_root=src_root), extra={})


def unused_rules(issues):
    """从 Issue 列表抽取 (rule, package) 对，便于断言。"""
    out = set()
    for iss in issues:
        rule = next((e[5:] for e in iss.evidence if e.startswith("rule:")), "")
        pkg = next((e[8:] for e in iss.evidence if e.startswith("package:")), "")
        if rule == "DEP-UNUSED":
            out.add((rule, pkg))
    return out


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.delenv("CODEAUDIT_OSV_ONLINE", raising=False)
    monkeypatch.delenv("CODEAUDIT_DEPCHECK_OFF", raising=False)
    monkeypatch.delenv("CODEAUDIT_CFGSECRET_OFF", raising=False)


# ---------------------------------------------------------------- python 侧


class TestPypiUnused:
    def test_alias_mapping_hit_not_reported(self, tmp_path):
        # 映射表命中：声明 beautifulsoup4，源码 import bs4 → 不报
        (tmp_path / "requirements.txt").write_text("beautifulsoup4==4.12.2\n", encoding="utf-8")
        (tmp_path / "app.py").write_text("import bs4\n", encoding="utf-8")
        assert unused_rules(scanner.run(make_ctx(tmp_path))) == set()

    def test_alias_table_contains_required_entries(self):
        # 内置映射常量覆盖任务点名条目（10+ 条）
        table = scanner.PYPI_IMPORT_ALIASES
        assert table["beautifulsoup4"] == ("bs4",)
        assert table["pyyaml"] == ("yaml",)
        assert table["python-dateutil"] == ("dateutil",)
        assert table["pillow"] == ("PIL",)
        assert len(table) >= 10

    def test_zero_reference_reported(self, tmp_path):
        (tmp_path / "requirements.txt").write_text(
            "jinja2==3.1.2\nbeautifulsoup4==4.12.2\n", encoding="utf-8"
        )
        (tmp_path / "app.py").write_text("import bs4\n", encoding="utf-8")
        found = unused_rules(scanner.run(make_ctx(tmp_path)))
        assert ("DEP-UNUSED", "jinja2") in found
        assert ("DEP-UNUSED", "beautifulsoup4") not in found

    def test_dash_underscore_swap_not_reported(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("my-lib-tool>=1.0\n", encoding="utf-8")
        (tmp_path / "app.py").write_text("import my_lib_tool\n", encoding="utf-8")
        assert unused_rules(scanner.run(make_ctx(tmp_path))) == set()

    def test_submodule_prefix_not_reported(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("jinja2==3.1.2\n", encoding="utf-8")
        (tmp_path / "app.py").write_text("from jinja2.sandbox import SandboxedEnv\n", encoding="utf-8")
        assert unused_rules(scanner.run(make_ctx(tmp_path))) == set()

    def test_import_list_and_alias_not_reported(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("jinja2==3.1.2\nmarkupsafe==2.1.0\n", encoding="utf-8")
        (tmp_path / "app.py").write_text(
            "import os, jinja2 as jj\nfrom markupsafe import escape\n", encoding="utf-8"
        )
        assert unused_rules(scanner.run(make_ctx(tmp_path))) == set()

    def test_extras_stripped_name_resolved(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("requests[socks]==2.31.0\n", encoding="utf-8")
        (tmp_path / "app.py").write_text("import requests\n", encoding="utf-8")
        assert unused_rules(scanner.run(make_ctx(tmp_path))) == set()

    def test_pyproject_deps_checked(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["flask>=3.0", "orjson==3.9.0"]\n',
            encoding="utf-8",
        )
        (tmp_path / "main.py").write_text("import flask\n", encoding="utf-8")
        found = unused_rules(scanner.run(make_ctx(tmp_path)))
        assert ("DEP-UNUSED", "orjson") in found
        assert ("DEP-UNUSED", "flask") not in found


# ---------------------------------------------------------------- npm 侧


class TestNpmUnused:
    def _project(self, tmp_path, deps: str, dev_deps: str = "", source: str = ""):
        pkg = {"dependencies": deps, "devDependencies": dev_deps}
        import json

        (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
        (tmp_path / "index.js").write_text(source or "// placeholder\n", encoding="utf-8")

    def test_zero_reference_reported(self, tmp_path):
        self._project(tmp_path, {"left-pad": "^1.3.0"})
        found = unused_rules(scanner.run(make_ctx(tmp_path)))
        assert ("DEP-UNUSED", "left-pad") in found

    def test_types_scope_exempt(self, tmp_path):
        # @types/* 包豁免：不报
        self._project(tmp_path, {"@types/node": "^20.0.0"})
        assert unused_rules(scanner.run(make_ctx(tmp_path))) == set()

    def test_node_builtin_exempt(self, tmp_path):
        # node 内置模块同名依赖豁免：不报
        self._project(tmp_path, {"fs": "^1.0.0", "left-pad": "^1.3.0"})
        found = unused_rules(scanner.run(make_ctx(tmp_path)))
        assert ("DEP-UNUSED", "fs") not in found
        assert ("DEP-UNUSED", "left-pad") in found

    def test_dev_dependencies_exempt(self, tmp_path):
        # devDependencies 不报（构建工具链）
        self._project(tmp_path, {"left-pad": "^1.3.0"}, dev_deps={"eslint": "^8.0.0"})
        found = unused_rules(scanner.run(make_ctx(tmp_path)))
        assert ("DEP-UNUSED", "eslint") not in found
        assert ("DEP-UNUSED", "left-pad") in found

    def test_subpath_require_counts_as_usage(self, tmp_path):
        self._project(tmp_path, {"lodash": "^4.17.0"}, source='const fp = require("lodash/fp");\n')
        assert unused_rules(scanner.run(make_ctx(tmp_path))) == set()

    def test_scoped_pkg_dynamic_import_counts(self, tmp_path):
        self._project(
            tmp_path,
            {"@scope/pkg": "^1.0.0"},
            source='async function f() { return import("@scope/pkg/client"); }\n',
        )
        assert unused_rules(scanner.run(make_ctx(tmp_path))) == set()

    def test_esm_from_import_counts(self, tmp_path):
        self._project(
            tmp_path,
            {"express": "^4.18.0", "lodash": "^4.17.0"},
            source="import express from 'express';\nimport { map } from \"lodash\";\n",
        )
        assert unused_rules(scanner.run(make_ctx(tmp_path))) == set()


# ---------------------------------------------------------------- 红线与契约


class TestRedlinesAndContract:
    def test_clean_python_project_zero_hits(self, tmp_path):
        # clean 红线：声明且使用 → 0 命中
        (tmp_path / "requirements.txt").write_text("jinja2==3.1.2\nflask==3.0.0\n", encoding="utf-8")
        (tmp_path / "app.py").write_text(
            "import jinja2\nfrom flask import Flask\napp = Flask(__name__)\n", encoding="utf-8"
        )
        assert unused_rules(scanner.run(make_ctx(tmp_path))) == set()

    def test_clean_npm_project_zero_hits(self, tmp_path):
        import json

        (tmp_path / "package.json").write_text(
            json.dumps({"dependencies": {"express": "^4.18.0", "lodash": "^4.17.0"}}),
            encoding="utf-8",
        )
        (tmp_path / "index.js").write_text(
            "const express = require('express');\nimport { map } from 'lodash';\n",
            encoding="utf-8",
        )
        assert unused_rules(scanner.run(make_ctx(tmp_path))) == set()

    def test_no_source_files_guard(self, tmp_path):
        # 防误报红线：项目内无任何 .py 源码 → pypi 清单不判冗余（无源码可证）
        (tmp_path / "requirements.txt").write_text("jinja2==3.1.2\n", encoding="utf-8")
        assert unused_rules(scanner.run(make_ctx(tmp_path))) == set()

    def test_no_js_source_guard(self, tmp_path):
        import json

        (tmp_path / "package.json").write_text(
            json.dumps({"dependencies": {"left-pad": "^1.3.0"}}), encoding="utf-8"
        )
        (tmp_path / "app.py").write_text("import os\n", encoding="utf-8")  # 只有 py 源码
        assert unused_rules(scanner.run(make_ctx(tmp_path))) == set()

    def test_issue_contract(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("jinja2==3.1.2\n", encoding="utf-8")
        (tmp_path / "app.py").write_text("import os\n", encoding="utf-8")
        issues = [i for i in scanner.run(make_ctx(tmp_path)) if "rule:DEP-UNUSED" in i.evidence]
        assert len(issues) == 1
        iss = issues[0]
        assert iss.category == Category.STYLE
        assert iss.severity == Severity.LOW
        assert iss.confidence == 0.7
        assert iss.file == "requirements.txt"
        assert iss.line_start == 1 and iss.line_end == 1
        assert "疑似冗余" in iss.description
        assert "请人工确认" in iss.description
        assert iss.evidence[0] == "rule:DEP-UNUSED"
        assert any(e.startswith("loc:requirements.txt:") for e in iss.evidence)

    def test_depcheck_off_disables_unused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEAUDIT_DEPCHECK_OFF", "1")
        (tmp_path / "requirements.txt").write_text("jinja2==3.1.2\n", encoding="utf-8")
        (tmp_path / "app.py").write_text("import os\n", encoding="utf-8")
        assert unused_rules(scanner.run(make_ctx(tmp_path))) == set()

    def test_ignored_dirs_not_scanned_for_references(self, tmp_path):
        # venv 内的引用不计数（目录剪枝）：声明包只在 venv 中被 import → 仍报冗余
        (tmp_path / "requirements.txt").write_text("jinja2==3.1.2\n", encoding="utf-8")
        venv = tmp_path / "venv" / "lib"
        venv.mkdir(parents=True)
        (venv / "sitecustomize.py").write_text("import jinja2\n", encoding="utf-8")
        (tmp_path / "app.py").write_text("import os\n", encoding="utf-8")
        assert ("DEP-UNUSED", "jinja2") in unused_rules(scanner.run(make_ctx(tmp_path)))
