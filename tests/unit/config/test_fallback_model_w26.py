"""W26 卡 B 单测：fallback_model（GLM_MODEL_FALLBACK）接入 from_sources 环境变量层。

docs/24 §4 收尾②：W25 卡 B 只接了 from_env 的 env 层，CLI/配置文件合成路径
（from_sources）未暴露。本卡补齐，合成优先级与既有键一致（契约 v1.4）：
默认值 < 配置文件（.codeaudit.toml）< 环境变量 GLM_MODEL_FALLBACK < CLI 显式参数。

隔离约定（与 test_config_w14.py 同款）：GLM_MODEL_FALLBACK 每用例 delenv
（monkeypatch 用例后恢复），from_sources 一律传 search_root=tmp_path 隔离 CWD
的真实配置文件。该键不在 .env 白名单（audit.config._DOTENV_GLM_KEYS 未扩），
.env 不影响断言，全程零网络。
"""

from __future__ import annotations

import pytest

from audit.config import AuditConfig

_ENV = "GLM_MODEL_FALLBACK"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """每用例清掉 fallback 环境变量，防开发机真实环境串扰。"""
    monkeypatch.delenv(_ENV, raising=False)


# ---------------------------------------------------------------- env 层接入


def test_from_sources_reads_fallback_env(monkeypatch, tmp_path):
    """env 层：GLM_MODEL_FALLBACK 注入 from_sources 产物（W25 仅 from_env 可读）。"""
    monkeypatch.setenv(_ENV, "glm-fb-env")
    cfg = AuditConfig.from_sources(cli_overrides={"source_path": "C:/tmp/proj"}, search_root=tmp_path)
    assert cfg.fallback_model == "glm-fb-env"


def test_from_sources_without_env_keeps_default_empty(monkeypatch, tmp_path):
    """无环境变量、无配置文件 → 默认空串（= 关闭 fallback，行为零变化）。"""
    cfg = AuditConfig.from_sources(search_root=tmp_path)
    assert cfg.fallback_model == ""


# ---------------------------------------------------------------- 合成优先级


def test_from_sources_env_overrides_config_file(monkeypatch, tmp_path):
    """环境变量层高于配置文件层（与 sandbox_backend 等既有键同序，文件层不越级）。"""
    (tmp_path / ".codeaudit.toml").write_text('fallback_model = "glm-fb-file"\n', encoding="utf-8")
    monkeypatch.setenv(_ENV, "glm-fb-env")
    cfg = AuditConfig.from_sources(search_root=tmp_path)
    assert cfg.fallback_model == "glm-fb-env"


def test_from_sources_config_file_layer_kept_and_cli_highest(monkeypatch, tmp_path):
    """无 env 时配置文件层照常生效（既有用法不破坏）；CLI 显式参数层最高。"""
    (tmp_path / ".codeaudit.toml").write_text('fallback_model = "glm-fb-file"\n', encoding="utf-8")
    assert AuditConfig.from_sources(search_root=tmp_path).fallback_model == "glm-fb-file"
    monkeypatch.setenv(_ENV, "glm-fb-env")
    cfg = AuditConfig.from_sources({"fallback_model": "glm-fb-cli"}, search_root=tmp_path)
    assert cfg.fallback_model == "glm-fb-cli"
