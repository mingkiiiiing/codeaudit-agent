"""W24-B doctor 子命令自测：零 Key 环境自检的各检查项与整体行为。

tmp_path 伪造环境（.env / .codeaudit.toml），语言包/docker/git 探测经
monkeypatch 注入替身，全程离线、不触网、不依赖本机 Docker 状态。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import cli


# ---------------------------------------------------------------- 整体行为


def test_doctor_runs_without_api_key_and_prints_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """doctor 零 Key 可跑：输出中文诊断表（Python/git/语言包/结论行），退出码 0。

    tmp_path 为干净 CWD（无 .env / .codeaudit.toml）；本机 Docker 状态未知，
    但 docker 缺失只记警告不记失败，退出码恒为 0。
    """
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    for token in ("环境诊断", "Python 版本", "git", "语言包 python", "语言包 java", "语言包 go", "语言包 cpp", ".env", "Docker 沙箱", "结论："):
        assert token in out, token


def test_doctor_returns_1_when_core_check_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """存在"失败"级条目（核心语言包缺失）→ 整体退出码 1，但不中断其余检查项。"""
    monkeypatch.chdir(tmp_path)

    real_import = __import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "tree_sitter_python":
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "_import_module", fake_import)
    rc = cli.main(["doctor"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "[失败] 语言包 python" in out
    assert "结论：" in out and "1 项失败" in out
    # 单项失败不中断整体：其余检查行仍在
    assert "语言包 javascript" in out and "Docker 沙箱" in out


# ---------------------------------------------------------------- 单项：Python / git


def test_check_python_ok_on_current_env() -> None:
    """当前解释器 >= 3.11 → OK，说明含版本号。"""
    status, name, detail, suggestion = cli._check_python()
    assert status == cli._DOCTOR_OK
    assert name == "Python 版本"
    assert detail.startswith("Python ")
    assert suggestion == ""


def test_check_python_fails_below_311(monkeypatch: pytest.MonkeyPatch) -> None:
    """版本低于 3.11（缺 tomllib）→ 失败 + 升级建议。"""
    import types

    fake_info = types.SimpleNamespace(major=3, minor=10, micro=0)
    monkeypatch.setattr(sys, "version_info", fake_info)
    status, _name, detail, suggestion = cli._check_python()
    assert status == cli._DOCTOR_FAIL
    assert "3.10" in detail
    assert "升级" in suggestion


def test_check_git_missing_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """git 不存在 → 警告（核心审计不受损）+ 安装建议，不崩溃。"""
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    status, _name, detail, suggestion = cli._check_git()
    assert status == cli._DOCTOR_WARN
    assert "未找到 git" in detail
    assert suggestion


# ---------------------------------------------------------------- 单项：语言包


def test_check_tree_sitter_java_missing_reported_not_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """java / go / cpp 语言包未安装 → 如实报"未安装"（警告级），python/js/ts 仍 OK。"""
    real_import = __import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name in {"tree_sitter_java", "tree_sitter_go", "tree_sitter_cpp"}:
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "_import_module", fake_import)
    rows = cli._check_tree_sitter()
    by_name = {name: (status, detail) for status, name, detail, _s in rows}
    assert by_name["语言包 java"][0] == cli._DOCTOR_WARN
    assert "未安装" in by_name["语言包 java"][1]
    assert by_name["语言包 go"][0] == cli._DOCTOR_WARN  # go 同为可选语言包（W26 卡 C）
    assert by_name["语言包 cpp"][0] == cli._DOCTOR_WARN  # cpp 同为可选语言包（W29 集成收口）
    for lang in ("python", "javascript", "typescript"):
        assert by_name[f"语言包 {lang}"][0] == cli._DOCTOR_OK


def test_check_tree_sitter_core_missing_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """核心语言包（python）导入失败 → 失败级 + pip install 建议；java/go 仍按可选记警告。"""

    def fake_import(name: str) -> object:
        raise ImportError(f"No module named {name!r}")

    monkeypatch.setattr(cli, "_import_module", fake_import)
    rows = {name: status for status, name, _d, _s in cli._check_tree_sitter()}
    assert rows["语言包 python"] == cli._DOCTOR_FAIL
    assert rows["语言包 javascript"] == cli._DOCTOR_FAIL
    assert rows["语言包 typescript"] == cli._DOCTOR_FAIL
    assert rows["语言包 java"] == cli._DOCTOR_WARN  # java 未装不报错（可选语言包）
    assert rows["语言包 go"] == cli._DOCTOR_WARN  # go 未装不报错（W26 卡 C，可选语言包）
    assert any("pip install tree_sitter_python" in s for _st, _n, _d, s in cli._check_tree_sitter())


# ---------------------------------------------------------------- 单项：.env / 配置文件


def test_check_dotenv_flags_non_whitelist_keys_without_leaking_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """.env 白名单外键 → 警告列出键名；键值（密钥）绝不回显。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "GLM_API_KEY=super-secret-value\nCODEAUDIT_DB_PATH=/tmp/x.db\nFOO_BAR=1\n",
        encoding="utf-8",
    )
    status, _name, detail, suggestion = cli._check_dotenv()
    assert status == cli._DOCTOR_WARN
    assert "FOO_BAR" in detail
    assert "super-secret-value" not in detail  # 值不落终端
    assert "GLM_API_KEY" in suggestion  # 修复建议列出白名单键


def test_check_dotenv_whitelisted_only_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """仅白名单键 → OK。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("GLM_API_KEY=k\nGLM_MODEL=m\n", encoding="utf-8")
    status, _name, detail, _s = cli._check_dotenv()
    assert status == cli._DOCTOR_OK
    assert "2 个白名单键" in detail


def test_check_dotenv_missing_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """无 .env → OK（跳过提示），不报错。"""
    monkeypatch.chdir(tmp_path)
    status, _name, detail, _s = cli._check_dotenv()
    assert status == cli._DOCTOR_OK
    assert "未找到" in detail


def test_check_config_file_broken_toml_warns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """配置文件 TOML 语法坏 → 警告 + 修复建议（与运行时"警告并继续"口径一致）。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".codeaudit.toml").write_text("this is [broken toml\n", encoding="utf-8")
    status, _name, detail, suggestion = cli._check_config_file()
    assert status == cli._DOCTOR_WARN
    assert "读取失败" in detail
    assert suggestion


def test_check_config_file_unknown_key_warns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """配置文件含未知键 → 警告点名该键。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".codeaudit.toml").write_text("no_such_key = 1\n", encoding="utf-8")
    status, _name, detail, _s = cli._check_config_file()
    assert status == cli._DOCTOR_WARN
    assert "no_such_key" in detail


def test_check_config_file_missing_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """无配置文件 → OK（提示可用 codeaudit init 生成）。"""
    monkeypatch.chdir(tmp_path)
    status, _name, detail, _s = cli._check_config_file()
    assert status == cli._DOCTOR_OK
    assert "未找到" in detail and "init" in detail


# ---------------------------------------------------------------- 单项：Docker


def test_check_docker_missing_warns_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """docker 不存在 → 报"不可用"（警告级）而非崩溃。"""
    monkeypatch.setattr(cli.shutil, "which", lambda name: "C:/bin/docker.exe" if name != "docker" else None)
    status, _name, detail, _s = cli._check_docker()
    assert status == cli._DOCTOR_WARN
    assert "不可用" in detail


def test_check_docker_daemon_down_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """docker CLI 存在但守护进程不可用 → 警告。"""
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "C:/bin/docker.exe")
    monkeypatch.setattr("audit.sandbox.executor.docker_available", lambda: False)
    status, _name, detail, _s = cli._check_docker()
    assert status == cli._DOCTOR_WARN
    assert "守护进程不可用" in detail


def test_check_docker_available_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """docker 探测通过 → OK。"""
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "C:/bin/docker.exe")
    monkeypatch.setattr("audit.sandbox.executor.docker_available", lambda: True)
    status, _name, detail, _s = cli._check_docker()
    assert status == cli._DOCTOR_OK
    assert "docker 可用" in detail
