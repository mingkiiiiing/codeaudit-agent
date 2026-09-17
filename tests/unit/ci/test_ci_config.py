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
PR_AUDIT_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "pr-audit.yml"
PYPROJECT = PROJECT_ROOT / "pyproject.toml"

# W15-C 棘轮基线：与 ci.yml 覆盖率步骤的 --cov-fail-under 保持同源（改动须两处同步上调）。
# 2026-09-15 全量实测 TOTAL 87%，向下取整到 5 的倍数 => 85。
COV_FAIL_UNDER = 85


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
        "actions/checkout@v7",
        "actions/setup-python@v7",
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
    # W20-B 适配：dev 依赖钉扎按升级任务合法变更为 pytest>=9.0.3 / pytest-asyncio>=1.4
    # （堵 PYSEC-2026-1845），原硬编码 "pytest>=8" 断言随之同步更新，断言意图不变。
    assert "[project.optional-dependencies]" in text
    assert "pytest>=9.0.3" in text
    assert "pytest-asyncio>=1.4" in text
    assert "asyncio_mode = \"auto\"" in text


# ---------------------------------------------------------------------------
# W15-C：依赖下限钉扎与 CI 门禁自检（防 CI 配置腐化，与既有断言同一风格）。
# ---------------------------------------------------------------------------

def test_pyproject_dependency_security_floors() -> None:
    """看门断言：依赖安全下限不得被回退（W15-C 堵 multipart/jinja2/starlette 已知 CVE）。"""
    text = PYPROJECT.read_text(encoding="utf-8")
    for needle in (
        "fastapi>=0.115",           # 起 starlette>=0.40，堵 CVE-2024-47874
        "jinja2>=3.1.6",            # 堵 CVE-2024-22195 / CVE-2024-34032 / CVE-2025-27516
        "python-multipart>=0.0.18",  # 堵 CVE-2024-53981
    ):
        assert needle in text, f"pyproject.toml 依赖安全下限缺失或被回退: {needle!r}"


def test_ci_workflow_no_issues_write_permission() -> None:
    """看门断言：ci.yml 不得声明 issues: write（W15-C 权限收减，无任何 issues 操作）。"""
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "issues: write" not in text, "ci.yml 重新引入了 issues: write 权限"


def test_ci_workflow_coverage_gate() -> None:
    """看门断言：覆盖率条目必须带 --cov-fail-under 棘轮门禁，且与常量同源（W15-C）。"""
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    needle = f"--cov-fail-under={COV_FAIL_UNDER}"
    assert needle in text, f"ci.yml 覆盖率步骤缺少棘轮门禁 {needle!r}"


def test_ci_workflow_pip_audit_step() -> None:
    """看门断言：ci.yml 必须包含 pip-audit 依赖漏洞审计步骤（W15-C）。"""
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    # 注意命令名是带连字符的 pip-audit（`pip audit` 不是有效子命令，已实测）
    for needle in ("pip install pip-audit", "pip-audit --strict"):
        assert needle in text, f"ci.yml 缺少 pip-audit 关键命令: {needle!r}"


def test_pr_audit_workflow_exists_and_shape() -> None:
    """看门断言：pr-audit.yml 存在且形态正确（W15-C 自举审计：离线/无 secrets/critical 门禁）。"""
    assert PR_AUDIT_WORKFLOW.is_file(), f"缺少 PR 自审计 workflow: {PR_AUDIT_WORKFLOW}"
    text = PR_AUDIT_WORKFLOW.read_text(encoding="utf-8")
    for needle in (
        "pull_request:",                       # 触发：PR
        "workflow_dispatch",                   # 触发：手动
        "concurrency:",                        # 防运行叠加
        "timeout-minutes:",                    # 兜底超时
        "--fail-on critical",                  # 门禁阈值：critical
        "--diff origin/main",                  # 增量审计
        "--format sarif",                      # SARIF 产物
        "actions/upload-artifact",             # 产物上传
    ):
        assert needle in text, f"pr-audit.yml 缺少关键字段: {needle!r}"
    # 零 secrets 依赖：任何形式的 secrets 引用都不允许出现
    assert "secrets." not in text, "pr-audit.yml 引用了 secrets，违背零 secrets 设计"


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
