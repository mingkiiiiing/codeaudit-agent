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

W14-A2（审计缺陷修复 M-2a/M-2b/M-3）：
- M-2a 开关接线：构造参数 backend（"subprocess" | "docker"，权威入口，大小写与
  首尾空白容忍）补齐 use_docker 布尔之外的等价入口；None（缺省）时回退旧式
  use_docker 语义，既有调用方行为完全不变。配置链路：AuditConfig.sandbox_backend
  （默认 "subprocess"）← 配置文件 / 环境变量 CODEAUDIT_SANDBOX_BACKEND / CLI
  --sandbox-backend 分层覆盖，fix/testgen stage 传入。非法值诚实降级 subprocess，
  降级事实写入实例属性 backend_note（非空即降级过），供调用方在进度事件中如实
  标注（沿用项目诚实降级风格）。默认值 subprocess：行为与 W13 收口裁决一致
  （commit e2685b0：CI 实证自动启用会把宿主解释器包进容器导致全 127）。
- M-2b 容器清理：docker run 加 --name codeaudit-<短uuid>；run() 结束（超时击杀
  docker CLI 进程后 / 正常退出 finally）按容器名补 docker rm -f 兜底删除——
  --rm 在 CLI 被杀时清理失效，容器会泄漏；清理子进程带短超时、全吞异常，
  不掩盖原超时语义（正常退出时容器已被 --rm 删除，rm -f 报 no such container
  同样被吞，幂等无害）。
- M-3 排空兜底：超时击杀进程后的 stdout/stderr 排空 await 加
  _DRAIN_FALLBACK_TIMEOUT_SEC（5s）兜底超时——Windows 下击杀不杀进程树，孙进程
  继承管道写端时 read 永远等不到 EOF，修复前 run() 会永久挂起；超时则 cancel
  排空任务、用已读到部分继续，按现有 [output truncated] 标记机制如实标注
  truncated，run() 必然返回（异常不吞、外层取消正常传播）。实测（Windows/Py3.13）
  发现挂死不止排空一段：asyncio 的 proc.wait() 在 Windows 上要等**全部管道
  transport 关闭**才返回（_try_finish），孙进程持写端时同样挂死——故超时击杀后
  先关闭读端 transport（促 pending IOCP recv abort，已缓冲数据仍可消费）、
  kill 后的 wait 回收再加 _PROC_WAIT_FALLBACK_SEC（5s）短兜底，排空兜底退为
  最后防线。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import time
import uuid
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
# W14-A2 M-2b：`docker rm -f` 兜底清理子进程的超时（尽力而为，失败不掩盖原语义）。
_DOCKER_RM_TIMEOUT_SEC = 5.0
# W14-A2 M-3：排空 await 的兜底超时（超时击杀进程后管道 EOF 可能永不到来——
# Windows 下孙进程继承写端；此时 cancel 排空任务、用已读部分继续并标 truncated）。
_DRAIN_FALLBACK_TIMEOUT_SEC = 5.0
# W14-A2 M-3：击杀后 proc.wait() 回收等待的兜底超时（Windows asyncio 的 wait 要等
# 全部管道关闭才返回；先 _close_pipes 后通常立即完成，此兜底仅防极端场景）。
_PROC_WAIT_FALLBACK_SEC = 5.0
# W14-A2 M-2a：沙箱后端合法值（backend 参数归一化口径；大小写与首尾空白容忍）。
_SANDBOX_BACKENDS = ("subprocess", "docker")
# W15-E：Docker 资源限额原散落字面量（"512m" / "1"）提为具名常量（值不变），
# 对齐 docs/02 §6 "生产建议 Docker（--network none --memory 512m）"。
_DOCKER_MEMORY_LIMIT = "512m"
_DOCKER_CPU_LIMIT = "1"

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
    """一次沙箱执行的产物；exit_code 为 None 表示退出码未知。

    exit_code=None 语义（W14-A2 M-3 起）：正常路径进程必已退出，exit_code 非
    None；唯一例外是超时击杀后 Windows 管道挂死兜底触发（孙进程持写端且
    transport 关闭仍未能回收退出码）——此时 timed_out=True 已如实标注超时，
    调用方按失败/复核语义处理（exit_code=None != 0 天然不满足全绿判定）。

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


async def _collect_drain(
    task: "asyncio.Future[Any]", sink: dict[str, Any], timeout: float
) -> tuple[bytes, bool]:
    """等待排空任务完成并取出已保存输出；带兜底超时（W14-A2 M-3）。

    修复前：超时击杀进程后的 `await 排空任务` 无兜底——Windows 下击杀不杀进程
    树，孙进程（如 pytest 派生）继承管道写端时 read 永远等不到 EOF，run() 永久
    挂起、server 任务卡死在 running。修复后：排空 await 有 timeout 兜底，超时则
    请求取消排空任务（**不等待取消收割**——Windows IOCP 上已提交的管道 ReadFile
    无法取消，wait_for 会在等收割时二次挂死；配合调用方先 close transport，本
    分支通常只是最后防线）、用 sink 中已读到的部分继续，并按"被截断"如实标注
    （沿用 [output truncated] 标记机制）。返回 (data, truncated)。
    """
    done, _pending = await asyncio.wait({task}, timeout=timeout)
    if not done:
        task.cancel()  # 请求取消，不等待收割（run() 必然返回优先）
        return b"".join(sink["chunks"]), True
    return b"".join(sink["chunks"]), bool(sink["discarded"])


def _close_pipes(proc: Any) -> None:
    """关闭子进程 stdout/stderr 读端 transport（W14-A2 M-3 的关键解除手段）。

    Windows IOCP 上已提交的管道 ReadFile 无法取消：孙进程继承写端时 EOF 永远
    不来，排空任务的 read 挂死；且 asyncio 的 proc.wait() 要等全部管道 transport
    关闭才返回（_try_finish），回收等待同样挂死。close transport 促使 pending
    recv 立即以 abort/feed_eof 完成——StreamReader 已缓冲的数据仍可被消费（不丢
    已读输出），排空任务与回收等待随之收尾。幂等：已关闭的 transport 再 close
    无副作用（正常退出路径 transport 已被 asyncio 关闭）。
    """
    for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
        # StreamReader 只有私有 _transport 属性（实测 Py3.13，无公开 transport）
        transport = getattr(stream, "_transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()


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
        use_docker: bool = False,
        backend: str | None = None,
    ) -> None:
        """backend（W14-A2 M-2a）：权威后端入口，合法值 "subprocess" | "docker"。

        - backend=None（缺省）时回退旧式 use_docker 布尔入口（True=愿意走
          Docker），既有调用方行为完全不变；两入口同时给出时 backend 优先；
        - 非法值诚实降级 subprocess，降级事实写入实例属性 backend_note（非空
          字符串），供调用方（fix/testgen stage）在进度事件中如实标注；
        - 实际是否走 Docker = 意愿 AND docker_available()。探测失败（本机无
          docker CLI / 守护进程不可用）时静默降级子进程路径，SandboxResult.backend
          如实标注，不告警不抛异常。

        W13 收口裁决（CI 实证）：默认 subprocess/opt-out（opt-in 实验特性）。
        GitHub Actions 的 ubuntu runner 预装 Docker，自动启用会把命令包装进
        python:3.12-slim 容器，而宿主解释器路径（sys.executable 指向
        hostedtoolcache）与 node 在容器内不存在 → 全部 127 失败。容器化需要配套
        镜像（python+node 双运行时）与路径映射策略，属后续演进；当前仅显式
        指定 docker 且镜像自足时可用。
        """
        if backend is None:
            request = "docker" if use_docker else "subprocess"
            self.backend_note = ""
        else:
            request = str(backend).strip().lower()
            if request in _SANDBOX_BACKENDS:
                self.backend_note = ""
            else:
                request = "subprocess"
                self.backend_note = (
                    f"沙箱后端配置 {backend!r} 非法（合法值 subprocess/docker），已诚实降级 subprocess"
                )
        self.backend_requested = request  # 意愿后端（探测失败仍可能在 run() 时降级）
        self._default_timeout = max(0.1, float(default_timeout))
        self._tail_lines = max(1, int(tail_lines))
        self._use_docker = request == "docker"

    def _docker_wrap(self, command: list[str], cwd: Path) -> tuple[list[str], str]:
        """把命令包装为 docker run 形式：无网络 + 内存/CPU 限额 + 挂载 cwd 到 /work。

        镜像默认 python:3.12-slim，环境变量 CODEAUDIT_DOCKER_IMAGE 可覆盖。docker
        可执行文件用 which 解析路径（与探针一致；仅探测通过后才会走到这里）。
        返回 (argv, container_name)：W14-A2 M-2b 起 argv 带 --name
        codeaudit-<短uuid>，容器名回传给 run() 供超时/结束时 docker rm -f 兜底
        清理（--rm 在 CLI 进程被杀时失效）。
        """
        docker_path = shutil.which("docker") or "docker"
        image = os.environ.get(_DOCKER_IMAGE_ENV) or _DEFAULT_DOCKER_IMAGE
        workdir = Path(cwd).resolve()
        container_name = f"codeaudit-{uuid.uuid4().hex[:12]}"
        return [
            docker_path,
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            "none",
            "--memory",
            _DOCKER_MEMORY_LIMIT,  # W15-E：字面量 "512m" 提为具名常量（值不变）
            "--cpus",
            _DOCKER_CPU_LIMIT,  # W15-E：字面量 "1" 提为具名常量（值不变）
            "-v",
            f"{workdir}:/work",
            "-w",
            "/work",
            image,
            *command,
        ], container_name

    async def _docker_rm_force(self, container_name: str) -> None:
        """`docker rm -f <name>` 兜底清理（W14-A2 M-2b）：短超时、尽力而为、全吞异常。

        用 asyncio 子进程（而非阻塞 subprocess.run）——server 单事件循环场景下
        清理最多占用 _DOCKER_RM_TIMEOUT_SEC 的时间片，不会卡死整个 loop。清理
        失败（CLI 瞬失 / 超时 / 容器已不存在）一律静默返回，不掩盖原超时语义。
        """
        docker_path = shutil.which("docker") or "docker"
        try:
            cleanup = await asyncio.create_subprocess_exec(
                docker_path,
                "rm",
                "-f",
                container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (FileNotFoundError, PermissionError, OSError):
            return
        try:
            await asyncio.wait_for(cleanup.wait(), timeout=_DOCKER_RM_TIMEOUT_SEC)
        except (asyncio.TimeoutError, TimeoutError):
            with contextlib.suppress(Exception):
                cleanup.kill()
            with contextlib.suppress(Exception):
                await cleanup.wait()

    async def run(
        self,
        command: list[str],
        cwd: Path,
        timeout_sec: float | None = None,
    ) -> SandboxResult:
        """执行一条命令（列表形式，不经 shell），返回退出码与末尾输出。

        W13-A2：use_docker/backend=docker 且 docker_available() 时把命令包装为
        docker run（容器内工作目录 /work）；docker CLI 本身就是子进程，超时
        （wait_for kill docker CLI）、输出限量、truncated 语义全部复用，并在
        结束时 docker rm -f 兜底清容器（W14-A2 M-2b）。探测失败静默降级子进程
        路径，backend 字段诚实标注实际后端；docker run 自身失败（如镜像拉取失败
        exit_code=125 类）如实返回，不重试不降级。排空 await 带兜底超时
        （W14-A2 M-3）：孙进程继承管道写端时 run() 不再永久挂起。
        """
        if not command:
            raise SandboxError("command 不能为空")
        use_docker = self._use_docker and docker_available()
        container_name = ""
        if use_docker:
            command, container_name = self._docker_wrap(command, cwd)
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

        def _make_sink() -> dict[str, Any]:
            """排空进度容器：chunks 实时累积（W14-A2 M-3 兜底超时后仍可取已读部分）。"""
            return {"chunks": [], "total": 0, "discarded": False}

        async def _drain(stream: Any, sink: dict[str, Any]) -> None:
            """限量排空一个输出流到 sink（chunks / total / discarded）。

            关键语义：累计读取达到 _OUTPUT_LIMIT_BYTES 后必须**继续读但丢弃**，
            不能停止读取——否则子进程写满 PIPE 缓冲后会永久阻塞，直至被超时
            击杀。"排空但丢弃"同时保证内存有界（≈ LIMIT + 一个 chunk）与子
            进程能正常退出（exit_code 不被误杀）。

            W14-A2（M-3）：进度实时写入 sink——本任务被兜底超时 cancel 后，
            run() 仍能取用已读到的部分输出（不丢已产出内容）。
            """
            if stream is None:
                return
            try:
                while True:
                    chunk = await stream.read(_OUTPUT_CHUNK_BYTES)
                    if not chunk:
                        break
                    if sink["total"] < _OUTPUT_LIMIT_BYTES:
                        sink["chunks"].append(chunk)
                        sink["total"] += len(chunk)
                    else:
                        sink["discarded"] = True
            except Exception:
                pass  # 读异常：保留已读部分（与修复前"异常时返回已保存字节"语义一致）

        out_sink = _make_sink()
        err_sink = _make_sink()
        out_task = asyncio.create_task(_drain(proc.stdout, out_sink))
        err_task = asyncio.create_task(_drain(proc.stderr, err_sink))
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            timed_out = True
            with contextlib.suppress(Exception):
                proc.kill()  # Windows 下为 TerminateProcess
            # W14-A2（M-3）：Windows asyncio 的 proc.wait() 要等**全部管道 transport
            # 关闭**才返回（_try_finish 语义）——孙进程继承写端时 EOF 永不到来，kill
            # 后的回收等待会挂死到孙进程退出。先关读端 transport（促 pending IOCP
            # recv abort；StreamReader 已缓冲数据仍可消费），再以短兜底回收 wait。
            _close_pipes(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=_PROC_WAIT_FALLBACK_SEC)
            except (asyncio.TimeoutError, TimeoutError):
                pass  # 极端场景拿不到退出码：如实返回 exit_code=None + timed_out=True
        finally:
            # W14-A2（M-2b）：Docker 路径容器兜底清理。超时击杀 docker CLI 进程后
            # 容器本身仍在运行（--rm 清理失效）；非超时路径同样执行（正常退出时
            # 容器已被 --rm 删除，rm -f 报 no such container 被吞，幂等无害）。
            if container_name:
                await self._docker_rm_force(container_name)
        # W14-A2（M-3）：关读端 transport（幂等）。正常路径 EOF 本已到达、transport
        # 已被 asyncio 关闭，此调用无副作用；孙进程存活等残余场景在此兜底解除。
        _close_pipes(proc)
        stdout, stdout_truncated = await _collect_drain(out_task, out_sink, _DRAIN_FALLBACK_TIMEOUT_SEC)
        stderr, stderr_truncated = await _collect_drain(err_task, err_sink, _DRAIN_FALLBACK_TIMEOUT_SEC)
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
