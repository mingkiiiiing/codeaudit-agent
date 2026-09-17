"""W14-A2 单测：sandbox_backend 配置分层（M-2a）与 max_tool_iterations 默认值（M-1）。

覆盖面：
- AuditConfig 默认值：sandbox_backend="subprocess"（W13 收口裁决：Docker 必须显式
  opt-in）、max_tool_iterations=12（M-1：与历史硬编码行为一致）；
- from_env：CODEAUDIT_SANDBOX_BACKEND 环境变量覆盖；显式 overrides 仍高于环境变量；
- from_sources 三层：配置文件 < 环境变量 < CLI 显式参数；非法值原样保存（降级
  职责在 SandboxExecutor，诚实降级并标注）。

隔离约定：CODEAUDIT_SANDBOX_BACKEND 每用例 delenv/setenv（monkeypatch 用例后恢复），
from_sources 一律传 search_root=tmp_path 隔离 CWD 的真实配置文件。
"""

from __future__ import annotations

import pytest

from audit.config import AuditConfig

_ENV = "CODEAUDIT_SANDBOX_BACKEND"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """每用例清掉沙箱后端环境变量，防开发机真实环境串扰。"""
    monkeypatch.delenv(_ENV, raising=False)


# ---------------------------------------------------------------- 默认值


def test_default_sandbox_backend_is_subprocess():
    """默认 subprocess：行为与 W13 收口裁决一致（Docker opt-in，不自动启用）。"""
    assert AuditConfig().sandbox_backend == "subprocess"


def test_default_max_tool_iterations_is_12():
    """M-1：默认 12（原 25 无任何消费点属死配置；12 与历史硬编码行为一致）。"""
    assert AuditConfig().max_tool_iterations == 12


# ---------------------------------------------------------------- from_env 层


def test_from_env_reads_sandbox_backend(monkeypatch):
    """环境变量覆盖默认值（server 侧 from_env 路径由此获得开关）。"""
    monkeypatch.setenv(_ENV, "docker")
    cfg = AuditConfig.from_env(source_path="x")
    assert cfg.sandbox_backend == "docker"


def test_from_env_explicit_override_beats_env(monkeypatch):
    """显式 overrides（如调用方传参）仍高于环境变量——from_env 优先级语义不变。"""
    monkeypatch.setenv(_ENV, "docker")
    cfg = AuditConfig.from_env(source_path="x", sandbox_backend="subprocess")
    assert cfg.sandbox_backend == "subprocess"


def test_from_env_without_env_keeps_default(monkeypatch):
    """无环境变量 → 默认值（不误读其他 CODEAUDIT_* 键）。"""
    cfg = AuditConfig.from_env(source_path="x")
    assert cfg.sandbox_backend == "subprocess"


# ---------------------------------------------------------------- from_sources 分层


def test_from_sources_config_file_layer(tmp_path, monkeypatch):
    """配置文件层：.codeaudit.toml 的 sandbox_backend 生效（无 env 无 CLI 时）。"""
    (tmp_path / ".codeaudit.toml").write_text('sandbox_backend = "docker"\n', encoding="utf-8")
    cfg = AuditConfig.from_sources(search_root=tmp_path)
    assert cfg.sandbox_backend == "docker"


def test_from_sources_env_overrides_config_file(tmp_path, monkeypatch):
    """环境变量层高于配置文件层。"""
    (tmp_path / ".codeaudit.toml").write_text('sandbox_backend = "docker"\n', encoding="utf-8")
    monkeypatch.setenv(_ENV, "subprocess")
    cfg = AuditConfig.from_sources(search_root=tmp_path)
    assert cfg.sandbox_backend == "subprocess"


def test_from_sources_cli_beats_env_and_file(tmp_path, monkeypatch):
    """CLI 显式参数层最高（契约 v1.4：默认 < 文件 < env < CLI）。"""
    (tmp_path / ".codeaudit.toml").write_text('sandbox_backend = "docker"\n', encoding="utf-8")
    monkeypatch.setenv(_ENV, "docker")
    cfg = AuditConfig.from_sources({"sandbox_backend": "subprocess"}, search_root=tmp_path)
    assert cfg.sandbox_backend == "subprocess"


def test_from_sources_invalid_value_kept_verbatim(tmp_path, monkeypatch):
    """非法值原样保存进配置：归一/降级职责在 SandboxExecutor（诚实降级并标注）。"""
    monkeypatch.setenv(_ENV, "podman")
    cfg = AuditConfig.from_sources(search_root=tmp_path)
    assert cfg.sandbox_backend == "podman"
