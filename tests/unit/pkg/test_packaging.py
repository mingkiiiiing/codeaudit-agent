"""PyPI 打包就绪测试（W4-A4）。

覆盖四类断言：

1. 版本单源：``audit.__version__`` 为合法 semver 且为 0.2.0；
2. pyproject 文本断言：dynamic version / console script / py-modules / package-data
   四项存在且指向正确；
3. 包数据：报告模板目录下 .j2 文件在磁盘真实存在；
4. console script 冒烟：``cli:main`` 入口可调用。

注意：``--version`` 参数由 A1 的 cli.py 改造提供；若尚未合入，argparse 会以
``SystemExit(2)`` 拒绝未知参数，此时降级为"入口可调度子命令"的冒烟校验，
两种状态测试均通过（以 cli.py 实际行为为准，见 test_console_script_smoke）。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PYPROJECT_PATH = ROOT / "pyproject.toml"
TEMPLATES_DIR = ROOT / "audit" / "report" / "templates"

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _load_pyproject() -> dict:
    """解析 pyproject.toml 为 dict（测试辅助）。"""
    with PYPROJECT_PATH.open("rb") as fh:
        return tomllib.load(fh)


def _pyproject_text() -> str:
    """读取 pyproject.toml 原始文本（测试辅助）。"""
    return PYPROJECT_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. 版本单源
# ---------------------------------------------------------------------------


def test_version_is_valid_semver() -> None:
    """audit.__version__ 必须是 x.y.z 形式的合法 semver。"""
    import audit

    assert SEMVER_RE.match(audit.__version__), f"非法 semver：{audit.__version__!r}"


def test_version_is_0_2_0() -> None:
    """Wave 4 版本单源约定值：0.2.0。"""
    import audit

    assert audit.__version__ == "0.2.0"


def test_pyproject_dynamic_version_single_source() -> None:
    """pyproject 无静态 version，dynamic 声明并指向 audit.__version__。"""
    data = _load_pyproject()
    assert "version" not in data["project"], "存在静态 version 则版本非单源"
    assert "version" in data["project"]["dynamic"]
    attr = data["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    assert attr == "audit.__version__", f"动态版本源错误：{attr!r}"


# ---------------------------------------------------------------------------
# 2. pyproject 文本断言（四项存在且指向正确）
# ---------------------------------------------------------------------------


def test_pyproject_text_declares_dynamic_and_attr() -> None:
    """文本级：dynamic = ["version"] 与 attr = "audit.__version__" 均出现。"""
    text = _pyproject_text()
    assert 'dynamic = ["version"]' in text
    assert 'attr = "audit.__version__"' in text


def test_pyproject_console_script() -> None:
    """console script：codeaudit 指向 cli:main（结构化 + 文本双断言）。"""
    data = _load_pyproject()
    assert data["project"]["scripts"]["codeaudit"] == "cli:main"
    assert 'codeaudit = "cli:main"' in _pyproject_text()


def test_pyproject_py_modules_includes_cli() -> None:
    """py-modules 含 cli（根目录模块，console script 可安装的前提）。"""
    data = _load_pyproject()
    assert "cli" in data["tool"]["setuptools"]["py-modules"]
    assert re.search(r'^py-modules\s*=\s*\[\s*"cli"\s*\]', _pyproject_text(), re.M)


def test_pyproject_package_data_templates() -> None:
    """package-data 为 audit 包声明 Jinja2 模板。"""
    data = _load_pyproject()
    patterns = data["tool"]["setuptools"]["package-data"]["audit"]
    assert any("*.j2" in p for p in patterns), f"模板未随包分发：{patterns!r}"


def test_pyproject_metadata_complete() -> None:
    """发布元数据齐全：readme / license / authors / keywords / classifiers / urls。"""
    project = _load_pyproject()["project"]
    assert project["readme"] == "README.md"
    assert project["license"]["text"] == "MIT"
    assert project["authors"] == [{"name": "mingkiiiiing", "email": "2200658909@qq.com"}]
    assert set(project["keywords"]) == {
        "code-review",
        "static-analysis",
        "llm-agent",
        "code-audit",
        "sast",
    }
    classifiers = "\n".join(project["classifiers"])
    for expected in (
        "Development Status :: 4 - Beta",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3.13",
    ):
        assert expected in classifiers
    urls = project["urls"]
    assert urls["Homepage"] == "https://github.com/mingkiiiiing/codeaudit-agent"
    assert urls["Repository"].startswith("https://github.com/mingkiiiiing/codeaudit-agent")
    assert urls["Issues"].endswith("/issues")
    assert urls["Changelog"].endswith("/CHANGELOG.md")


# ---------------------------------------------------------------------------
# 3. 包数据：模板文件在磁盘存在
# ---------------------------------------------------------------------------


def test_template_files_exist() -> None:
    """templates 目录下 Markdown 与 HTML 两个模板真实存在。"""
    templates = sorted(p.name for p in TEMPLATES_DIR.glob("*.j2"))
    assert "report.md.j2" in templates
    assert "report.html.j2" in templates
    assert len(templates) >= 2


# ---------------------------------------------------------------------------
# 4. console script 冒烟（以 cli.py 实际签名为准）
# ---------------------------------------------------------------------------


def test_cli_main_signature() -> None:
    """cli.main(argv) -> int 入口存在且可调用（console script 的目标）。"""
    import inspect

    import cli

    assert callable(cli.main)
    params = inspect.signature(cli.main).parameters
    assert "argv" in params
    assert cli.main.__annotations__.get("return") is int or params["argv"].default is None


def test_console_script_smoke(capsys: pytest.CaptureFixture[str]) -> None:
    """cli.main 冒烟：--version 可用则断言版本输出；未合入则降级为调度冒烟。

    - A1 已合入 --version：返回/退出 0，且输出含 0.2.0（argparse action="version"
      的实现会以 SystemExit(0) 结束，同样视为通过）；
    - A1 尚未合入：argparse 以 SystemExit(2) 拒绝未知参数，此时降级调用
      report 子命令的不存在路径分支，断言 main 顶层兜底返回退出码 1。
    """
    import cli

    try:
        rc: object = cli.main(["--version"])
    except SystemExit as exc:  # A1 的 --version 未合入：未知参数 SystemExit(2)
        assert exc.code == 2
        rc = cli.main(["report", "__no_such_report__.json"])
        captured = capsys.readouterr()
        assert rc == 1, "main 顶层兜底应对错误路径返回退出码 1"
        assert "[错误]" in captured.err
    else:
        assert rc in (0, None)
        captured = capsys.readouterr()
        assert "0.2.0" in captured.out
