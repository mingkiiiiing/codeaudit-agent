"""W19-E：package-lock.json 传递依赖解析与 DEP-CVE 扫描单测。

覆盖：lockfileVersion 3（根跳过/dev 跳过/行号断言）、lockfileVersion 1
（顶层 + 递归一层）、lockfileVersion 2 并存去重、lodash 4.17.20 与种子库
CVE-2021-23337 命中、安全版本不命中、>5MB 跳过记账、条目数上限、行号回退
标注、与 package.json/requirements.txt 并存时 lock 只出 CVE 且 UNUSED 不回归。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from audit.depcheck import manifest, scanner
from audit.models import Severity

# lockfileVersion 3 形态：lodash 条目在第 18 行（根 "" 在第 7 行、dev 条目
# left-pad 在第 14 行——前者跳过，后者 dev 跳过）
LV3_LOCK = """{
  "name": "demo",
  "version": "1.0.0",
  "lockfileVersion": 3,
  "requires": true,
  "packages": {
    "": {
      "name": "demo",
      "version": "1.0.0",
      "dependencies": {
        "lodash": "^4.17.20"
      }
    },
    "node_modules/left-pad": {
      "version": "1.3.0",
      "dev": true
    },
    "node_modules/lodash": {
      "version": "4.17.20",
      "resolved": "https://registry.npmjs.org/lodash/-/lodash-4.17.20.tgz"
    }
  }
}
"""

# lockfileVersion 1 形态：顶层 lodash 第 5 行、dev 条目 left-pad 第 9 行
# （跳过）、foreground-child 第 13 行、其嵌套 lodash 第 17 行
LV1_LOCK = """{
  "name": "demo",
  "lockfileVersion": 1,
  "dependencies": {
    "lodash": {
      "version": "4.17.20",
      "dev": false
    },
    "left-pad": {
      "version": "1.3.0",
      "dev": true
    },
    "foreground-child": {
      "version": "2.0.0",
      "dependencies": {
        "lodash": {
          "version": "3.10.1"
        }
      }
    }
  }
}
"""


def make_ctx(src_root, extra=None) -> SimpleNamespace:
    """与 test_scanner_contract 同款最小 ctx（鸭子类型兼容 PipelineContext）。"""
    return SimpleNamespace(
        workspace=SimpleNamespace(src_root=src_root),
        extra=extra if extra is not None else {},
    )


def rules_of(issues):
    """提取全部 rule:* 证据（顺序敏感）。"""
    return [e[len("rule:"):] for i in issues for e in i.evidence if e.startswith("rule:")]


# ---------------------------------------------------------------- manifest.parse_package_lock


class TestParsePackageLock:
    def test_lockfile_v3_root_dev_skipped_and_line(self, tmp_path):
        path = tmp_path / "package-lock.json"
        path.write_text(LV3_LOCK, encoding="utf-8")
        deps = manifest.parse_package_lock(path)
        assert [d.name for d in deps] == ["lodash"]  # 根 "" 与 dev left-pad 跳过
        dep = deps[0]
        assert dep.ecosystem == "npm"
        assert len(dep.constraints) == 1
        con = dep.constraints[0]
        assert con.op == "==" and con.version == "4.17.20"
        assert con.line == 18  # 构造已知行：node_modules/lodash 键所在行

    def test_lockfile_v1_top_and_nested_one_level(self, tmp_path):
        path = tmp_path / "package-lock.json"
        path.write_text(LV1_LOCK, encoding="utf-8")
        deps = manifest.parse_package_lock(path)
        got = [(d.name, d.constraints[0].version, d.constraints[0].line) for d in deps]
        # 顶层 lodash(5) + foreground-child(13) + 嵌套一层 lodash(16，键行口径)；
        # dev left-pad 跳过
        assert got == [("lodash", "4.17.20", 5), ("foreground-child", "2.0.0", 13), ("lodash", "3.10.1", 16)]

    def test_v1_nested_beyond_one_level_not_parsed(self, tmp_path):
        text = json.dumps(
            {
                "lockfileVersion": 1,
                "dependencies": {
                    "a": {
                        "version": "1.0.0",
                        "dependencies": {
                            "b": {"version": "2.0.0", "dependencies": {"c": {"version": "3.0.0"}}}
                        },
                    }
                },
            }
        )
        path = tmp_path / "package-lock.json"
        path.write_text(text, encoding="utf-8")
        deps = manifest.parse_package_lock(path)
        assert [d.name for d in deps] == ["a", "b"]  # 更深嵌套的 c 不做

    def test_v2_packages_preferred_no_duplicates(self, tmp_path):
        text = json.dumps(
            {
                "lockfileVersion": 2,
                "packages": {"node_modules/lodash": {"version": "4.17.20"}},
                "dependencies": {"lodash": {"version": "4.17.20"}},
            }
        )
        path = tmp_path / "package-lock.json"
        path.write_text(text, encoding="utf-8")
        deps = manifest.parse_package_lock(path)
        assert [(d.name, d.constraints[0].version) for d in deps] == [("lodash", "4.17.20")]

    def test_non_node_modules_keys_skipped(self, tmp_path):
        text = json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "ws-demo"},
                    "apps/server": {"version": "1.0.0"},  # workspace 相对路径键
                    "node_modules/lodash": {"version": "4.17.20"},
                },
            }
        )
        path = tmp_path / "package-lock.json"
        path.write_text(text, encoding="utf-8")
        deps = manifest.parse_package_lock(path)
        assert [d.name for d in deps] == ["lodash"]

    def test_non_version_spec_skipped(self, tmp_path):
        text = json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "node_modules/local-pkg": {"version": "file:../local-pkg"},
                    "node_modules/git-pkg": {"version": "github:a/b#main"},
                    "node_modules/lodash": {"version": "4.17.20"},
                },
            }
        )
        path = tmp_path / "package-lock.json"
        path.write_text(text, encoding="utf-8")
        deps = manifest.parse_package_lock(path)
        assert [d.name for d in deps] == ["lodash"]

    def test_nested_scoped_name_resolved(self, tmp_path):
        text = json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "node_modules/@types/node": {"version": "18.0.0"},
                    "node_modules/a/node_modules/lodash": {"version": "3.10.1"},
                },
            }
        )
        path = tmp_path / "package-lock.json"
        path.write_text(text, encoding="utf-8")
        deps = manifest.parse_package_lock(path)
        assert [d.name for d in deps] == ["@types/node", "lodash"]  # 嵌套取最后一段

    def test_entry_cap_only_first_n(self, tmp_path, monkeypatch):
        monkeypatch.setattr(manifest, "MAX_LOCK_ENTRIES", 2)
        text = json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "demo"},  # 计入预算但被跳过
                    "node_modules/a": {"version": "1.0.0"},
                    "node_modules/b": {"version": "2.0.0"},  # 预算耗尽，不解析
                    "node_modules/lodash": {"version": "4.17.20"},
                },
            }
        )
        path = tmp_path / "package-lock.json"
        path.write_text(text, encoding="utf-8")
        deps = manifest.parse_package_lock(path)
        assert [d.name for d in deps] == ["a"]

    def test_bad_json_raises(self, tmp_path):
        path = tmp_path / "package-lock.json"
        path.write_text("{broken json", encoding="utf-8")
        try:
            manifest.parse_package_lock(path)
        except json.JSONDecodeError:
            pass  # 与 parse_package_json 同口径：抛 JSONDecodeError 由调用方容错
        else:
            raise AssertionError("损坏 JSON 应抛 json.JSONDecodeError")


# ---------------------------------------------------------------- scanner：lock 只出 DEP-CVE


class TestScannerLockCve:
    def test_lodash_4_17_20_hits_seed_db(self, tmp_path):
        (tmp_path / "package-lock.json").write_text(LV3_LOCK, encoding="utf-8")
        issues = scanner.run(make_ctx(tmp_path))
        assert rules_of(issues) == ["DEP-CVE-CVE-2021-23337"]
        issue = issues[0]
        assert issue.file == "package-lock.json"
        assert issue.line_start == 18 and issue.line_end == 18
        assert issue.severity == Severity.HIGH  # CVSS 7.2
        assert "cve:CVE-2021-23337" in issue.evidence
        assert "loc:package-lock.json:18" in issue.evidence
        assert "line_fallback:1" not in issue.evidence  # 行号回查成功不标注

    def test_safe_version_no_hit(self, tmp_path):
        text = LV3_LOCK.replace('"4.17.20"', '"4.17.21"')
        (tmp_path / "package-lock.json").write_text(text, encoding="utf-8")
        issues = scanner.run(make_ctx(tmp_path))
        assert issues == []  # 修复版本之外无命中

    def test_oversize_lock_skipped_and_recorded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scanner, "LOCK_MAX_BYTES", 10)
        (tmp_path / "package-lock.json").write_text(LV3_LOCK, encoding="utf-8")
        extra: dict = {}
        issues = scanner.run(make_ctx(tmp_path, extra=extra))
        assert issues == []  # 超限跳过，不产出 CVE
        errs = extra["post_scan_errors"]
        assert "depcheck.lock:package-lock.json" in errs
        assert "LockFileTooLarge" in errs["depcheck.lock:package-lock.json"]

    def test_bad_json_lock_skipped_silently(self, tmp_path):
        (tmp_path / "package-lock.json").write_text("{broken json", encoding="utf-8")
        extra: dict = {}
        issues = scanner.run(make_ctx(tmp_path, extra=extra))
        assert issues == []  # 坏文件与既有清单路径同口径：跳过不记账
        assert not [k for k in extra.get("post_scan_errors", {}) if k.startswith("depcheck.lock")]

    def test_parse_failure_recorded_by_step_guard(self, tmp_path, monkeypatch):
        def boom(_src_root):
            raise RuntimeError("injected failure")

        # 步骤级故障由 run() 外层 try 记账（单文件解析异常走内层静默跳过口径）
        monkeypatch.setattr(scanner, "_collect_lock_files", boom)
        (tmp_path / "package-lock.json").write_text(LV3_LOCK, encoding="utf-8")
        extra: dict = {}
        issues = scanner.run(make_ctx(tmp_path, extra=extra))
        assert issues == []
        assert extra["post_scan_errors"]["depcheck.lock"].startswith("RuntimeError")

    def test_minified_lock_line_fallback_annotated(self, tmp_path):
        minified = json.dumps(json.loads(LV3_LOCK), separators=(",", ":"))
        (tmp_path / "package-lock.json").write_text(minified, encoding="utf-8")
        issues = scanner.run(make_ctx(tmp_path))
        assert rules_of(issues) == ["DEP-CVE-CVE-2021-23337"]
        issue = issues[0]
        assert issue.line_start == 1  # 压缩单行回查不到条目行，回退 1
        assert "line_fallback:1" in issue.evidence  # evidence 标注回退

    def test_coexist_with_package_json_lock_cve_only(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "demo", "dependencies": {"lodash": "^4.0.0"}}), encoding="utf-8"
        )
        (tmp_path / "package-lock.json").write_text(LV3_LOCK, encoding="utf-8")
        (tmp_path / "app.js").write_text('const _ = require("lodash");\n', encoding="utf-8")
        issues = scanner.run(make_ctx(tmp_path))
        rules = rules_of(issues)
        # 各自扫各自的：package.json 区间命中 + lock 精确版本命中
        assert rules == ["DEP-CVE-CVE-2021-23337", "DEP-CVE-CVE-2021-23337"]
        files = [i.file for i in issues]
        assert sorted(files) == ["package-lock.json", "package.json"]
        # lock 只出 CVE：lock 内生产依赖 left-pad 不触发 UNUSED（lodash 有引用同样不触发）
        assert not any(r.startswith("DEP-UNUSED") for r in rules)

    def test_requirements_unused_semantics_not_regressed(self, tmp_path):
        (tmp_path / "requirements.txt").write_text(
            "flask==2.3.2\nnumpy==1.24.0\n", encoding="utf-8"
        )
        (tmp_path / "main.py").write_text("import flask\n", encoding="utf-8")
        (tmp_path / "package-lock.json").write_text(LV3_LOCK, encoding="utf-8")
        issues = scanner.run(make_ctx(tmp_path))
        rules = rules_of(issues)
        assert "DEP-CVE-CVE-2021-23337" in rules  # lock 命中照常
        unused = [i for i in issues if "rule:DEP-UNUSED" in i.evidence]
        assert len(unused) == 1 and unused[0].file == "requirements.txt"  # UNUSED 口径不变

    def test_depcheck_off_disables_lock(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEAUDIT_DEPCHECK_OFF", "1")
        (tmp_path / "package-lock.json").write_text(LV3_LOCK, encoding="utf-8")
        issues = scanner.run(make_ctx(tmp_path))
        assert issues == []  # 总开关一并关闭 lock 扫描
