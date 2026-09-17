"""manifest 三类清单解析单测：requirements*.txt / package.json / pyproject.toml 正反例。"""

from __future__ import annotations

import json

import pytest

from audit.depcheck import manifest


# ---------------------------------------------------------------- requirements*.txt


class TestParseRequirements:
    def test_basic_and_line_numbers(self):
        text = "Django==3.2.10\nrequests>=2.25.0\n"
        deps = manifest.parse_requirements(text)
        assert [d.name for d in deps] == ["django", "requests"]
        assert deps[0].constraints[0].op == "=="
        assert deps[0].constraints[0].version == "3.2.10"
        assert deps[0].constraints[0].line == 1
        assert deps[1].constraints[0].line == 2

    def test_comments_blank_and_ref_lines_ignored(self):
        text = (
            "# 整行注释\n"
            "\n"
            "-r base.txt\n"
            "--requirement dev.txt\n"
            "-e .\n"
            "--index-url https://example.com/simple\n"
            "flask==2.3.2\n"
        )
        deps = manifest.parse_requirements(text)
        assert [d.name for d in deps] == ["flask"]
        assert deps[0].constraints[0].line == 7

    def test_compound_constraints_and_inline_comment(self):
        text = "requests[security]>=2.25.0, <2.31.0  # pinned for CVE\n"
        deps = manifest.parse_requirements(text)
        assert deps[0].name == "requests"
        assert [(c.op, c.version) for c in deps[0].constraints] == [(">=", "2.25.0"), ("<", "2.31.0")]

    def test_environment_marker_and_tilde_equal(self):
        text = 'jinja2~=3.1.0 ; python_version >= "3.7"\n'
        deps = manifest.parse_requirements(text)
        assert deps[0].name == "jinja2"
        assert [(c.op, c.version) for c in deps[0].constraints] == [("~=", "3.1.0")]

    def test_name_normalization_and_extras(self):
        deps = manifest.parse_requirements("Pillow[tests]==10.1.0\nGit_Python==3.1.30\n")
        assert [d.name for d in deps] == ["pillow", "git-python"]
        assert [d.raw_name for d in deps] == ["Pillow", "Git_Python"]

    def test_empty_and_comment_only_files(self):
        assert manifest.parse_requirements("") == []
        assert manifest.parse_requirements("# 只有注释\n\n") == []

    def test_no_version_pin_has_no_constraints(self):
        deps = manifest.parse_requirements("six\n")
        assert deps[0].name == "six"
        assert deps[0].constraints == []


# ---------------------------------------------------------------- package.json


class TestParsePackageJson:
    def test_dependencies_and_dev_dependencies(self):
        text = json.dumps(
            {
                "name": "app",
                "dependencies": {"lodash": "^4.17.19", "axios": "1.5.1"},
                "devDependencies": {"semver": "~6.3.0"},
            },
            indent=2,
        )
        deps = manifest.parse_package_json(text)
        by_name = {d.name: d for d in deps}
        assert set(by_name) == {"lodash", "axios", "semver"}
        assert by_name["lodash"].ecosystem == "npm"
        assert by_name["lodash"].constraints[0].op == "^"
        assert by_name["axios"].constraints[0].op == "="  # 裸版本视为精确
        assert by_name["semver"].constraints[0].op == "~"

    def test_line_numbers_from_raw_text(self):
        text = '{\n  "dependencies": {\n    "lodash": "^4.0.0"\n  }\n}\n'
        deps = manifest.parse_package_json(text)
        assert deps[0].constraints[0].line == 3

    def test_nested_structure_and_other_keys_ignored(self):
        text = json.dumps(
            {"scripts": {"build": "x"}, "dependencies": {"a": "1.0.0"}, "overrides": {"a": "2.0.0"}},
            indent=1,
        )
        deps = manifest.parse_package_json(text)
        assert [d.name for d in deps] == ["a"]  # 只取 dependencies/devDependencies
        assert deps[0].constraints[0].version == "1.0.0"

    def test_non_version_specs_skipped(self):
        text = json.dumps(
            {
                "dependencies": {
                    "gitdep": "github:user/repo",
                    "any": "*",
                    "latest": "latest",
                    "work": "workspace:^1.0.0",
                    "real": ">=1.2.3",
                }
            }
        )
        deps = manifest.parse_package_json(text)
        by_name = {d.name: d for d in deps}
        assert by_name["gitdep"].constraints == []
        assert by_name["any"].constraints == []
        assert by_name["latest"].constraints == []
        assert by_name["work"].constraints == []
        assert by_name["real"].constraints[0].op == ">="

    def test_bad_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            manifest.parse_package_json("{not json")

    def test_empty_object(self):
        assert manifest.parse_package_json("{}") == []


# ---------------------------------------------------------------- pyproject.toml


class TestParsePyproject:
    def test_project_dependencies_array(self):
        text = (
            '[project]\nname = "demo"\ndependencies = [\n    "jinja2==3.1.2",\n'
            '    "requests>=2.30.0,<3",\n]\n'
        )
        deps = manifest.parse_pyproject(text)
        assert [d.name for d in deps] == ["jinja2", "requests"]
        assert deps[0].constraints[0].line == 4  # 行号回查到真实行
        assert deps[1].constraints[0].line == 5
        assert [(c.op, c.version) for c in deps[1].constraints] == [(">=", "2.30.0"), ("<", "3")]

    def test_no_project_section(self):
        assert manifest.parse_pyproject('[tool.black]\nline-length = 88\n') == []

    def test_bad_toml_raises(self):
        import tomllib

        with pytest.raises(tomllib.TOMLDecodeError):
            manifest.parse_pyproject("not [valid toml")

    def test_missing_dependency_falls_back_to_line_one(self):
        # 依赖由变量拼接、原文回查不到时回退第 1 行（docstring 已注明局限）
        text = '[project]\ndependencies = ["requests>=2.0"]\n'
        deps = manifest.parse_pyproject(text)
        assert deps[0].constraints[0].line == 2


# ---------------------------------------------------------------- 文件分类


class TestClassifyManifest:
    def test_known_manifests(self):
        assert manifest.classify_manifest("requirements.txt") == "requirements"
        assert manifest.classify_manifest("requirements-dev.txt") == "requirements"
        assert manifest.classify_manifest("package.json") == "package_json"
        assert manifest.classify_manifest("pyproject.toml") == "pyproject"

    def test_unknown_files(self):
        assert manifest.classify_manifest("requirements.md") is None
        assert manifest.classify_manifest("setup.py") is None
        assert manifest.classify_manifest("poetry.lock") is None
