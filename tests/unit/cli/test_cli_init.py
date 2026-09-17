"""W24-B init 子命令自测：.codeaudit.toml 注释模板骨架生成与拒绝覆盖。"""

from __future__ import annotations

from pathlib import Path

import pytest
import tomllib

import cli
from audit.config import AuditConfig

# 模板必须覆盖的三键（任务卡口径）
_INIT_KEYS = ("disabled_rules", "severity_overrides", "ignore_paths")


def test_init_writes_commented_template(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """init 在指定目录生成模板：三键齐备且全部注释（骨架不改变默认行为）。"""
    rc = cli.main(["init", str(tmp_path)])
    assert rc == 0
    target = tmp_path / ".codeaudit.toml"
    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    for key in _INIT_KEYS:
        assert key in text, key
        assert f"# {key}" in text or f"# {key} " in text or f"# [{key}]" in text  # 注释态
    # 全注释模板可被 TOML 解析且不含任何生效键（配置零变化）
    data = tomllib.loads(text)
    assert data == {}
    config = AuditConfig.from_sources(config_file=str(target))
    assert config.config_warnings == []
    assert config.disabled_rules == [] and config.ignore_paths == [] and config.severity_overrides == {}
    assert "已生成配置模板" in capsys.readouterr().out


def test_init_default_dir_is_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """不带目录参数：写入当前目录。"""
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init"]) == 0
    assert (tmp_path / ".codeaudit.toml").is_file()


def test_init_refuses_overwrite_exit_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """目标已存在：拒绝覆盖、退出码 1、既有内容原样保留。"""
    target = tmp_path / ".codeaudit.toml"
    target.write_text('disabled_rules = ["PY-EQ-NONE"]\n', encoding="utf-8")
    rc = cli.main(["init", str(tmp_path)])
    assert rc == 1
    assert "拒绝覆盖" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == 'disabled_rules = ["PY-EQ-NONE"]\n'
