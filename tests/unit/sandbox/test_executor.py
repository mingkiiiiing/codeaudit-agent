"""T3 单测：SandboxExecutor（真实本地子进程，无网络）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from audit.sandbox.executor import SandboxExecutor
from audit.errors import SandboxError

PY = sys.executable


async def test_run_captures_stdout_and_exit_code(tmp_path: Path):
    executor = SandboxExecutor()
    res = await executor.run([PY, "-c", "print('ok')"], cwd=tmp_path)
    assert res.exit_code == 0
    assert res.timed_out is False
    assert res.stdout_tail.strip() == "ok"
    assert res.duration_sec > 0
    assert res.truncated is False  # W10 F4：小输出不误标


async def test_run_captures_stderr(tmp_path: Path):
    executor = SandboxExecutor()
    res = await executor.run([PY, "-c", "import sys; print('boom', file=sys.stderr); raise SystemExit(3)"], cwd=tmp_path)
    assert res.exit_code == 3
    assert "boom" in res.stderr_tail


async def test_timeout_kills_process(tmp_path: Path):
    executor = SandboxExecutor()
    res = await executor.run(
        [PY, "-c", "import time; time.sleep(5)"], cwd=tmp_path, timeout_sec=1
    )
    assert res.timed_out is True
    assert res.duration_sec < 5  # 进程被提前 kill


async def test_stdout_tail_keeps_last_80_lines(tmp_path: Path):
    executor = SandboxExecutor()
    code = "import sys\nfor i in range(100):\n    print(f'line{i}')"
    res = await executor.run([PY, "-c", code], cwd=tmp_path)
    tail_lines = res.stdout_tail.strip().splitlines()
    assert len(tail_lines) == 80
    assert tail_lines[-1] == "line99"
    assert "line10" not in res.stdout_tail  # 前 20 行被裁掉


async def test_run_tests_pytest_pass_and_fail(tmp_path: Path):
    (tmp_path / "test_pass.py").write_text("def test_ok():\n    assert 1 == 1\n", encoding="utf-8")
    (tmp_path / "test_fail.py").write_text("def test_bad():\n    assert 1 == 2\n", encoding="utf-8")
    executor = SandboxExecutor(default_timeout=60)

    ok = await executor.run_tests("pytest", tmp_path, target="test_pass.py")
    assert ok.exit_code == 0
    assert "passed" in ok.stdout_tail + ok.stderr_tail

    bad = await executor.run_tests("pytest", tmp_path, target="test_fail.py")
    assert bad.exit_code == 1
    combined = bad.stdout_tail + bad.stderr_tail
    assert "failed" in combined


async def test_run_tests_whitelist(tmp_path: Path):
    executor = SandboxExecutor()
    with pytest.raises(SandboxError):
        await executor.run_tests("make", tmp_path)
    with pytest.raises(SandboxError):
        await executor.run_tests("", tmp_path)


async def test_run_tests_jest_missing_returns_clear_error(tmp_path: Path, monkeypatch):
    import audit.sandbox.executor as mod

    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    executor = SandboxExecutor()
    res = await executor.run_tests("jest", tmp_path)
    assert res.exit_code == 127
    assert "jest" in res.stderr_tail and "未找到" in res.stderr_tail


async def test_missing_binary_returns_127_not_crash(tmp_path: Path):
    executor = SandboxExecutor()
    res = await executor.run(["definitely-not-exist-xyz-987"], cwd=tmp_path)
    assert res.exit_code == 127
    assert "error" in res.stderr_tail


async def test_empty_command_raises(tmp_path: Path):
    executor = SandboxExecutor()
    with pytest.raises(SandboxError):
        await executor.run([], cwd=tmp_path)


async def test_large_stdout_truncated_but_process_exits_normally(tmp_path: Path):
    """W10 F4：约 50MB stdout 触发限量；排空语义下进程正常退出（不被 PIPE 写阻塞误杀）。"""
    executor = SandboxExecutor()
    code = "for _ in range(500000):\n    print('A' * 100)"  # 500000 * 101B ≈ 50MB > 8MB 限量
    res = await executor.run([PY, "-c", code], cwd=tmp_path, timeout_sec=60)
    assert res.exit_code == 0  # 核心：继续读但丢弃，子进程不被 PIPE 阻塞到超时击杀
    assert res.timed_out is False
    assert res.truncated is True
    assert res.stdout_tail.startswith("[output truncated]")
    tail_lines = res.stdout_tail.splitlines()
    assert len(tail_lines) <= 81  # 标记行 + 最多 80 行 tail
    assert set(tail_lines[-1]) <= {"A"}  # 末行仍是真实输出内容


async def test_large_stderr_truncated(tmp_path: Path):
    """W10 F4：约 20MB stderr 同样限量排空并标记；未超限的 stdout 不受影响。"""
    executor = SandboxExecutor()
    code = "import sys\nfor _ in range(200000):\n    sys.stderr.write('B' * 100 + '\\n')"  # ≈ 20MB
    res = await executor.run([PY, "-c", code], cwd=tmp_path, timeout_sec=60)
    assert res.exit_code == 0
    assert res.timed_out is False
    assert res.truncated is True
    assert res.stderr_tail.startswith("[output truncated]")
    assert len(res.stderr_tail.splitlines()) <= 81
    assert "[output truncated]" not in res.stdout_tail  # stdout 未超限，不误标
