"""沙箱执行器（T3）：子进程执行命令与白名单测试框架。

Windows 落地实现（asyncio 子进程 + 超时 kill；CREATE_NEW_PROCESS_GROUP 便于进程组
控制）。生产环境建议替换为 Docker 执行（--network none --memory 512m），对外接口
保持不变（docs/02 §6）。

安全语义：不执行来自 LLM 的任意 shell 命令，仅暴露 run()（受控调用方使用）与
run_tests()（白名单 pytest/jest/node-test）。可执行文件缺失以 exit_code=127 的
SandboxResult 返回而非抛异常；未知测试框架抛 SandboxError（属调用方编程错误）。

W7-A3：新增 node-test（node 18+ 内置 test runner，命令 [node, --test, target]，
零第三方依赖）；test_runner_available() 供 fix/testgen 阶段在执行前探测可用性
（探测失败走诚实降级：JS/TS 修复验证降级 syntax-ok、单测生成跳过该语言目标）。
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from audit.errors import SandboxError

__all__ = ["SandboxExecutor", "SandboxResult", "TEST_FRAMEWORKS", "test_runner_available"]

TEST_FRAMEWORKS = ("pytest", "jest", "node-test")


@dataclass
class SandboxResult:
    """一次沙箱执行的产物；exit_code 为 None 表示进程尚未退出（理论上不出现）。"""

    exit_code: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    timed_out: bool = False
    duration_sec: float = 0.0


def _decode(data: bytes) -> str:
    """子进程输出解码：utf-8 优先，GBK 容错，最终 replace。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _tail(text: str, max_lines: int) -> str:
    """只保留末尾 max_lines 行。"""
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[-max_lines:])


def test_runner_available(framework: str) -> bool:
    """探测测试运行器在本机是否可用（不实际执行进程，毫秒级）。

    - pytest：sys.executable 存在（shutil.which 恒真于正常解释器）且 pytest 可导入；
    - jest / node-test：shutil.which 探测可执行文件；
    - 未知框架返回 False（调用方按不可用做诚实降级）。
    """
    fw = str(framework or "").strip().lower()
    if fw == "pytest":
        if shutil.which(sys.executable) is None and not Path(sys.executable).is_file():
            return False
        try:
            import pytest  # noqa: F401 —— 仅探测可导入性
        except ImportError:
            return False
        return True
    if fw == "jest":
        return shutil.which("jest") is not None
    if fw == "node-test":
        return shutil.which("node") is not None
    return False


class SandboxExecutor:
    """asyncio 子进程沙箱。"""

    def __init__(self, default_timeout: float = 60.0, tail_lines: int = 80) -> None:
        self._default_timeout = max(0.1, float(default_timeout))
        self._tail_lines = max(1, int(tail_lines))

    async def run(
        self,
        command: list[str],
        cwd: Path,
        timeout_sec: float | None = None,
    ) -> SandboxResult:
        """执行一条命令（列表形式，不经 shell），返回退出码与末尾输出。"""
        if not command:
            raise SandboxError("command 不能为空")
        timeout = self._default_timeout if timeout_sec is None else max(0.1, float(timeout_sec))
        popen_kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **popen_kwargs,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            # 可执行文件缺失/不可执行：以 127（command not found）语义返回，不崩溃
            return SandboxResult(
                exit_code=127,
                stderr_tail=f"error: 无法启动命令 {command[0]!r}: {type(exc).__name__}: {exc}",
                duration_sec=0.0,
            )

        async def _drain(stream: Any) -> bytes:
            if stream is None:
                return b""
            with contextlib.suppress(Exception):
                return await stream.read()
            return b""

        out_task = asyncio.create_task(_drain(proc.stdout))
        err_task = asyncio.create_task(_drain(proc.stderr))
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            timed_out = True
            with contextlib.suppress(Exception):
                proc.kill()  # Windows 下为 TerminateProcess
            with contextlib.suppress(Exception):
                await proc.wait()
        stdout = await out_task
        stderr = await err_task
        duration = time.monotonic() - start
        return SandboxResult(
            exit_code=proc.returncode,
            stdout_tail=_tail(_decode(stdout), self._tail_lines),
            stderr_tail=_tail(_decode(stderr), self._tail_lines),
            timed_out=timed_out,
            duration_sec=round(duration, 3),
        )

    async def run_tests(self, framework: str, cwd: Path, target: str = "") -> SandboxResult:
        """在沙箱中运行白名单测试框架；未知框架抛 SandboxError。"""
        fw = str(framework or "").strip().lower()
        if fw not in TEST_FRAMEWORKS:
            raise SandboxError(
                f"不支持的测试框架: {framework!r}（白名单: pytest / jest / node-test）"
            )
        if fw == "pytest":
            command = [sys.executable, "-m", "pytest"]
            if target:
                command.append(target)
            command += ["-q", "--no-header"]
            return await self.run(command, cwd)
        if fw == "node-test":
            # node 18+ 内置 test runner：[node, --test, target]，零第三方依赖。
            # node 缺失时返回 127 结构化错误（诚实降级，不抛异常）。
            node = shutil.which("node")
            if not node:
                return SandboxResult(
                    exit_code=127,
                    stderr_tail="error: 未找到 node 可执行文件（未安装或不在 PATH 中），无法运行 node --test 测试",
                )
            command = [node, "--test"]
            if target:
                command.append(target)
            return await self.run(command, cwd)
        # jest：先探测可执行文件，缺失返回清晰 error 而非崩溃
        jest = shutil.which("jest")
        if not jest:
            return SandboxResult(
                exit_code=127,
                stderr_tail="error: 未找到 jest 可执行文件（未安装或不在 PATH 中），无法运行 jest 测试",
            )
        if sys.platform == "win32" and jest.lower().endswith((".cmd", ".bat")):
            command = ["cmd", "/c", jest]
        else:
            command = [jest]
        if target:
            command.append(target)
        return await self.run(command, cwd)
