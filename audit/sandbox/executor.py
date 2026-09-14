"""沙箱执行器（T3）：子进程执行命令与白名单测试框架。

Windows 落地实现（asyncio 子进程 + 超时 kill；CREATE_NEW_PROCESS_GROUP 便于进程组
控制）。docs/02 §6 的"生产建议 Docker（--network none --memory 512m）"已由 W13-A2
落地为可选 Docker 后端（探测可用则容器化执行，不可用自动降级本路径，见下）。

安全语义：不执行来自 LLM 的任意 shell 命令，仅暴露 run()（受控调用方使用）与
run_tests()（白名单 pytest/jest/node-test）。可执行文件缺失以 exit_code=127 的
SandboxResult 返回而非抛异常；未知测试框架抛 SandboxError（属调用方编程错误）。

W7-A3：新增 node-test（node 18+ 内置 test runner，命令 [node, --test, target]，
零第三方依赖）；test_runner_available() 供 fix/testgen 阶段在执行前探测可用性
（探测失败走诚实降级：JS/TS 修复验证降级 syntax-ok、单测生成跳过该语言目标）。

W10-A2（F4 沙箱输出限量）：_drain 改为限量排空——累计读取达 _OUTPUT_LIMIT_BYTES
（8MB）后继续读但丢弃（防止子进程写满 PIPE 永久阻塞），内存上限 ≈ LIMIT + 一个
chunk；被限量的流在 tail 首行带 [output truncated] 标记行，SandboxResult.truncated
置 True（诚实降级可见）。

W13-A2（Docker 沙箱后端）：构造参数 use_docker=True 表达意愿（不改 AuditConfig）；
实际是否走 Docker = use_docker AND docker_available()（探测：shutil.which("docker")
命中且 `docker info` 0 退出、5s 超时；结果进程内缓存只探一次，reset_docker_cache()
供测试重探）。Docker 路径把命令包装为
`docker run --rm --network none --memory 512m --cpus 1 -v <cwd>:/work -w /work <image> <cmd>`
（镜像默认 python:3.12-slim，环境变量 CODEAUDIT_DOCKER_IMAGE 可覆盖），执行复用既有
asyncio 子进程机制——docker CLI 本身就是子进程，超时（wait_for kill docker CLI）、
限量排空、truncated 标记语义不变。SandboxResult.backend 诚实标注实际后端
（"docker" | "subprocess"）：use_docker=True 但探测失败 → 静默降级 subprocess
（不告警，调用方读 backend 可知）；docker run 自身失败（如镜像拉取失败 exit_code=125
类）→ 如实返回退出码与 stderr，不重试不降级（docker 命令的失败就是失败）。
test_runner_available() 语义保持宿主探测不变（v1 边界）：容器内 pytest 可用性由
镜像自带保证（pytest 命令仍为宿主 [sys.executable, -m pytest] 原样透传）。
docs/02 §6 "生产建议 Docker（--network none --memory 512m）"由此兑现。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from audit.errors import SandboxError

__all__ = [
    "SandboxExecutor",
    "SandboxResult",
    "TEST_FRAMEWORKS",
    "test_runner_available",
    "docker_available",
    "reset_docker_cache",
]

TEST_FRAMEWORKS = ("pytest", "jest", "node-test")

# W10 F4：单流输出限量。累计读取达该字节数后继续读但丢弃（排空语义），
# 防止被审项目海量输出（如 200MB 测试日志）把服务端瞬时内存打到同量级。
_OUTPUT_LIMIT_BYTES = 8 * 1024 * 1024
# 限量排空的读取粒度（64KB）：内存占用上限 ≈ LIMIT + 一个 chunk。
_OUTPUT_CHUNK_BYTES = 64 * 1024
# 限量标记行：出现在被限量流的 tail 首行，使诚实降级对调用方可见。
_TRUNCATED_MARKER = "[output truncated]"

# ---------------------------------------------------------------------------
# W13-A2：Docker 沙箱后端（探测 / 缓存 / 包装）。
# ---------------------------------------------------------------------------
# 默认镜像：官方 python:3.12-slim（自带 pytest 所需解释器；拉取由使用者离线预置）。
_DEFAULT_DOCKER_IMAGE = "python:3.12-slim"
# 镜像覆盖环境变量：直接读 os.environ，不改 AuditConfig（W13-A2 约束）。
_DOCKER_IMAGE_ENV = "CODEAUDIT_DOCKER_IMAGE"
# `docker info` 探针超时：守护进程无响应时快速判定不可用（走降级路径）。
_DOCKER_PROBE_TIMEOUT_SEC = 5.0

# 探测缓存：None=进程内尚未探测；True/False=已探测结果（进程内只探测一次）。
_docker_available_cache: bool | None = None


def reset_docker_cache() -> None:
    """清空 docker 可用性探测缓存（供测试重探；运行时环境变化后也可调用）。"""
    global _docker_available_cache
    _docker_available_cache = None


def _probe_docker() -> bool:
    """单次探测：docker CLI 存在（which）且 `docker info` 0 退出（5s 超时）。"""
    docker_path = shutil.which("docker")
    if docker_path is None:
        return False
    try:
        proc = subprocess.run(
            [docker_path, "info"],
            capture_output=True,
            timeout=_DOCKER_PROBE_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError):
        # 二进制瞬失 / 守护进程未启动 / 探针超时：一律按不可用处理（降级子进程路径）
        return False
    return proc.returncode == 0


def docker_available() -> bool:
    """探测 Docker 是否可用；结果进程内缓存，只真实探测一次。"""
    global _docker_available_cache
    if _docker_available_cache is None:
        _docker_available_cache = _probe_docker()
    return _docker_available_cache


@dataclass
class SandboxResult:
    """一次沙箱执行的产物；exit_code 为 None 表示进程尚未退出（理论上不出现）。

    truncated（W10 F4）：stdout 或 stderr 任一超过 _OUTPUT_LIMIT_BYTES 被限量
    排空（超出部分读取后丢弃）时置 True，且对应 tail 首行带 [output truncated]
    标记行。尾部默认值字段，向后兼容。

    backend（W13-A2）：实际执行后端，"subprocess"（Windows 子进程路径）或
    "docker"（容器化路径）。诚实标注：use_docker=True 但探测失败静默降级时，
    此处如实标 "subprocess"；docker run 自身失败仍标 "docker"。默认值
    "subprocess" 向后兼容（既有调用方无需感知）。
    """

    exit_code: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    timed_out: bool = False
    duration_sec: float = 0.0
    truncated: bool = False
    backend: str = "subprocess"


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


def _mark_truncated(text: str, truncated: bool) -> str:
    """被限量的流在 tail 首行插入 [output truncated] 标记行（诚实降级可见）。"""
    if not truncated:
        return text
    if not text:
        return _TRUNCATED_MARKER
    return f"{_TRUNCATED_MARKER}\n{text}"


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
    """asyncio 子进程沙箱（W13-A2 起可选 Docker 容器化后端，探测失败自动降级）。"""

    def __init__(
        self,
        default_timeout: float = 60.0,
        tail_lines: int = 80,
        use_docker: bool = True,
    ) -> None:
        """use_docker 仅表达意愿：实际走 Docker = use_docker AND docker_available()。

        探测失败（本机无 docker CLI / 守护进程不可用）时静默降级子进程路径，
        SandboxResult.backend 如实标注，不告警不抛异常。
        """
        self._default_timeout = max(0.1, float(default_timeout))
        self._tail_lines = max(1, int(tail_lines))
        self._use_docker = bool(use_docker)

    @staticmethod
    def _docker_wrap(command: list[str], cwd: Path) -> list[str]:
        """把命令包装为 docker run 形式：无网络 + 内存/CPU 限额 + 挂载 cwd 到 /work。

        镜像默认 python:3.12-slim，环境变量 CODEAUDIT_DOCKER_IMAGE 可覆盖。docker
        可执行文件用 which 解析路径（与探针一致；仅探测通过后才会走到这里）。
        """
        docker_path = shutil.which("docker") or "docker"
        image = os.environ.get(_DOCKER_IMAGE_ENV) or _DEFAULT_DOCKER_IMAGE
        workdir = Path(cwd).resolve()
        return [
            docker_path,
            "run",
            "--rm",
            "--network",
            "none",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "-v",
            f"{workdir}:/work",
            "-w",
            "/work",
            image,
            *command,
        ]

    async def run(
        self,
        command: list[str],
        cwd: Path,
        timeout_sec: float | None = None,
    ) -> SandboxResult:
        """执行一条命令（列表形式，不经 shell），返回退出码与末尾输出。

        W13-A2：use_docker=True 且 docker_available() 时把命令包装为 docker run
        （容器内工作目录 /work）；docker CLI 本身就是子进程，超时（wait_for kill
        docker CLI）、输出限量、truncated 语义全部复用。探测失败静默降级子进程
        路径，backend 字段诚实标注实际后端；docker run 自身失败（如镜像拉取失败
        exit_code=125 类）如实返回，不重试不降级。
        """
        if not command:
            raise SandboxError("command 不能为空")
        use_docker = self._use_docker and docker_available()
        if use_docker:
            command = self._docker_wrap(command, cwd)
        backend = "docker" if use_docker else "subprocess"
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
                backend=backend,
            )

        async def _drain(stream: Any) -> tuple[bytes, bool]:
            """限量排空一个输出流，返回（已保存字节, 是否丢弃过数据）。

            关键语义：累计读取达到 _OUTPUT_LIMIT_BYTES 后必须**继续读但丢弃**，
            不能停止读取——否则子进程写满 PIPE 缓冲后会永久阻塞，直至被超时
            击杀。"排空但丢弃"同时保证内存有界（≈ LIMIT + 一个 chunk）与子
            进程能正常退出（exit_code 不被误杀）。
            """
            if stream is None:
                return b"", False
            chunks: list[bytes] = []
            total = 0
            discarded = False
            try:
                while True:
                    chunk = await stream.read(_OUTPUT_CHUNK_BYTES)
                    if not chunk:
                        break
                    if total < _OUTPUT_LIMIT_BYTES:
                        chunks.append(chunk)
                        total += len(chunk)
                    else:
                        discarded = True
            except Exception:
                return b"".join(chunks), discarded
            return b"".join(chunks), discarded

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
        stdout, stdout_truncated = await out_task
        stderr, stderr_truncated = await err_task
        duration = time.monotonic() - start
        return SandboxResult(
            exit_code=proc.returncode,
            stdout_tail=_mark_truncated(_tail(_decode(stdout), self._tail_lines), stdout_truncated),
            stderr_tail=_mark_truncated(_tail(_decode(stderr), self._tail_lines), stderr_truncated),
            timed_out=timed_out,
            duration_sec=round(duration, 3),
            truncated=stdout_truncated or stderr_truncated,
            backend=backend,
        )

    async def run_tests(self, framework: str, cwd: Path, target: str = "") -> SandboxResult:
        """在沙箱中运行白名单测试框架；未知框架抛 SandboxError。

        W13-A2：与 run() 共用 Docker 包装（探测通过即容器化执行）。v1 边界：
        test_runner_available() 仍是宿主探测、语义未变；容器内 pytest 可用性由
        镜像自带保证（pytest 命令以宿主 [sys.executable, -m pytest] 原样透传进
        容器，宿主解释器路径在容器内是否有效取决于镜像，生产镜像需提供兼容入口）。
        """
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
