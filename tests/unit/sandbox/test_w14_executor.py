"""W14-A2 单测：沙箱后端开关接线（M-2a）、容器兜底清理（M-2b）、排空兜底超时（M-3）。

覆盖矩阵：
- M-2a：backend 参数归一化（大小写/空白容忍）、与旧式 use_docker 的兼容与优先级、
  非法值诚实降级 subprocess 并记录 backend_note、端到端真实降级可执行；
- M-2b：docker 路径 argv 带 --name codeaudit-<短uuid>；正常结束与超时击杀后均追加
  一次 docker rm -f <同名容器>（假 CLI 落盘捕获断言，复用 test_docker_backend 的
  mock 设施——W14 起其捕获为追加模式）；
- M-3：_collect_drain 兜底超时返回已读部分并 cancel 排空任务（协程级）；真 Windows
  端到端——孙进程继承管道写端时 run() 不再永久挂起，结果按 [output truncated]
  如实标注（本机 Windows 实测）。

docker CLI 全 mock / 或假脚本，零网络、不拉镜像、不触真实 Docker。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

import pytest

import audit.sandbox.executor as mod
from audit.sandbox.executor import SandboxExecutor

from test_docker_backend import _mock_docker_present_with_fake_cli

PY = sys.executable


@pytest.fixture(autouse=True)
def _fresh_docker_probe_cache():
    """每条用例前后清空进程级探测缓存（与 test_docker_backend 同约定）。"""
    mod.reset_docker_cache()
    yield
    mod.reset_docker_cache()


def _make_slow_fake_docker(tmp_path: Path, capture: Path, delay_sec: int = 2) -> str:
    """追加捕获 argv 的假 docker CLI：run 分支额外睡眠 delay_sec（供超时路径用）。"""
    if sys.platform == "win32":
        script = tmp_path / "fake_docker_slow.bat"
        script.write_text(
            "@echo off\r\n"
            f'echo [%*] >> "{capture}"\r\n'
            f'if "%~1"=="run" ping -n {delay_sec + 1} 127.0.0.1 >nul\r\n'
            "exit /b 0\r\n",
            encoding="ascii",
        )
    else:
        script = tmp_path / "fake_docker_slow.sh"
        script.write_text(
            "#!/bin/sh\n"
            f"printf '[%s]\\n' \"$*\" >> '{capture}'\n"
            f'if [ "$1" = "run" ]; then sleep {delay_sec}; fi\n'
            "exit 0\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
    return str(script)


# ---------------------------------------------------------------------------
# M-2a：backend 参数（归一化 / 兼容 / 非法降级）
# ---------------------------------------------------------------------------


def test_backend_defaults_subprocess():
    """缺省（无 backend 无 use_docker）→ subprocess 意愿，无降级备注。"""
    ex = SandboxExecutor()
    assert ex.backend_requested == "subprocess"
    assert ex._use_docker is False
    assert ex.backend_note == ""


def test_backend_none_falls_back_to_use_docker():
    """backend=None 时回退旧式 use_docker 入口（既有调用方行为完全不变）。"""
    assert SandboxExecutor(use_docker=True).backend_requested == "docker"
    assert SandboxExecutor(use_docker=False).backend_requested == "subprocess"


def test_backend_normalizes_case_and_whitespace():
    """backend 大小写与首尾空白容忍（配置文件里写 "Docker" 也该生效）。"""
    assert SandboxExecutor(backend="docker").backend_requested == "docker"
    assert SandboxExecutor(backend="  Docker ").backend_requested == "docker"
    assert SandboxExecutor(backend="SUBPROCESS").backend_requested == "subprocess"


def test_backend_takes_precedence_over_use_docker():
    """两入口同时给出且冲突时，显式 backend 权威。"""
    ex = SandboxExecutor(use_docker=True, backend="subprocess")
    assert ex.backend_requested == "subprocess"
    assert ex._use_docker is False


def test_backend_invalid_degrades_with_note():
    """非法值诚实降级 subprocess，降级事实写入 backend_note。"""
    ex = SandboxExecutor(backend="podman")
    assert ex.backend_requested == "subprocess"
    assert ex._use_docker is False
    assert ex.backend_note
    assert "podman" in ex.backend_note
    assert "subprocess" in ex.backend_note


async def test_backend_invalid_end_to_end_runs_subprocess(tmp_path: Path):
    """非法后端端到端：真实以 subprocess 执行命令，结果 backend 如实标注。"""
    ex = SandboxExecutor(backend="kubernetes")
    res = await ex.run([PY, "-c", "print('degraded-fine')"], cwd=tmp_path)
    assert res.backend == "subprocess"
    assert res.exit_code == 0
    assert "degraded-fine" in res.stdout_tail


async def test_backend_docker_end_to_end_uses_container_wrap(tmp_path: Path, monkeypatch):
    """backend="docker" 与 use_docker=True 等价：探测通过即容器化包装执行。"""
    capture = tmp_path / "argv.txt"
    _mock_docker_present_with_fake_cli(tmp_path, monkeypatch, capture)
    res = await SandboxExecutor(backend="docker").run(["echo", "hi"], cwd=tmp_path)
    assert res.backend == "docker"
    raw = capture.read_text(encoding="utf-8", errors="replace")
    assert "run --rm" in raw


# ---------------------------------------------------------------------------
# M-2b：--name 注入与 docker rm -f 兜底清理（正常路径 + 超时路径）
# ---------------------------------------------------------------------------


async def test_docker_run_adds_container_name_and_cleans_up(tmp_path: Path, monkeypatch):
    """正常路径：argv 带 --name codeaudit-<短uuid>；run() 结束前 docker rm -f 同名容器。"""
    capture = tmp_path / "argv.txt"
    _mock_docker_present_with_fake_cli(tmp_path, monkeypatch, capture)
    res = await SandboxExecutor(backend="docker").run(["echo", "hi"], cwd=tmp_path)
    assert res.backend == "docker"
    assert res.timed_out is False
    lines = [ln for ln in capture.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    assert len(lines) == 2, f"应恰好两次 docker CLI 调用（run + rm -f），实际：{lines}"
    assert lines[0].startswith("[run ")
    assert "--name codeaudit-" in lines[0]
    # rm -f 的容器名与 run 的 --name 一致（同一次 run 会话）
    container_name = lines[0].split("--name ")[1].split("]")[0].split()[0]
    assert container_name.startswith("codeaudit-")
    assert len(container_name) <= len("codeaudit-") + 12  # 短 uuid
    assert lines[1].strip() == f"[rm -f {container_name}]"


async def test_docker_timeout_removes_container(tmp_path: Path, monkeypatch):
    """超时路径（M-2b 核心）：击杀 docker CLI 进程后仍补 docker rm -f，run() 必然返回。"""
    capture = tmp_path / "argv.txt"
    fake = _make_slow_fake_docker(tmp_path, capture, delay_sec=4)
    monkeypatch.setattr(mod.shutil, "which", lambda name: fake if name == "docker" else None)
    # 探针 subprocess.run 用假返回（不触真实 CLI）
    monkeypatch.setattr(
        mod.subprocess, "run", lambda *args, **kwargs: type("R", (), {"returncode": 0})()
    )
    workdir = tmp_path / "work"
    workdir.mkdir()
    res = await SandboxExecutor(backend="docker").run(["sleep-cmd"], cwd=workdir, timeout_sec=1)
    assert res.backend == "docker"
    assert res.timed_out is True
    assert res.duration_sec < 10  # 不等假 CLI 睡满 4 秒（1 秒即击杀 + 清理）
    lines = [ln for ln in capture.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    assert lines, "run 调用的 argv 应先于挂起被捕获落盘"
    assert lines[0].startswith("[run ") and "--name codeaudit-" in lines[0]
    container_name = lines[0].split("--name ")[1].split("]")[0].split()[0]
    assert any(ln.strip() == f"[rm -f {container_name}]" for ln in lines[1:]), f"缺少同名容器清理调用：{lines}"


async def test_subprocess_path_skips_container_cleanup(tmp_path: Path, monkeypatch):
    """subprocess 路径无容器名：不产生任何 docker rm -f 调用（即便 docker 可用）。"""
    capture = tmp_path / "argv.txt"
    _mock_docker_present_with_fake_cli(tmp_path, monkeypatch, capture)
    res = await SandboxExecutor(backend="subprocess").run([PY, "-c", "print('native')"], cwd=tmp_path)
    assert res.backend == "subprocess"
    assert "native" in res.stdout_tail
    assert not capture.exists() or "rm -f" not in capture.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# M-3：排空兜底超时
# ---------------------------------------------------------------------------


async def test_collect_drain_fallback_returns_partial_and_cancels():
    """排空任务永不完成 → 兜底超时返回已读部分、truncated 置真、run() 不等收割。

    W14-A2 实测教训（Windows IOCP）：已提交的管道 ReadFile 无法取消，等待"取消
    完成"（wait_for 语义）会在孙进程持写端时二次挂死——故 _collect_drain 只请求
    取消（不收割），立即用已读部分返回。
    """
    started = asyncio.Event()

    async def never_ends() -> None:
        started.set()
        await asyncio.Event().wait()  # 永不 set：模拟孙进程持管道写端的 EOF 永等

    task = asyncio.create_task(never_ends())
    await started.wait()
    sink = {"chunks": [b"par", b"tial"], "total": 7, "discarded": False}
    data, truncated = await mod._collect_drain(task, sink, timeout=0.2)
    assert data == b"partial"  # 已读到部分不丢
    assert truncated is True  # 如实标注截断（[output truncated] 机制消费该标志）
    # 收尾清理：Event.wait 可取消（真实场景由 _close_pipes 促成 pending recv abort）
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_collect_drain_normal_path_returns_discard_flag():
    """正常完成路径：返回完整数据与限量丢弃标志（与修复前语义一致）。"""
    sink = {"chunks": [b"abc"], "total": 3, "discarded": True}

    async def immediate() -> None:
        return None

    task = asyncio.create_task(immediate())
    await task
    data, truncated = await mod._collect_drain(task, sink, timeout=5)
    assert data == b"abc"
    assert truncated is True  # discarded 限量语义保留


async def test_grandchild_pipe_holder_does_not_hang_run(tmp_path: Path):
    """Windows 端到端（M-3 核心回归）：孙进程继承 stdout 写端时长眠，run() 必然返回。

    修复前：父进程被超时击杀后 run() 挂到孙进程退出（20s+）——本机 Windows/
    Py3.13 实测 duration ≈ 21s：asyncio 的 proc.wait() 要等全部管道 transport
    关闭（_try_finish），孙进程持写端时 EOF 永不到来，排空与回收等待双双挂死，
    server 任务会卡死在 running。修复后：击杀即关读端 transport（促 pending
    IOCP recv abort），duration ≈ timeout + 数百毫秒。
    """
    executor = SandboxExecutor()
    # W15 集成修复（偶发 flaky）：原写法仅在 t=0 打印一次——机器负载高时击杀前排空
    # 任务可能尚未读到该块，stdout_tail 断言偶发失败（实测约 1/6）。改为在超时窗口
    # 内持续输出（50×0.02s≈1s 覆盖整个 timeout_sec=1），回归意图不变：孙进程仍持
    # 写端、run() 仍须快速返回、已读部分仍不丢。
    code = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])\n"
        "for _ in range(50):\n"
        "    print('parent-done', flush=True)\n"
        "    time.sleep(0.02)\n"
        "time.sleep(30)\n"
    )
    res = await executor.run([PY, "-c", code], cwd=tmp_path, timeout_sec=1)
    assert res.timed_out is True
    assert "parent-done" in res.stdout_tail  # 已读到部分不丢
    assert res.duration_sec < 10  # 修复前 ≈21s（挂到孙进程退出）；修复后 ≈ timeout + ε
