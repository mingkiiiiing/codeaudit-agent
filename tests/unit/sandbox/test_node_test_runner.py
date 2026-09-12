"""W7-A3 单测：node-test 白名单与可用性探测（真实 node 子进程，毫秒级本地进程、禁网）。

- node 存在时真跑一个最小 .test.mjs（通过 / 失败两种路径）；
- node 不存在路径用 monkeypatch shutil.which 模拟 → 127 结构化错误（不抛异常）；
- test_runner_available 的 pytest / node-test / jest / 未知框架语义。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import audit.sandbox.executor as executor_mod
from audit.errors import SandboxError
from audit.sandbox import SandboxExecutor
from audit.sandbox.executor import TEST_FRAMEWORKS

# 注意：不用 from-import 引入 test_runner_available——test_* 命名会被 pytest 误收集。

_requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="本机无 node 可执行文件"
)

GOOD_MJS = """import { test } from 'node:test';
import assert from 'node:assert';

test('ok', () => {
    assert.strictEqual(1 + 1, 2);
});
"""

BAD_MJS = GOOD_MJS.replace("2);", "3);")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------- 白名单


def test_node_test_in_whitelist():
    assert "node-test" in TEST_FRAMEWORKS
    assert "pytest" in TEST_FRAMEWORKS and "jest" in TEST_FRAMEWORKS


# ---------------------------------------------------------------- 真跑 node --test


@_requires_node
async def test_run_tests_node_test_passes(tmp_path: Path):
    _write(tmp_path / "good.test.mjs", GOOD_MJS)
    executor = SandboxExecutor(default_timeout=60)
    result = await executor.run_tests("node-test", tmp_path, target="good.test.mjs")
    assert result.exit_code == 0
    assert not result.timed_out
    combined = result.stdout_tail + result.stderr_tail
    assert "pass 1" in combined


@_requires_node
async def test_run_tests_node_test_failure_reported(tmp_path: Path):
    _write(tmp_path / "bad.test.mjs", BAD_MJS)
    executor = SandboxExecutor(default_timeout=60)
    result = await executor.run_tests("node-test", tmp_path, target="bad.test.mjs")
    assert result.exit_code != 0
    combined = result.stdout_tail + result.stderr_tail
    assert "fail 1" in combined


@_requires_node
async def test_run_tests_node_test_discovers_by_convention(tmp_path: Path):
    """无 target 时 node --test 按 *.test.* 约定发现（与现有测试探测路径一致）。"""
    _write(tmp_path / "tests" / "deep.test.mjs", GOOD_MJS)
    executor = SandboxExecutor(default_timeout=60)
    result = await executor.run_tests("node-test", tmp_path)
    assert result.exit_code == 0
    assert "pass 1" in result.stdout_tail + result.stderr_tail


async def test_run_tests_node_test_missing_returns_127(tmp_path: Path, monkeypatch):
    """node 缺失：127 结构化错误（诚实降级），不抛异常。"""
    monkeypatch.setattr(executor_mod.shutil, "which", lambda name: None)
    executor = SandboxExecutor()
    result = await executor.run_tests("node-test", tmp_path)
    assert result.exit_code == 127
    assert "node" in result.stderr_tail and "未找到" in result.stderr_tail


async def test_run_tests_unknown_framework_still_raises(tmp_path: Path):
    executor = SandboxExecutor()
    with pytest.raises(SandboxError):
        await executor.run_tests("node", tmp_path)  # 白名单名是 node-test，不是 node
    with pytest.raises(SandboxError):
        await executor.run_tests("", tmp_path)


# ---------------------------------------------------------------- 可用性探测


def test_test_runner_available_pytest():
    assert executor_mod.test_runner_available("pytest") is True


def test_test_runner_available_node_test_matches_which(monkeypatch):
    expected = shutil.which("node") is not None
    assert executor_mod.test_runner_available("node-test") is expected
    monkeypatch.setattr(executor_mod.shutil, "which", lambda name: None)
    assert executor_mod.test_runner_available("node-test") is False
    assert executor_mod.test_runner_available("jest") is False


def test_test_runner_available_unknown_framework():
    assert executor_mod.test_runner_available("mocha") is False
    assert executor_mod.test_runner_available("") is False
