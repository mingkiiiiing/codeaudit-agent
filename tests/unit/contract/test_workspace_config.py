"""T1 契约层自测：WorkspaceContext 与 AuditConfig。"""

from audit import config as config_module
from audit.config import AuditConfig
from audit.workspace import WorkspaceContext, is_ignored


def test_config_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GLM_API_KEY", "k-test")
    monkeypatch.setenv("GLM_MODEL", "glm-test-model")
    cfg = AuditConfig.from_env(source_path="C:/tmp/proj")
    assert cfg.api_key == "k-test"
    assert cfg.model == "glm-test-model"
    assert cfg.llm_available

    # W27 卡 A（F7-R1 根治）后语义：.env 值经 _DOTENV_CACHE 跨调用可见，
    # 「delenv 后必然不可用」不再是契约。本断言改为与 .env 存在性解耦的
    # 真契约：环境与 .env 双无 → 不可用（清缓存标志 + 切到无 .env 目录，
    # 避免依赖仓库根是否恰有 .env）。
    monkeypatch.delenv("GLM_API_KEY")
    monkeypatch.setattr(config_module, "_ENV_LOADED", False)
    monkeypatch.setattr(config_module, "_DOTENV_CACHE", None)
    monkeypatch.chdir(tmp_path)
    cfg2 = AuditConfig.from_env()
    assert not cfg2.llm_available


def test_workspace_read_lines(sample_workspace):
    lines = sample_workspace.read_lines("app/services/orders.py", 10, 11)
    assert "user[" in lines[0]
    assert "SELECT * FROM orders" in lines[1]

    full = sample_workspace.read_file_text("app/config.py")
    assert "API_KEY" in full


def test_workspace_rel_and_filter(sample_workspace):
    files = list(sample_workspace.source_files(languages=["python"]))
    rels = {sample_workspace.rel(p) for p in files}
    assert "app/services/orders.py" in rels
    assert "README.md" not in {r for r in rels}  # 非源码语言被过滤
    assert not any("__pycache__" in r for r in rels)


def test_is_ignored():
    from pathlib import Path

    assert is_ignored(Path("node_modules/x/index.js"))
    assert is_ignored(Path("assets/logo.png"))
    assert is_ignored(Path(".codeaudit/abc123/src/main.py"))  # R3-1：工具自身工作区默认忽略
    assert not is_ignored(Path("app/main.py"))


def test_workspace_index_slot(sample_workspace):
    assert sample_workspace.index is None
    sample_workspace.index = object()  # 编排层注入点
    assert sample_workspace.index is not None
