"""W13-A2 单测：Docker 沙箱后端（全 mock docker CLI，零网络、不拉镜像）。

覆盖矩阵：
- docker 不可用（which 未命中 / `docker info` 非 0）→ 静默降级，backend="subprocess"，
  命令未被 docker 包装（真实子进程 echo 输出可断言）；
- docker 可用 → run() 实际执行的 argv 含 docker run 包装参数（--network none /
  --memory 512m / --cpus 1 / -v <cwd>:/work / -w /work / 镜像名），用注入的假 docker
  可执行脚本捕获 argv 写临时文件再断言（Windows 用 .bat、POSIX 用 .sh，已在本机
  实证 create_subprocess_exec 可直接执行 .bat 并透传 argv 与 exit code）；
- reset_docker_cache 生效（探测缓存命中后重探生效）；
- use_docker=False 强制 subprocess（即便 docker 可用）；
- docker run 自身失败（exit_code=125 类）→ 如实返回 backend="docker" + 退出码，不降级；
- 镜像可经 CODEAUDIT_DOCKER_IMAGE 覆盖；run_tests 同样走 Docker 包装；
- 本机无 Docker 的真实降级路径实测（有 Docker 的环境自动跳过）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import audit.sandbox.executor as mod
from audit.sandbox.executor import SandboxExecutor

PY = sys.executable


class _FakeCompleted:
    """`docker info` 探针（subprocess.run）的假返回值，只需 returncode。"""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


@pytest.fixture(autouse=True)
def _fresh_docker_probe_cache():
    """每条用例前后清空进程级探测缓存，保证用例互不串扰。"""
    mod.reset_docker_cache()
    yield
    mod.reset_docker_cache()


def _mock_docker_present(monkeypatch: pytest.MonkeyPatch, info_ok: bool = True) -> None:
    """让探测认为 docker 存在且 `docker info` 按需成功/失败（不触真实 CLI）。"""
    monkeypatch.setattr(
        mod.shutil, "which", lambda name: f"C:/fake-tools/{name}" if name == "docker" else None
    )
    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: _FakeCompleted(0 if info_ok else 1))


def _make_fake_docker(tmp_path: Path, capture: Path, exit_code: int = 0) -> str:
    """注入的假 docker 可执行脚本：把 argv 原样（含包装参数）**追加**写入 capture 后按 exit_code 退出。

    W14-A2（M-2b）起 run() 在结束时会追加一次 `docker rm -f <name>` 兜底清理调用，
    故捕获改为追加模式（>>），既有断言均为子串/哨兵检查，语义不受影响。
    注意 [..] 哨兵：避免 %* 以数字结尾时 cmd 把 "1>" 误解析为句柄重定向。
    """
    if sys.platform == "win32":
        script = tmp_path / "fake_docker.bat"
        script.write_text(
            f'@echo off\r\necho [%*] >> "{capture}"\r\nexit /b {exit_code}\r\n',
            encoding="ascii",
        )
    else:
        script = tmp_path / "fake_docker.sh"
        script.write_text(
            f"#!/bin/sh\nprintf '[%s]\\n' \"$*\" >> '{capture}'\nexit {exit_code}\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
    return str(script)


def _mock_docker_present_with_fake_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capture: Path, exit_code: int = 0
) -> None:
    """探测通过，且 which("docker") 指向假 CLI 脚本（真实执行以捕获 run() 的 argv）。"""
    fake = _make_fake_docker(tmp_path, capture, exit_code=exit_code)
    monkeypatch.setattr(mod.shutil, "which", lambda name: fake if name == "docker" else None)
    monkeypatch.setattr(mod.subprocess, "run", lambda *args, **kwargs: _FakeCompleted(0))


# ---------------------------------------------------------------------------
# 降级路径：docker 不可用 → 静默走 subprocess，backend 诚实标注
# ---------------------------------------------------------------------------


async def test_docker_unavailable_degrades_to_subprocess(tmp_path: Path, monkeypatch):
    """which 未命中（本机无 docker）→ 降级 subprocess，命令未被 docker 包装。"""
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    res = await SandboxExecutor().run([PY, "-c", "print('hello-subprocess')"], cwd=tmp_path)
    assert res.backend == "subprocess"
    assert res.exit_code == 0
    assert res.timed_out is False
    # 命令被原样直接执行（若被 docker 包装，假路径根本跑不出该输出）
    assert "hello-subprocess" in res.stdout_tail


async def test_docker_info_failure_degrades_to_subprocess(tmp_path: Path, monkeypatch):
    """CLI 存在但 `docker info` 非 0 退出（守护进程未启动类）→ 同样降级。"""
    _mock_docker_present(monkeypatch, info_ok=False)
    res = await SandboxExecutor().run([PY, "-c", "print('degraded')"], cwd=tmp_path)
    assert res.backend == "subprocess"
    assert res.exit_code == 0
    assert "degraded" in res.stdout_tail


@pytest.mark.skipif(mod.docker_available(), reason="本机存在可用 Docker，无 Docker 降级实测仅适用于无 Docker 环境")
async def test_real_host_without_docker_degrades(tmp_path: Path):
    """无 mock 真实降级路径实测：本机（无 Docker）默认参数下走 subprocess。"""
    res = await SandboxExecutor().run([PY, "-c", "print('real-degrade')"], cwd=tmp_path)
    assert mod.docker_available() is False
    assert res.backend == "subprocess"
    assert "real-degrade" in res.stdout_tail


# ---------------------------------------------------------------------------
# Docker 路径：探测通过 → argv 被 docker run 包装（假 CLI 捕获落盘断言）
# ---------------------------------------------------------------------------


async def test_docker_available_wraps_run_argv(tmp_path: Path, monkeypatch):
    """docker 可用时 run() 的实际 argv 含全部包装参数（network/memory/cpus/卷/工作目录/镜像）。"""
    capture = tmp_path / "docker_argv.txt"
    _mock_docker_present_with_fake_cli(tmp_path, monkeypatch, capture)
    workdir = tmp_path / "work"
    workdir.mkdir()
    res = await SandboxExecutor(use_docker=True).run(["inner-cmd", "inner-arg"], cwd=workdir)
    assert res.backend == "docker"
    assert res.exit_code == 0
    assert res.timed_out is False
    raw = capture.read_text(encoding="utf-8", errors="replace")
    assert raw.startswith("[") and raw.rstrip().endswith("]")  # 哨兵完整，argv 未被截断
    assert "--network none" in raw
    assert "--memory 512m" in raw
    assert "--cpus 1" in raw
    assert f"{Path(workdir).resolve()}:/work" in raw
    assert "-w /work" in raw
    assert "python:3.12-slim" in raw  # 默认镜像
    assert "--rm" in raw
    assert "inner-cmd inner-arg" in raw  # 原命令原样追加在镜像之后


async def test_docker_image_env_override(tmp_path: Path, monkeypatch):
    """CODEAUDIT_DOCKER_IMAGE 覆盖默认镜像（不改 config.py，直接读环境变量）。"""
    capture = tmp_path / "docker_argv.txt"
    _mock_docker_present_with_fake_cli(tmp_path, monkeypatch, capture)
    monkeypatch.setenv("CODEAUDIT_DOCKER_IMAGE", "registry.example/codeaudit:latest")
    res = await SandboxExecutor(use_docker=True).run(["echo", "hi"], cwd=tmp_path)
    assert res.backend == "docker"
    raw = capture.read_text(encoding="utf-8", errors="replace")
    assert "registry.example/codeaudit:latest" in raw
    assert "python:3.12-slim" not in raw


async def test_docker_run_failure_returned_honestly(tmp_path: Path, monkeypatch):
    """docker run 自身失败（如镜像拉取失败 exit_code=125）→ 如实返回，不重试不降级。"""
    capture = tmp_path / "docker_argv.txt"
    _mock_docker_present_with_fake_cli(tmp_path, monkeypatch, capture, exit_code=125)
    res = await SandboxExecutor(use_docker=True).run(["echo", "hi"], cwd=tmp_path)
    assert res.backend == "docker"
    assert res.exit_code == 125
    assert res.timed_out is False


async def test_run_tests_goes_through_docker_wrap(tmp_path: Path, monkeypatch):
    """run_tests 与 run 共用 Docker 包装：pytest 命令原样透传进容器。"""
    capture = tmp_path / "docker_argv.txt"
    _mock_docker_present_with_fake_cli(tmp_path, monkeypatch, capture)
    res = await SandboxExecutor(use_docker=True).run_tests("pytest", tmp_path, target="test_x.py")
    assert res.backend == "docker"
    assert res.exit_code == 0
    raw = capture.read_text(encoding="utf-8", errors="replace")
    assert "-w /work" in raw
    assert "pytest" in raw and "--no-header" in raw and "test_x.py" in raw


# ---------------------------------------------------------------------------
# 开关与缓存：use_docker=False 强制 subprocess；reset_docker_cache 生效
# ---------------------------------------------------------------------------


async def test_default_off_even_when_docker_available(tmp_path: Path, monkeypatch):
    """W13 收口裁决回归：默认参数即便 docker 可用也必须走 subprocess。

    CI 实证：GitHub Actions ubuntu runner 预装 Docker，自动启用会把宿主
    sys.executable 包装进容器导致全部 127。Docker 后端必须显式 opt-in。
    """
    _mock_docker_present_with_fake_cli(tmp_path, monkeypatch, tmp_path / "unused.txt")
    res = await SandboxExecutor().run([sys.executable, "-c", "print('default-off')"], cwd=tmp_path)
    assert res.backend == "subprocess"
    assert res.exit_code == 0
    assert "default-off" in res.stdout_tail


async def test_use_docker_false_forces_subprocess(tmp_path: Path, monkeypatch):
    """use_docker=False 即便 docker 可用也强制走 subprocess（意愿优先于探测）。"""
    _mock_docker_present(monkeypatch, info_ok=True)
    assert mod.docker_available() is True
    res = await SandboxExecutor(use_docker=False).run([PY, "-c", "print('forced-subprocess')"], cwd=tmp_path)
    assert res.backend == "subprocess"
    assert "forced-subprocess" in res.stdout_tail


def test_reset_docker_cache_reprobes(monkeypatch):
    """探测结果进程内缓存；reset_docker_cache() 后按最新环境重探。"""
    _mock_docker_present(monkeypatch, info_ok=True)
    assert mod.docker_available() is True
    # 环境变差：缓存仍命中旧结果 True（不再重探——进程内只探一次的语义）
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    assert mod.docker_available() is True
    # 重置缓存后按新环境重探 → False
    mod.reset_docker_cache()
    assert mod.docker_available() is False


def test_sandbox_result_backend_default_is_subprocess():
    """backend 默认值 "subprocess"，既有调用方（不读该字段）行为完全向后兼容。"""
    res = mod.SandboxResult()
    assert res.backend == "subprocess"
    assert res.exit_code is None and res.truncated is False and res.timed_out is False
