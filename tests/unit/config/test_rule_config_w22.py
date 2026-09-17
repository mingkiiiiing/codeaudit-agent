"""W22-C 规则级配置合成测试：disabled_rules / severity_overrides / ignore_paths。

覆盖三层：

- 默认值：三字段空容器 = 既有行为零变化；
- 配置文件合成：.codeaudit.toml 顶层三键经 from_sources 进 config（未知键仍走
  既有 config_warnings 通道——三键是合法 dataclass 字段，不触发未知键警告）；
- CLI 显式参数层：--disable-rule / --ignore-path 的 overrides 合并优先于配置文件。

engine 侧行为（禁用后零命中 / 严重度改写 / 路径跳过）见
tests/unit/detect/test_engine_rule_config_w22.py。
"""

from __future__ import annotations

from audit.config import AuditConfig


def test_rule_config_fields_default_empty():
    """默认构造：三字段为空容器（行为零变化的基线）。"""
    config = AuditConfig()
    assert config.disabled_rules == []
    assert config.severity_overrides == {}
    assert config.ignore_paths == []


def test_rule_config_from_toml(monkeypatch, tmp_path):
    """.codeaudit.toml 顶层三键全部进入合成配置，且不产生未知键警告。"""
    monkeypatch.chdir(tmp_path)  # 隔离 CWD：不读仓库根真实配置
    (tmp_path / ".codeaudit.toml").write_text(
        "\n".join(
            [
                "disabled_rules = [\"PY-EQ-NONE\", \"JS-DOUBLE-EQ-NULL\"]",
                "ignore_paths = [\"vendor/\", \"**/*_generated.py\"]",
                "[severity_overrides]",
                "PY-PRINT-DEBUG = \"medium\"",
                "PY-BARE-EXCEPT = \"high\"",
            ]
        ),
        encoding="utf-8",
    )
    config = AuditConfig.from_sources()
    assert config.disabled_rules == ["PY-EQ-NONE", "JS-DOUBLE-EQ-NULL"]
    assert config.severity_overrides == {"PY-PRINT-DEBUG": "medium", "PY-BARE-EXCEPT": "high"}
    assert config.ignore_paths == ["vendor/", "**/*_generated.py"]
    assert config.config_warnings == []


def test_rule_config_cli_overrides_win_over_file(monkeypatch, tmp_path):
    """CLI 显式参数层覆盖配置文件（合成优先级铁律对三键同样成立）。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".codeaudit.toml").write_text(
        'disabled_rules = ["PY-EQ-NONE"]\nignore_paths = ["vendor/"]\n',
        encoding="utf-8",
    )
    config = AuditConfig.from_sources(
        cli_overrides={
            "disabled_rules": ["PY-PRINT-DEBUG"],
            "ignore_paths": ["gen/**"],
        }
    )
    assert config.disabled_rules == ["PY-PRINT-DEBUG"]
    assert config.ignore_paths == ["gen/**"]


def test_rule_config_toml_via_pyproject_section(monkeypatch, tmp_path):
    """pyproject.toml 的 [tool.codeaudit] 节同样支持三键（自动发现回退路径）。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[tool.codeaudit]",
                'disabled_rules = ["PY-MAGIC-NUMBER"]',
                "[tool.codeaudit.severity_overrides]",
                'PY-EQ-NONE = "medium"',
            ]
        ),
        encoding="utf-8",
    )
    config = AuditConfig.from_sources()
    assert config.disabled_rules == ["PY-MAGIC-NUMBER"]
    assert config.severity_overrides == {"PY-EQ-NONE": "medium"}
