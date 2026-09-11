"""CI 与工程质量自检（W3-A4）：保证 CI workflow 与 ruff 配置本身不腐化。

轻量实现，不引入 pyyaml 等新依赖：
- 对 ci.yml / pyproject.toml 做关键字段文本断言；
- 通过 subprocess 调用 ruff CLI 验证 `ruff check .` 零错误。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

# tests/unit/ci/test_ci_config.py -> 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT = PROJECT_ROOT / "pyproject.toml"


def test_ci_workflow_exists() -> None:
    """CI workflow 文件必须存在（docs/08 G-D 验收项）。"""
    assert CI_WORKFLOW.is_file(), f"缺少 CI workflow: {CI_WORKFLOW}"


def test_ci_workflow_contains_key_fields() -> None:
    """ci.yml 必须包含触发器、矩阵与关键步骤字段（文本断言，不引 YAML 依赖）。"""
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    for needle in (
        "name: CI",            # workflow 名称
        "push:",               # 触发：push
        "branches: [main]",    # push 限定 main
        "pull_request:",       # 触发：PR
        "jobs:",               # job 定义
        "strategy:",           # 测试矩阵
        "matrix:",             # 测试矩阵
        "3.11",                # 下限版本
        "3.13",                # 主版本
        "ubuntu-latest",       # 主 runner
        "windows-latest",      # Windows 兼容性实证条目
        "actions/checkout@v4",
        "actions/setup-python@v5",
        "cache: pip",          # pip 缓存
        'pip install -e ".[dev]"',
        "ruff check .",        # 静态检查步骤
        "pytest -q --tb=short",  # 测试步骤
        "pytest-cov",          # 覆盖率（仅 py3.13 条目）
    ):
        assert needle in text, f"ci.yml 缺少关键字段: {needle!r}"


def test_pyproject_contains_ruff_section() -> None:
    """pyproject.toml 必须包含 [tool.ruff] 配置节与核心字段。"""
    text = PYPROJECT.read_text(encoding="utf-8")
    for needle in (
        "[tool.ruff]",
        "line-length = 120",
        'target-version = "py311"',
        "[tool.ruff.lint]",
        "per-file-ignores",
    ):
        assert needle in text, f"pyproject.toml [tool.ruff] 缺少关键字段: {needle!r}"


def test_pyproject_dependency_sections_untouched() -> None:
    """看门断言：ruff 配置节的追加不得波及依赖与 pytest 配置（W3 协作规约第 2 条）。"""
    text = PYPROJECT.read_text(encoding="utf-8")
    assert '[project.optional-dependencies]\ndev = ["pytest>=8", "pytest-asyncio>=0.23"]' in text
    assert "asyncio_mode = \"auto\"" in text


def test_ruff_check_zero_errors() -> None:
    """ruff CLI 全仓检查必须零错误（与 CI lint 步骤同一门槛）。"""
    ruff = shutil.which("ruff")
    if ruff is None:
        # CI 的矩阵条目中 ruff 仅安装在 lint 步骤所在环境；未安装则交由 CI 把关
        pytest.skip("ruff 不在 PATH 中，跳过本地检查（CI lint 步骤负责把关）")
    proc = subprocess.run(
        [ruff, "check", "."],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, f"ruff check 失败:\n{proc.stdout}\n{proc.stderr}"
