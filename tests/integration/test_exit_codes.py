"""联调场景⑤（R2 必测场景 5）：退出码矩阵 0/1/2/3 参数化 + --json stdout 纯净性。

退出码契约（docs/09 §3 / cli.py 模块头）：
  0 完成/门禁通过 ｜ 1 运行错误 ｜ 2 参数错误（argparse）｜ 3 --check 门禁失败。

本文件全部走 in-process cli.main（真实流水线，无 LLM）；
argparse 的 SystemExit(2) 统一折算成返回码参与矩阵断言。

stdout 纯净性：``--json --check`` 时 stdout 必须可整体 json.loads（门禁消息只允许
出现在 stderr）。该契约由 R3-12 修复保障（A3 已合入），本文件按最终行为断言。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest

import cli

# 已知缺陷样本：可变默认参数（high）触发门禁
_HIGH_ISSUE_PY = (
    "def collect(items, bucket=[]):\n"
    "    bucket.extend(items)\n"
    "    return bucket\n"
)
# 无缺陷样本：纯函数，全部规则静默
_CLEAN_PY = "def add(a, b):\n    return a + b\n\n\nresult = add(1, 2)\n"


def _invoke(args: list[str]) -> int:
    """调用 cli.main 并把 argparse 的 SystemExit 折算为返回码（统一进矩阵）。"""
    try:
        return cli.main(args)
    except SystemExit as exc:  # argparse error / --help 用 SystemExit 传退出码
        code = exc.code
        return int(code) if code is not None else 0


@pytest.fixture
def project_factory(make_project) -> Callable[[str, str], Path]:
    def _make(content: str, name: str = "proj") -> Path:
        return make_project({"app.py": content}, name=name)

    return _make


# ---------------------------------------------------------------- 退出码矩阵


def test_exit_code_0_run_completes_without_gate(project_factory, tmp_path: Path) -> None:
    """0：审计完成且未启用门禁——即使发现问题也以 0 结束（上报与门禁分离）。"""
    proj = project_factory(_HIGH_ISSUE_PY, name="exit0")
    rc = _invoke(
        ["run", str(proj), "--no-llm", "--work-root", str(tmp_path / "w"), "--out", str(tmp_path / "o")]
    )
    assert rc == 0


def test_exit_code_1_missing_source_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """1：运行错误（源路径不存在）→ stderr 中文提示，退出码 1。"""
    rc = _invoke(
        [
            "run", str(tmp_path / "no_such_dir"),
            "--no-llm",
            "--work-root", str(tmp_path / "w"),
            "--out", str(tmp_path / "o"),
        ]
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "[错误]" in captured.err
    assert "源路径不存在" in captured.err


def test_exit_code_2_invalid_cli_argument(project_factory, tmp_path: Path) -> None:
    """2：CLI 参数非法（--format 取值不在 choices）→ argparse 以 2 退出。"""
    proj = project_factory(_CLEAN_PY, name="exit2")
    rc = _invoke(
        [
            "run", str(proj), "--no-llm", "--format", "pdf",
            "--work-root", str(tmp_path / "w"), "--out", str(tmp_path / "o"),
        ]
    )
    assert rc == 2


def test_exit_code_3_gate_failure(project_factory, tmp_path: Path) -> None:
    """3：--check 门禁失败（1 个 high >= 阈值 high）。"""
    proj = project_factory(_HIGH_ISSUE_PY, name="exit3")
    rc = _invoke(
        [
            "run", str(proj), "--no-llm",
            "--work-root", str(tmp_path / "w"), "--out", str(tmp_path / "o"),
            "--check", "--fail-on", "high",
        ]
    )
    assert rc == 3


def test_exit_code_0_gate_passes_on_clean_project(project_factory, tmp_path: Path) -> None:
    """0：门禁启用但无 >= 阈值的问题 → 0（矩阵闭环：门禁的两个方向）。"""
    proj = project_factory(_CLEAN_PY, name="exit0gate")
    rc = _invoke(
        [
            "run", str(proj), "--no-llm",
            "--work-root", str(tmp_path / "w"), "--out", str(tmp_path / "o"),
            "--check", "--fail-on", "high",
        ]
    )
    assert rc == 0


# ---------------------------------------------------------------- stdout 纯净性（R3-12 已修复后的契约）


def test_json_check_stdout_is_pure_json(project_factory, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--json --check：stdout 可整体 json.loads，门禁消息只出现在 stderr。

    本用例最初按 xfail(strict=True, ISSUE-R3-12) 标注；A3 已落地修复
    （门禁消息改走 stderr），按 xfail 协议移除标记转为常规契约用例。
    """
    proj = project_factory(_HIGH_ISSUE_PY, name="jsoncheck")
    rc = _invoke(
        [
            "run", str(proj), "--no-llm", "--json",
            "--work-root", str(tmp_path / "w"), "--out", str(tmp_path / "o"),
            "--check", "--fail-on", "high",
        ]
    )
    assert rc == 3
    captured = capsys.readouterr()
    payload = json.loads(captured.out)  # stdout 必须是纯 JSON（整体可解析）
    assert payload["summary"]["high"] >= 1
    assert "门禁" in captured.err  # 门禁消息归属 stderr
