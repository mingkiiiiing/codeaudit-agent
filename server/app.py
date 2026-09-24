"""FastAPI 服务：创建/查询/删除审计任务、SSE 事件流、报告下载、SPA 托管与演示页回退。

W11 形态演进（契约 docs/16 §1.1/§1.2）：任务表由内存 AUDITS dict 迁移为 TaskStore
（SQLite WAL，audit/taskstore.py）——服务重启后终态任务与报告仍可查询下载，启动
（lifespan）时 sweep_interrupted 把遗留 queued/running 置 failed（服务重启中断）；
审计任务经 asyncio.to_thread 在独立线程执行（线程内 asyncio.run(run_audit(...))，每任务
独立事件循环，CPU 密集段不再饿死服务循环）；DELETE 改为协作式取消——移除表行即取消
信号，任务线程在 emitter 事件边界发现 is_cancelled（行已删除/取消标志置位）即抛
CancelledError，由 BaseException 兜底落 failed（行已被删则幽灵静默）。
模块级 _STORE 惰性初始化：db 路径 env CODEAUDIT_DB_PATH 优先，否则 <work_root>/audits.db；
测试经 monkeypatch.setattr(server.app, "_STORE", TaskStore(...)) 注入。模块级引用
run_audit 便于测试注入假流水线（任务线程内经模块属性延迟取用，monkeypatch 同样生效）。
服务治理（契约 v2.1/v2.2）：后台任务经模块级 _RUN_GATE 信号量排队执行（per-worker，
超出的停留既有 queued 状态；多 worker 全局上限 = workers × gate 容量）；429 准入改走
store.count_active()（多 worker 下即全局计数）；FIFO 容量淘汰经 store.prune(_AUDITS_MAX)
只淘汰终态；SSE 轮询改从 store 按 seq 游标读取（回放/跟随语义不变）；上传按 256 KB
分块流式落盘，超限立即 413 并停止读取（不再整包缓冲进内存）。
W14 修复（M-4/M-5/M-6）：磁盘回收——store 携带 work_root，prune / 启动 sweep /
DELETE（终态）删行时同步回收 <work_root>/<audit_id>/ 与 uploads zip，DELETE 对
queued/running 只删行（协作取消），目录由执行体幽灵收尾回收（_cleanup_if_ghost）；
治理上限（_MAX_RUNNING_AUDITS/_MAX_PENDING_AUDITS）改惰性解析——.env 运行期加载后
即生效，两个模块名保留为测试覆写入口（非 None 直接生效）；上传在读流完成后、建任务
前做第二次权威准入校验（M-6 TOCTOU），拒绝时清掉已落盘 zip 保持零残留。
W15-A1（docs/20 §4.1，全部 opt-in，env 缺省 = 治理关闭、既有行为零变化）：
- 鉴权——/api/*（除 GET /api/health）在 CODEAUDIT_API_TOKEN 非空时要求
  Authorization: Bearer <token> 或 X-API-Token 头（常量时间比较），失败 401；
  中间件注册先于 CORS（鉴权在 CORS 内层），预检 OPTIONS 由 CORS 外层短路应答；
  token 为空时 lifespan 打 WARNING 提醒无鉴权仅限本机；
- source_path 白名单——CODEAUDIT_SOURCE_ROOTS 非空时 POST /api/audits 的
  source_path 必须 resolve 后位于任一根之内，越界 400；
- 限流——CODEAUDIT_RATE_LIMIT > 0 时对 POST/DELETE 按客户端 IP 做内存滑动窗口
  限流，超限 429；0 = 关闭；
- SSE 并发上限——模块常量 _MAX_SSE_STREAMS = 50，超限新建事件流 429 不排队，
  名额在 generator finally 释放（正常收流 / 客户端断开 / 异常路径均覆盖）；
- 存储接线——lifespan 启动 sweep 按 CODEAUDIT_SWEEP_GRACE_SEC 传 grace_seconds
  （卡B 契约参数，运行时签名探测兼容旧签名），yield 后关闭 store（close 幂等）。
P0-9（resume 全接线）：POST /api/audits/{audit_id}/resume 断点续跑端点——守卫与
CLI `codeaudit resume` 单源（cli._resume_guard_action 纯函数），running 滞留态按
grace 窗口 sweep 自愈；config 自任务行 config_json 重建（cli._rebuild_config_
from_task_json），后台执行复用 _launch_audit（run_audit 透传 resume_stages）。
F6-R1（W26 第六轮审计清偿）：resume 重建 config 后复验 SOURCE_ROOTS 白名单
（与创建端点同口径，越界/缺失 400），先校验后 mark_resuming，校验失败不改状态。
W27-B（结果版本化）查询面：GET /api/audits/{id}/reports 报告历史摘要列表
（404=任务不存在）、GET /api/audits/{id}/reports/{seq} 指定历史版本完整 JSON
（404=任务或版本不存在）；双写在 TaskStore.set_report 同事务完成
（audit/taskstore.py），当前报告 = 历史最大 seq，两端点跟随 /api/* 既有鉴权中间件。
W28-B（safe-rename server 入口）：POST /api/rename 符号重命名端点——plan/apply
两段式复用 audit/refactor/rename.py 的 plan_rename/apply_rename（tree-sitter
identifier 节点区间替换，零 LLM、零子串/正则替换）。请求体 {"source_path",
"old_name", "new_name", "apply"}：apply=false（缺省）= dry-run 只返回 diff 预览
不落盘；apply=true 才落盘（对应 CLI --yes 语义，字段名避让布尔语境的 yes/apply
混用，与补丁端点的 ApplyPatchRequest.yes 相互独立）。治理链全部前置（F6-R1
教训：第一版即带全治理，不留待审计补课），逐条列出：
1. 鉴权——跟随 /api/* 既有中间件（CODEAUDIT_API_TOKEN 非空时要求 Bearer /
   X-API-Token 凭据，失败 401），端点零额外代码（中间件按 /api/ 前缀全覆盖）；
2. SOURCE_ROOTS 白名单——CODEAUDIT_SOURCE_ROOTS 非空时 source_path 必须
   resolve 后位于任一根之内，越界 400（复用 _ensure_source_allowed，与创建/
   resume 端点同口径；白名单判定先于存在性检查，不泄露越界路径在本机的存在性）；
3. 限流——POST 属写方法，被既有全局中间件滑动窗口覆盖（CODEAUDIT_RATE_LIMIT>0
   超限 429，W26 卡 A 定性），端点零额外代码；
4. 业务守卫——source_path 空/不存在 400；plan 阶段 errors 非空（非法名/
   old==new/多定义点/非 UTF-8 或解析失败）→ 400 中文 detail（plan.errors 原文
   拼接），计划阶段即拒绝、绝不落盘（apply=true 亦然）；
5. apply 复检失败（目标内容与计划时不一致/AST 复检失败）→ 409 Conflict +
   逐文件原因——rename 模块 all-or-nothing 已保证零落盘，409=请求前提与资源
   当前状态冲突的准确语义（区别于 500 的服务端故障）。
响应：{"ok", "applied", "files", "replace_points", "diffs", "errors"}，dry-run
时 applied=false；解析/落盘等阻塞操作经 asyncio.to_thread 执行（apply_patch 先例）。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hmac
import inspect
import json
import logging
import os
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from audit import __version__ as _AUDIT_VERSION
from audit.config import AuditConfig
from audit.models import AuditReport, Category, Severity
from audit.orchestrator.pipeline import resume_stage_done, run_audit  # noqa: F401 —— 模块级引用，供测试注入替身
from audit.refactor.rename import apply_rename, plan_rename
from audit.report.render import render_html, render_markdown
from audit.taskstore import TaskStore
from audit.utils import new_audit_id
from cli import _rebuild_config_from_task_json, _resume_guard_action, _resume_sweep_grace_seconds

# 项目根目录（web/ 静态演示页所在）
_ROOT = Path(__file__).resolve().parent.parent
_WEB_DIR = _ROOT / "web"
# SPA 构建产物目录（模块级常量，便于测试 monkeypatch）
_SPA_DIST = _ROOT / "frontend" / "dist"

# 上传 zip 大小上限（模块级常量，便于测试 monkeypatch 成小值）
_UPLOAD_MAX_BYTES = 200 * 1024 * 1024

# F2 流式上传：分块读取的 chunk 大小（256 KB；模块级常量，便于测试 monkeypatch 验证多块路径）
_UPLOAD_CHUNK_BYTES = 262144


def _int_from_env(name: str, default: int, minimum: int) -> int:
    """解析环境变量为整数：缺省 / 解析失败 / 低于下限（含负数与非法值）均回落默认值。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


# F5 准入控制上限（M-5 惰性解析）：import 期不再固化——治理上限在每次取用时实时
# 读 env，lifespan/请求路径里 from_env 触发 .env 自动加载（CODEAUDIT_* setdefault 进
# os.environ）之后再取值即生效，对齐 CODEAUDIT_DB_PATH 的生效时机；解析结果按
# （变量名, 原始值, 下限）缓存在模块级小字典，原始值未变时不重复解析。
# 两个模块级名字同时是测试覆写入口：非 None 时直接生效（既有测试的
# monkeypatch.setattr(server_app, "_MAX_PENDING_AUDITS", 2) 写法保持可用），
# None = 按 env 惰性解析（生产缺省）。
# - 同时运行中的任务上限（后台任务经 _RUN_GATE 排队，超出的停留既有 queued 状态；
#   per-worker 口径，多 worker 下全局并发上限 = workers × 该值，契约 docs/16 §1.2）；
# - 非终态（queued+running）任务总数上限，达到后 POST 建任务返回 429
#   （W11 起经 store 计数，多 worker 下即全局准确）。
_MAX_RUNNING_AUDITS: int | None = None
_MAX_PENDING_AUDITS: int | None = None

# 惰性解析小缓存：键含原始 env 值——env 变化（含 .env 加载 / 测试 setenv）自然失效
_ENV_LIMIT_CACHE: dict[tuple[str, str | None, int], int] = {}


def _get_max_running() -> int:
    """同时运行任务上限（惰性）：覆写入口优先，否则实时读 CODEAUDIT_MAX_RUNNING。"""
    if _MAX_RUNNING_AUDITS is not None:
        return _MAX_RUNNING_AUDITS
    return _lazy_env_limit("CODEAUDIT_MAX_RUNNING", 4, minimum=1)


def _get_max_pending() -> int:
    """非终态任务总数上限（惰性）：覆写入口优先，否则实时读 CODEAUDIT_MAX_PENDING
    （下限与当前 running 值绑定，低于其回落默认——与原 import 期解析语义一致）。"""
    if _MAX_PENDING_AUDITS is not None:
        return _MAX_PENDING_AUDITS
    return _lazy_env_limit("CODEAUDIT_MAX_PENDING", 20, minimum=_get_max_running())


def _lazy_env_limit(name: str, default: int, minimum: int) -> int:
    """实时解析治理上限 env（每次调用读 os.environ），按（名, 原始值, 下限）缓存。"""
    raw = os.environ.get(name)
    key = (name, raw, minimum)
    cached = _ENV_LIMIT_CACHE.get(key)
    if cached is not None:
        return cached
    value = _int_from_env(name, default, minimum)
    _ENV_LIMIT_CACHE[key] = value
    return value


# 运行闸门（模块级全局，与 _TASKS/_RUNNING 同层；create_app 每次建实例但闸门模块级共享，
# gate 亦同）。M-5：容量随 gate 惰性初始化（首次取用时按 _get_max_running() 建）——
# .env 运行期加载后 CODEAUDIT_MAX_RUNNING 即生效；测试排队语义可直接 monkeypatch
# 替换为新 Semaphore（非 None 时 _get_run_gate 原样返回）。
_RUN_GATE: asyncio.Semaphore | None = None


def _get_run_gate() -> asyncio.Semaphore:
    """返回运行闸门（惰性初始化：首个任务启动前 .env 已被 from_env 加载）。"""
    global _RUN_GATE
    if _RUN_GATE is None:
        _RUN_GATE = asyncio.Semaphore(_get_max_running())
    return _RUN_GATE

# 持久化任务存储（W11 §1.1）：惰性初始化（见 _get_store）；
# 测试经 monkeypatch.setattr(server.app, "_STORE", TaskStore(...)) 注入替身实例。
_STORE: TaskStore | None = None

_TERMINAL_STATUSES = ("done", "failed")

# 任务表容量上限（R1-6）：新建任务后经 store.prune 淘汰最旧的终态行（FIFO 容量淘汰，
# 只淘汰 done/failed，queued/running 不受影响——语义与内存表时代一致）
_AUDITS_MAX = 50

# 本 worker 的运行句柄：{audit_id: asyncio.Task}（W11 sticky 执行模型下本机任务登记，
# 替代内存表时代的 entry["task"]；DELETE 不再 cancel 外层 task——cancel 杀不掉线程
# 反而破坏协作取消语义，见 delete_audit 与 _run_audit_sync）。
_RUNNING: dict[str, asyncio.Task] = {}

# 后台任务引用（R1-7）：create_task 只返回弱引用，必须由本集合持有，
# 任务结束的 done callback 中 discard，防止被 GC 中途回收。
_TASKS: set[asyncio.Task] = set()

# 合法过滤值（与 audit.models 枚举保持一致）
_SEVERITY_VALUES = {s.value for s in Severity}
_CATEGORY_VALUES = {c.value for c in Category}
_DEFAULT_ISSUES_LIMIT = 50

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------- W15-A1 安全治理（docs/20 §4.1）
# SSE 并发事件流上限（模块常量便于测试 monkeypatch 成小值）：超限新建 SSE 直接 429
# 不做排队；计数为进程内口径（per-worker），多 worker 全局上限 = workers × 该值，
# 与 _RUN_GATE 的治理口径一致。_SSE_ACTIVE 名额由 _acquire_sse_slot/_release_sse_slot
# 维护（generator finally 保证连接结束必释放），仅事件循环线程访问，无锁。
_MAX_SSE_STREAMS = 50
_SSE_ACTIVE = 0

# 限流滑动窗口记账：{client_ip: [monotonic 时间戳]}——仅事件循环线程访问（中间件
# 内同步读写、无 await 间隙），无需加锁；窗口 60 秒（rate_limit_per_min 的"每分钟"口径）。
_RATE_LIMIT_WINDOW_SEC = 60.0
_RATE_LIMIT_EVENTS: dict[str, list[float]] = {}


def _get_security_config() -> AuditConfig:
    """W15-A1：安全治理配置惰性取用——from_env 触发 .env 加载后 env 即生效，
    与 _get_max_pending 的惰性解析口径一致（import 期不固化，测试 setenv 即生效）。"""
    return AuditConfig.from_env()


def _tokens_equal(provided: str, expected: str) -> bool:
    """常量时间比较（防时序侧信道）：编码为 bytes 后经 hmac.compare_digest。"""
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _request_token_ok(request: Request, expected: str) -> bool:
    """校验请求凭据：Authorization: Bearer <token> 或 X-API-Token 头，任一匹配即通过。"""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        bearer = auth[len("Bearer "):].strip()
        if bearer and _tokens_equal(bearer, expected):
            return True
    header_token = request.headers.get("x-api-token", "")
    return bool(header_token) and _tokens_equal(header_token, expected)


def _rate_limit_exceeded(client_ip: str, limit_per_min: int) -> bool:
    """滑动窗口限流记账：超限返回 True（调用方回 429），否则记一笔并放行。

    只保留窗口内时间戳；桶数量膨胀时顺带回收全空桶，防长期运行内存无界增长。
    """
    now = time.monotonic()
    events = _RATE_LIMIT_EVENTS.setdefault(client_ip, [])
    events[:] = [t for t in events if now - t < _RATE_LIMIT_WINDOW_SEC]
    if len(events) >= limit_per_min:
        return True
    events.append(now)
    if len(_RATE_LIMIT_EVENTS) > 4096:
        for ip in [ip for ip, ev in _RATE_LIMIT_EVENTS.items() if not ev]:
            _RATE_LIMIT_EVENTS.pop(ip, None)
    return False


def _acquire_sse_slot() -> bool:
    """占用一个 SSE 流名额；已达上限返回 False（调用方回 429，不做排队）。"""
    global _SSE_ACTIVE
    if _SSE_ACTIVE >= _MAX_SSE_STREAMS:
        return False
    _SSE_ACTIVE += 1
    return True


def _release_sse_slot() -> None:
    """释放 SSE 流名额（下限钳 0，防异常路径重复释放把计数打穿为负）。"""
    global _SSE_ACTIVE
    _SSE_ACTIVE = max(0, _SSE_ACTIVE - 1)


def _ensure_source_allowed(source: Path) -> None:
    """W15-A2：source_path 白名单——CODEAUDIT_SOURCE_ROOTS 非空时，POST /api/audits
    的 source_path 必须 resolve 后位于任一根之内（根目录本身也放行），越界 400；
    空 = 不限制（既有行为）。白名单判定先于存在性检查：不向调用方泄露越界路径
    是否存在于本机。
    """
    roots = _get_security_config().allowed_source_roots
    if not roots:
        return
    try:
        resolved = source.resolve()
    except OSError:
        raise HTTPException(status_code=400, detail=f"源路径无法解析：{source}") from None
    for root in roots:
        try:
            resolved.relative_to(Path(root).resolve())
            return
        except ValueError:
            continue
    raise HTTPException(status_code=400, detail=f"source_path 不在允许的根目录内：{source}")


def _sweep_on_startup() -> int:
    """W15-A5 存储接线（docs/20 §4.3）：启动 sweep 携带 grace_seconds——
    CODEAUDIT_SWEEP_GRACE_SEC（缺省 0 = 既有语义）经 _int_from_env 惰性解析。
    sweep_interrupted 的 grace_seconds 形参由卡B（W15-B）并行落地；为不阻塞并行
    开发，此处按运行时签名探测：支持参数即按契约调用，否则回落无参调用（旧签名
    语义不变）。卡B 合入后探测恒走契约分支，本函数无需再改。
    """
    store = _get_store()
    grace_seconds = _int_from_env("CODEAUDIT_SWEEP_GRACE_SEC", 0, minimum=0)
    if "grace_seconds" in inspect.signature(store.sweep_interrupted).parameters:
        return store.sweep_interrupted(grace_seconds=grace_seconds)
    return store.sweep_interrupted()


def _get_store() -> TaskStore:
    """返回模块级任务存储（惰性初始化，进程一个实例）。

    db 路径：env CODEAUDIT_DB_PATH 优先，否则 <work_root>/audits.db（work_root 取
    AuditConfig.from_env().work_root，与上传/报告目录同一基座）。

    W12 收口修复：from_env 调用必须先于 env 读取——from_env 触发 F7 的 .env 自动
    加载（CODEAUDIT_* setdefault 进 os.environ），顺序颠倒会使 .env 中的
    CODEAUDIT_DB_PATH 静默失效（实测 store 落默认路径）。
    """
    global _STORE
    if _STORE is None:
        work_root = str(Path(AuditConfig.from_env().work_root))
        db_path = os.environ.get("CODEAUDIT_DB_PATH") or str(Path(work_root) / "audits.db")
        # M-4：store 携带 work_root——prune/启动 sweep/DELETE 删行时同步回收
        # <work_root>/<audit_id>/ 工作目录与 uploads zip（磁盘泄漏修复）。
        _STORE = TaskStore(db_path, work_root=work_root)
    return _STORE


def _sev_str(severity: Any) -> str:
    return severity.value if isinstance(severity, Severity) else str(severity)


def _cat_str(category: Any) -> str:
    return category.value if isinstance(category, Category) else str(category)


def _require_done_report(audit_id: str) -> AuditReport:
    """按既有 404 语义取已完成任务的报告：任务不存在 / 任务未完成。"""
    store = _get_store()
    entry = store.get(audit_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{audit_id}")
    report = store.get_report(audit_id)
    if entry["status"] != "done" or report is None:
        raise HTTPException(status_code=404, detail=f"任务未完成：{entry['status']}")
    return report


def _ensure_pending_capacity() -> None:
    """F5 准入检查：非终态任务数达到上限时拒绝新建任务（429），不落任何表项或文件。

    W11 起计数走 store.count_active()——多 worker 下即全局非终态任务数（契约 §1.2）。
    M-5：上限每次调用时惰性解析（.env 运行期加载后即生效）。
    """
    max_pending = _get_max_pending()
    if _get_store().count_active() >= max_pending:
        raise HTTPException(
            status_code=429,
            detail=f"服务器并发审计数已达上限（{max_pending}），请稍后重试",
        )


class CreateAuditRequest(BaseModel):
    """POST /api/audits 请求体。"""

    source_path: str = Field(..., description="待审计项目路径或 zip")
    do_fix: bool = Field(False, description="是否生成修复补丁（Wave 2）")
    do_tests: bool = Field(False, description="是否生成单测（Wave 2）")


class ApplyPatchRequest(BaseModel):
    """POST /api/audits/{id}/patches/{n}/apply 请求体（可选，默认 dry-run）。"""

    yes: bool = Field(False, description="true=写入源码；false（默认）=dry-run 预览不落盘")


class RenameRequest(BaseModel):
    """POST /api/rename 请求体（W28-B safe-rename 符号重命名）。"""

    source_path: str = Field(..., description="待重命名扫描范围（单 .py 文件或目录，目录时递归）")
    old_name: str = Field(..., description="现名（须为合法 python 标识符且非关键字）")
    new_name: str = Field(..., description="新名（须为合法 python 标识符且非关键字，且 != old_name）")
    apply: bool = Field(
        False,
        description="true=写入源码；false（默认）=dry-run 预览不落盘（对应 CLI --yes 语义，避让布尔字段名 yes）",
    )
    language: str = Field(
        "python",
        description="目标语言（缺省 python，与 CLI --lang 同语义）；非 python 值由 plan_rename 校验拒绝为 400",
    )


def _wrapped_emitter(store: TaskStore, audit_id: str):
    """构造任务工作线程内的事件发射器：事件边界检查协作取消 + 事件落库。

    表项已被 DELETE 删除（或取消标志置位）时 is_cancelled 为 True → 抛
    CancelledError，由 _run_audit_sync 的 BaseException 兜底落终态
    （行已被删则 set_status 幽灵静默）——行消失即取消信号（契约 docs/16 §1.2）。
    """

    async def emitter(event: dict[str, Any]) -> None:
        if store.is_cancelled(audit_id):
            raise asyncio.CancelledError()
        store.append_event(audit_id, dict(event))

    return emitter


def _cleanup_if_ghost(store: TaskStore, audit_id: str) -> None:
    """M-4 执行体幽灵收尾：行已被 DELETE 删除（协作式取消）时，本线程持有的工作
    目录与上传 zip 由执行体在退出前回收——DELETE 对 queued/running 任务只删行不动
    文件（见 TaskStore.delete），磁盘回收责任在此闭环；行仍在（正常终态）时不动，
    终态目录交由 prune / DELETE（终态）时机回收。"""
    if store.get(audit_id) is None:
        store.cleanup_workdir(audit_id)


def _run_audit_sync(
    audit_id: str, config: AuditConfig, resume_stages: frozenset[str] | None = None
) -> None:
    """任务工作线程主体（W11 §1.2）：独立事件循环执行流水线，状态与产物落 store。

    run_audit 经 ``import server.app as _self`` 延迟取模块属性（而非闭包捕获模块级
    全局），保证测试在任务线程启动后 monkeypatch.setattr(server.app, "run_audit", ...)
    注入的假流水线同样生效。任何 BaseException（含 emitter 边界的 CancelledError）
    兜底落 failed——R4-9：任务表不残留 running 僵尸项；行已被 DELETE 删除时
    set_status 幽灵任务静默，不报错不回写。
    F6：入参 config 是创建请求时构造的原始对象（内存中带真实 api_key），
    绝不自 store 的 config_json 反序列化——落库值已脱敏为 "<redacted>"。
    M-4：幽灵收尾（行已删）时同步回收工作目录与上传 zip。
    P0-9：resume_stages 非 None 时按断点续跑形态调用 run_audit（audit_id=任务 ID +
    resume_stages=done）——audit_id 决定 task_root（<work_root>/<audit_id>），续跑
    必须复用原任务目录才能命中工作副本/索引/断点产物；None（普通创建）保持既有
    调用形状逐字节不变（不传 audit_id，报告内部 ID 与任务 ID 是两个体系）——
    假流水线替身多只声明 (config, emitter) 两参，续跑替身才需接受关键字。
    """
    import server.app as _self  # 运行期取模块属性，供测试注入替身（monkeypatch 生效）

    store = _get_store()
    emitter = _wrapped_emitter(store, audit_id)
    try:
        if resume_stages is None:
            report = asyncio.run(_self.run_audit(config, emitter))
        else:
            report = asyncio.run(
                _self.run_audit(config, emitter, audit_id=audit_id, resume_stages=resume_stages)
            )
    except BaseException as exc:  # noqa: BLE001 —— CancelledError 等异常路径也必须落终态
        store.set_status(audit_id, "failed", error=f"{type(exc).__name__}: {exc}")
        _cleanup_if_ghost(store, audit_id)
        return
    if report is not None:
        store.set_report(audit_id, report)
    store.set_status(audit_id, "done")
    _cleanup_if_ghost(store, audit_id)


async def _run_audit_task(
    audit_id: str, config: AuditConfig, resume_stages: frozenset[str] | None = None
) -> None:
    """后台执行审计任务（外层 task）：闸门排队 → 置 running → 线程执行。

    W11 §1.2：审计任务经 asyncio.to_thread 迁到独立线程（线程内独立事件循环，
    见 _run_audit_sync），服务事件循环不再被 CPU 密集段饿死。_RUN_GATE 语义保留
    （M-5 起经 _get_run_gate 惰性取用）：acquire 在 to_thread 之前于事件循环内
    await（超出的任务停留既有 queued 状态）；acquired 标志 + finally 严格配对
    release 防闸门泄漏——外层 task 被 cancel 时（含排队中/执行中被取消）release
    仍须执行。任务状态与产物的落库全部在 _run_audit_sync 内完成（DELETE 竞态由
    store 的幽灵静默语义兜底）。P0-9：resume_stages 透传给工作线程（断点续跑）。
    """
    acquired = False
    gate = _get_run_gate()  # acquire/release 必须同一信号量实例（M-5 惰性初始化）
    try:
        await gate.acquire()
        acquired = True
        _get_store().set_status(audit_id, "running")
        await asyncio.to_thread(_run_audit_sync, audit_id, config, resume_stages)
    finally:
        if acquired:
            gate.release()


def _launch_audit(
    audit_id: str, config: AuditConfig, resume_stages: frozenset[str] | None = None
) -> None:
    """拉起后台任务并登记本 worker 运行句柄（创建与 resume 端点共用，P0-9）。

    resume_stages 仅续跑路径非 None（断点续跑），普通创建为 None 走既有形态。
    """
    task = asyncio.create_task(_run_audit_task(audit_id, config, resume_stages))
    _RUNNING[audit_id] = task
    _TASKS.add(task)

    def _on_done(done_task: asyncio.Task) -> None:
        # R1-7：任务结束释放弱引用集与本 worker 运行句柄，防泄漏
        _TASKS.discard(done_task)
        if _RUNNING.get(audit_id) is done_task:
            _RUNNING.pop(audit_id, None)

    task.add_done_callback(_on_done)


def _start_audit(audit_id: str, config: AuditConfig) -> None:
    """建任务公共流程（POST /api/audits 与 /api/audits/upload 共用）。

    报告目录按任务隔离（R1-30）→ 任务行落 store → 容量淘汰（store.prune）→
    拉起后台任务（_launch_audit）并登记本 worker 运行句柄（_RUNNING）。
    """
    if not config.out_dir:
        config.out_dir = str(Path(config.work_root) / audit_id / "reports")
    store = _get_store()
    # F6（docs/17 §1.2，W12-A1）：config_json 落库前脱敏——api_key 以 "<redacted>"
    # 占位后再 asdict 序列化（db 中的值仅作审计痕迹）。
    # 运行时语义保证：任务执行（_run_audit_task → to_thread(_run_audit_sync)）用的
    # 是创建请求时构造的原始 config 对象（内存中带真实 Key，见下方 create_task 传参），
    # 全程不读回 db 的 config_json——因此 store 里任何值都取不到明文 Key。
    store.create(
        audit_id,
        created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        source_path=config.source_path,
        do_fix=config.do_fix,
        do_tests=config.do_tests,
        config_json=json.dumps(
            dataclasses.asdict(dataclasses.replace(config, api_key="<redacted>")),
            ensure_ascii=False,
        ),
    )
    store.prune(_AUDITS_MAX)
    _launch_audit(audit_id, config)


def _task_list_item(task: dict[str, Any]) -> dict[str, Any]:
    """任务列表元素（契约 v2.0 白名单字段，不泄漏 config_json 等内部列）。"""
    return {
        "audit_id": task["audit_id"],
        "status": task["status"],
        "created_at": task.get("created_at"),
        "source_path": task.get("source_path"),
        "do_fix": task.get("do_fix"),
        "do_tests": task.get("do_tests"),
        "error": task.get("error"),
    }


def create_app() -> FastAPI:
    """构建 FastAPI 应用（每个测试可独立创建实例，任务存储为模块级共享）。"""

    @contextlib.asynccontextmanager
    async def _lifespan(_app: FastAPI):
        # 启动 sweep（契约 docs/16 §1.1 重启语义）：遗留 queued/running → failed
        # （error=服务重启中断）；终态任务与报告重启后仍可查询与下载。
        # M-4：被 sweep 的任务同步回收工作目录与上传 zip（重启后无执行体持有）。
        # uvicorn 实例/import string 两种形态与 TestClient 上下文管理器都会执行 lifespan。
        # W15-A5：grace_seconds 接线见 _sweep_on_startup（卡B 契约，签名探测兼容）。
        _sweep_on_startup()
        # W15-A1：token 为空 = 鉴权关闭，启动时提醒本部署仅限本机访问。
        if not _get_security_config().api_token:
            _LOG.warning(
                "CODEAUDIT_API_TOKEN 未配置：/api/* 无鉴权，请确保服务仅绑定本机"
                "（127.0.0.1）访问，暴露到网络前必须配置 API 令牌"
            )
        yield
        # W15-A5：关停时关闭 store 连接（TaskStore.close 幂等——audit/taskstore.py
        # 以 _closed 标志守卫，重复调用安全）；任务工作线程与关库的良性收尾竞态
        # 以 try 兜底记 warning，不阻塞进程退出。
        try:
            _get_store().close()
        except Exception as exc:  # noqa: BLE001 —— 关停竞态不阻塞退出
            _LOG.warning("任务存储关停异常（忽略）：%s", exc)

    app = FastAPI(title="CodeAudit Agent", version=_AUDIT_VERSION, lifespan=_lifespan)

    # W15-A1：安全中间件先于 CORS 注册（后注册者位于 Starlette 中间件栈外层）——鉴权位于
    # CORS 内层，浏览器预检 OPTIONS 由 CORS 外层短路应答（预检请求不携带自定义凭据
    # 是 CORS 协议约束，不应被 401 拦截）；实际跨域请求经 CORS 补头后进入本中间件。
    @app.middleware("http")
    async def _security_middleware(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/"):
            cfg = _get_security_config()
            # 鉴权：token 非空时生效；GET /api/health 豁免（存活探针无凭据）。
            if cfg.api_token and not (path == "/api/health" and request.method == "GET"):
                if not _request_token_ok(request, cfg.api_token):
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "未授权：缺少或无效的 API 令牌"},
                    )
            # 限流：rate_limit_per_min > 0 时对写方法（POST/DELETE）按客户端 IP 滑动窗口记账。
            if cfg.rate_limit_per_min > 0 and request.method in ("POST", "DELETE"):
                client_ip = request.client.host if request.client else "unknown"
                if _rate_limit_exceeded(client_ip, cfg.rate_limit_per_min):
                    return JSONResponse(
                        status_code=429,
                        content={"detail": "请求过于频繁，请稍后重试"},
                    )
        return await call_next(request)

    # 契约 v2.0：仅放行开发态 Vite dev server 的两个源
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        """健康检查：版本号 + 任务表规模（active=非终态任务数，与 F5 准入同口径）。"""
        store = _get_store()
        active = store.count_active()
        return {
            "status": "ok",
            "version": _AUDIT_VERSION,
            "audits": {"active": active, "total": store.list(0, 0)[0]},
        }

    @app.get("/api/audits")
    async def list_audits(limit: str = "", offset: str = "") -> dict[str, Any]:
        """任务列表（持久化于 store）：按创建时间新→旧排序，limit/offset 分页。

        limit 缺省/0 表示不分页返回全部；total 始终为任务总数。
        """
        try:
            n_limit = int(limit) if limit.strip() else 0
            n_offset = int(offset) if offset.strip() else 0
        except ValueError:
            raise HTTPException(status_code=400, detail="limit / offset 必须为整数") from None
        if n_limit < 0:
            raise HTTPException(status_code=400, detail="limit 不能为负数")
        if n_offset < 0:
            raise HTTPException(status_code=400, detail="offset 不能为负数")

        total, items = _get_store().list(0, 0)
        page = items[n_offset : n_offset + n_limit] if n_limit else items[n_offset:]
        return {
            "total": total,
            "audits": [_task_list_item(task) for task in page],
        }

    @app.post("/api/audits")
    async def create_audit(req: CreateAuditRequest) -> dict[str, str]:
        if not req.source_path.strip():
            raise HTTPException(status_code=400, detail="source_path 不能为空")
        source = Path(req.source_path)
        # W15-A2：白名单先于存在性检查（越界 400，不泄露越界路径在本机的存在性）
        _ensure_source_allowed(source)
        if not source.exists():
            raise HTTPException(status_code=400, detail=f"源路径不存在：{req.source_path}")
        audit_id = new_audit_id()
        config = AuditConfig.from_env(
            source_path=str(source), do_fix=req.do_fix, do_tests=req.do_tests
        )
        _ensure_pending_capacity()
        _start_audit(audit_id, config)
        return {"audit_id": audit_id}

    @app.post("/api/audits/upload")
    async def upload_audit(
        file: Annotated[UploadFile, File()],
        do_fix: Annotated[bool, Form()] = False,
        do_tests: Annotated[bool, Form()] = False,
    ) -> dict[str, str]:
        """上传 zip 源码包建任务：流式落盘 <work_root>/uploads/<audit_id>.zip 后走既有创建流程。

        F2：按 256 KB 分块读取累计写入磁盘临时文件（.part），累计超
        _UPLOAD_MAX_BYTES 立即 413 并停止读取剩余 body——不再把整个文件
        缓冲进内存；成功路径 os.replace 到最终路径，落盘字节与请求体逐字节
        一致。F5：准入 429 在任何读盘/落盘之前判定。
        M-6：读流间隙存在大量 await——并发上传可能使容量在首次检查后越限，
        故读流完成后、真正建任务前再做一次权威校验（与快速路径同形的 429），
        拒绝时清掉已落盘 zip，保持零残留。
        """
        filename = (file.filename or "").lower()
        if not filename.endswith(".zip"):
            raise HTTPException(status_code=400, detail=f"仅支持 .zip 上传，收到：{file.filename}")
        _ensure_pending_capacity()
        audit_id = new_audit_id()
        work_root = AuditConfig.from_env().work_root
        zip_path = Path(work_root) / "uploads" / f"{audit_id}.zip"
        part_path = zip_path.with_name(f"{audit_id}.zip.part")
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        received = 0
        try:
            with part_path.open("wb") as sink:
                while True:
                    chunk = await file.read(_UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > _UPLOAD_MAX_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"上传文件过大：{received} 字节，上限 {_UPLOAD_MAX_BYTES} 字节",
                        )
                    sink.write(chunk)
            if not zipfile.is_zipfile(part_path):
                raise HTTPException(status_code=400, detail="上传内容不是合法的 zip 文件")
        except BaseException:
            part_path.unlink(missing_ok=True)  # 失败路径不留半截文件
            raise
        part_path.replace(zip_path)
        config = AuditConfig.from_env(
            source_path=str(zip_path), do_fix=do_fix, do_tests=do_tests
        )
        try:
            _ensure_pending_capacity()  # M-6 权威二次校验（读流后、建任务前；与建任务间无 await）
        except BaseException:
            zip_path.unlink(missing_ok=True)  # 拒绝后零残留（.part 已 replace 成正式路径）
            raise
        _start_audit(audit_id, config)
        return {"audit_id": audit_id}

    @app.get("/api/audits/{audit_id}")
    async def get_audit(audit_id: str) -> dict[str, Any]:
        store = _get_store()
        entry = store.get(audit_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"任务不存在：{audit_id}")
        payload: dict[str, Any] = {
            "audit_id": audit_id,
            "status": entry["status"],
            "error": entry["error"],
            "created_at": entry.get("created_at"),
            "source_path": entry.get("source_path"),
            "do_fix": entry.get("do_fix"),
            "do_tests": entry.get("do_tests"),
        }
        report = store.get_report(audit_id)
        if entry["status"] == "done" and report is not None:
            payload["report"] = report.to_dict()
        return payload

    @app.delete("/api/audits/{audit_id}")
    async def delete_audit(audit_id: str) -> Response:
        """删除任务：未知 404；其余（终态与非终态）直接物理删除任务行与全部事件（204）。

        W11 协作式取消（契约 docs/16 §1.2）：表行消失即取消信号——执行线程在下一个
        emitter 事件边界发现 is_cancelled（表项已删除）即抛 CancelledError 兜底落
        failed（行已被删则幽灵静默）；不再 cancel 外层 task（cancel 杀不掉线程反而
        破坏语义）。SSE 轮询端因 entry is None 自然收流。
        M-4 磁盘回收：终态任务的删除由 store.delete 同步回收工作目录与上传 zip；
        非终态（queued/running）为协作取消语义——此处只删行不删文件，工作目录由
        执行体幽灵收尾时回收（_cleanup_if_ghost），绝不 rmtree 运行中任务的工作副本。
        """
        if not _get_store().delete(audit_id):
            raise HTTPException(status_code=404, detail=f"任务不存在：{audit_id}")
        return Response(status_code=204)

    @app.post("/api/audits/{audit_id}/resume")
    async def resume_audit(audit_id: str) -> dict[str, Any]:
        """续跑被中断的审计任务（P0-9，守卫与 CLI `codeaudit resume` 单源同语义）。

        - 可续跑态：interrupted；failed 且带阶段进度（stage_done 非空）；running 且
          带阶段进度（SIGKILL 级硬杀滞留态）时先按 grace 窗口
          （CODEAUDIT_SWEEP_GRACE_SEC，缺省 30s）sweep 自愈，重读后已变
          interrupted 即续跑、仍 running（窗口内有活动，疑似真有执行体在跑）→ 409；
        - 其余状态不可续跑 → 409（文案与 CLI 的「不可续跑」一致）；任务不存在 → 404；
        - 守卫判定复用 cli._resume_guard_action 纯函数；config 从任务行 config_json
          重建（cli._rebuild_config_from_task_json，脱敏 api_key 剔除、按环境补全）；
        - F6-R1：重建出的 config.source_path 复经 _ensure_source_allowed 白名单校验
          （空/缺失 400，越界 400），先校验后 mark_resuming——校验失败不改任务状态；
        - mark_resuming 复位 → resume_stage_done 判定可跳过阶段（宁缺勿跳）→
          后台执行 run_audit(resume_stages=done)：闸门/线程/事件流机制照抄创建端点
          （_launch_audit 共用），成功后 set_report + done；
        - 响应：任务行白名单字段（与任务列表同形态，不泄漏 config_json）。
        """
        store = _get_store()
        entry = store.get(audit_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"任务不存在：{audit_id}")
        status = str(entry.get("status") or "")
        progress = store.get_stage_done(audit_id)
        action = _resume_guard_action(status, progress)
        if action == "sweep":
            # SIGKILL 滞留自愈：先按窗口清扫（只清 updated_at 早于 now-grace 的行）
            # 再重读定夺；仍 running = 窗口内有 updated_at 活动，不冒险双执行体续跑。
            store.sweep_interrupted(grace_seconds=_resume_sweep_grace_seconds())
            entry = store.get(audit_id)
            status = str((entry or {}).get("status") or "")
            action = _resume_guard_action(status, progress)
            if action == "sweep":
                raise HTTPException(
                    status_code=409,
                    detail=f"任务 {audit_id} 仍在运行或刚有活动，请稍后重试",
                )
        if action == "reject":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"任务 {audit_id} 状态为 {status}，不可续跑"
                    "（仅 interrupted，或带阶段进度的 failed 支持 resume）。"
                ),
            )
        config = _rebuild_config_from_task_json(str((entry or {}).get("config_json") or ""))
        # F6-R1（第六轮审计）：resume 与创建同受 SOURCE_ROOTS 白名单约束——config_json
        # 落库后可经迁移/篡改与创建时不同，重建出的 config 必须复验，堵住
        # 「创建端点 400、resume 端点 200」的绕过路径。空 source_path（config_json
        # 空/损坏时 from_env 缺省 ""）同样直接 400：Path('') resolve 后是进程 cwd，
        # 交由白名单判定会产生误导性报错甚至放行 cwd；无 source 的任务 run_audit
        # 也必然无法执行。顺序关键：先校验后 mark_resuming，校验失败不改任务状态。
        if not str(config.source_path or "").strip():
            raise HTTPException(
                status_code=400,
                detail=f"任务 {audit_id} 的任务记录缺少 source_path，无法续跑",
            )
        _ensure_source_allowed(Path(config.source_path))
        if not store.mark_resuming(audit_id):
            raise HTTPException(
                status_code=409,
                detail=f"任务 {audit_id} 状态复位失败（状态已变化，请查列表后重试）。",
            )
        done = resume_stage_done(store, audit_id, config)
        if not config.out_dir:  # 报告目录按任务隔离（R1-30，与 _start_audit 同口径）
            config.out_dir = str(Path(config.work_root) / audit_id / "reports")
        _launch_audit(audit_id, config, resume_stages=done)
        row = store.get(audit_id)
        if row is None:  # pragma: no cover —— 与 DELETE/prune 的极端竞态，如实 404
            raise HTTPException(status_code=404, detail=f"任务不存在：{audit_id}")
        return _task_list_item(row)

    @app.get("/api/audits/{audit_id}/events")
    async def audit_events(audit_id: str) -> EventSourceResponse:
        if not _get_store().exists(audit_id):
            raise HTTPException(status_code=404, detail=f"任务不存在：{audit_id}")
        # W15-A4：并发事件流超限直接 429，不做排队（404 之后判定——任务存在才有流可言）。
        if not _acquire_sse_slot():
            raise HTTPException(
                status_code=429,
                detail=f"SSE 事件流并发数已达上限（{_MAX_SSE_STREAMS}），请稍后重试",
            )

        async def event_stream():
            try:
                store = _get_store()
                idx = 0
                while True:
                    # 回放历史事件 / 跟随新事件（seq 游标读 store；0.05s 轮询，测试友好）
                    for seq, event in store.get_events(audit_id, after_seq=idx):
                        yield {"data": json.dumps(event, ensure_ascii=False)}
                        idx = seq
                    entry = store.get(audit_id)
                    if entry is None:  # 表项被删除或容量淘汰 → 收流
                        return
                    if entry["status"] in _TERMINAL_STATUSES:
                        yield {"data": json.dumps({"type": "done"}, ensure_ascii=False)}
                        return
                    await asyncio.sleep(0.05)
            finally:
                # W15-A4：名额必须在连接结束的所有路径上归还（正常收流 / 客户端
                # 断开触发 generator aclose / 异常），否则计数泄漏会耗尽上限。
                _release_sse_slot()

        return EventSourceResponse(event_stream())

    @app.get("/api/audits/{audit_id}/report")
    async def get_report(audit_id: str, format: str = "json"):  # noqa: A002
        report = _require_done_report(audit_id)
        fmt = format.lower()
        if fmt == "json":
            return JSONResponse(content=report.to_dict())
        if fmt == "md":
            return PlainTextResponse(
                render_markdown(report), media_type="text/markdown; charset=utf-8"
            )
        if fmt == "html":
            return HTMLResponse(render_html(report))
        raise HTTPException(status_code=400, detail=f"不支持的格式：{format}（可选 md/html/json）")

    @app.get("/api/audits/{audit_id}/reports")
    async def list_report_versions(audit_id: str) -> dict[str, Any]:
        """报告历史版本摘要列表（W27-B 结果版本化）：seq/created_at/health_score/
        issue_count（不含 report_json 全文），seq 升序；当前报告即历史最大 seq。
        任务不存在 404（任务存在但尚无任何版本返回空列表，非 404）；鉴权跟随
        /api/* 既有中间件，形态对齐 list_issues / list_patches（total + 列表）。
        """
        store = _get_store()
        if not store.exists(audit_id):
            raise HTTPException(status_code=404, detail=f"任务不存在：{audit_id}")
        versions = store.list_reports(audit_id)
        return {"total": len(versions), "reports": versions}

    @app.get("/api/audits/{audit_id}/reports/{seq}")
    async def get_report_version(audit_id: str, seq: int) -> JSONResponse:
        """取指定历史版本（seq 从 1 起）的完整报告 JSON（W27-B）。

        404 = 任务不存在或该版本不存在；seq 非整数由 FastAPI 路径校验回 422
        （与 patches/{patch_index} 的 int 路径参数既有约定一致）。不要求任务处于
        done 态：resume/重跑推进中，既有历史版本依然只读可见。
        """
        store = _get_store()
        if not store.exists(audit_id):
            raise HTTPException(status_code=404, detail=f"任务不存在：{audit_id}")
        report = store.get_report(audit_id, seq=seq)
        if report is None:
            raise HTTPException(status_code=404, detail=f"报告版本不存在：seq={seq}")
        return JSONResponse(content=report)

    @app.get("/api/audits/{audit_id}/issues")
    async def list_issues(
        audit_id: str,
        severity: str = "",
        category: str = "",
        limit: str = "",
        offset: str = "",
    ) -> dict[str, Any]:
        """分页问题列表：支持 severity / category 过滤（供仪表盘问题表）。"""
        report = _require_done_report(audit_id)

        sev = severity.strip().lower()
        if sev and sev not in _SEVERITY_VALUES:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的 severity：{severity}（可选 {'/'.join(sorted(_SEVERITY_VALUES))}）",
            )
        cat = category.strip().lower()
        if cat and cat not in _CATEGORY_VALUES:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的 category：{category}（可选 {'/'.join(sorted(_CATEGORY_VALUES))}）",
            )
        try:
            n_limit = int(limit) if limit.strip() else _DEFAULT_ISSUES_LIMIT
            n_offset = int(offset) if offset.strip() else 0
        except ValueError:
            raise HTTPException(status_code=400, detail="limit / offset 必须为整数") from None
        if n_limit <= 0:
            raise HTTPException(status_code=400, detail="limit 必须为正整数")
        if n_offset < 0:
            raise HTTPException(status_code=400, detail="offset 不能为负数")

        filtered = [
            i
            for i in report.issues
            if (not sev or _sev_str(i.severity) == sev)
            and (not cat or _cat_str(i.category) == cat)
        ]
        page = filtered[n_offset : n_offset + n_limit]
        return {
            "total": len(filtered),
            "offset": n_offset,
            "limit": n_limit,
            "issues": [i.to_dict() for i in page],
        }

    @app.get("/api/audits/{audit_id}/patches")
    async def list_patches(audit_id: str) -> dict[str, Any]:
        """修复补丁列表：含 diff 原文与应用状态。"""
        report = _require_done_report(audit_id)
        return {
            "total": len(report.patches),
            "patches": [p.to_dict() for p in report.patches],
        }

    @app.post("/api/audits/{audit_id}/patches/{patch_index}/apply")
    async def apply_patch(
        audit_id: str, patch_index: int, req: ApplyPatchRequest | None = None
    ) -> dict[str, Any]:
        """应用单个补丁回源码（P0-2，语义与 CLI apply 一致）。

        - body 可选 {"yes": bool}，缺省 false = dry-run 预览（不落盘）；
        - 目标源码目录取任务记录的 source_path（须为目录，zip 任务返回 400）；
        - 校验：目标文件 sha256 与审计时不一致 / 老补丁无指纹 → 拒绝该补丁且
          整体不落盘（all-or-nothing），拒绝原因在 rejected[].reason 中；
        - 补丁序号（0 起）越界返回 404。
        """
        report = _require_done_report(audit_id)
        if patch_index < 0 or patch_index >= len(report.patches):
            raise HTTPException(
                status_code=404,
                detail=(
                    f"补丁序号越界：{patch_index}"
                    f"（有效范围 0-{len(report.patches) - 1}，共 {len(report.patches)} 个补丁）"
                    if report.patches
                    else f"任务 {audit_id} 没有补丁"
                ),
            )
        entry = _get_store().get(audit_id)
        source = Path(str((entry or {}).get("source_path") or ""))
        if not source.is_dir():
            raise HTTPException(
                status_code=400,
                detail=(
                    f"任务源路径不是本机目录（{source or '空'}），无法回写源码；"
                    "zip 上传的审计请改用 CLI：codeaudit apply <id> --workdir <解包目录>"
                ),
            )
        yes = bool(req.yes) if req is not None else False

        from audit.fix.applyer import apply_patches

        def _apply() -> dict[str, Any]:
            result = apply_patches(report.patches, source, yes=yes, only_index=patch_index)
            return result.to_dict()

        try:
            return await asyncio.to_thread(_apply)
        except Exception as exc:  # noqa: BLE001 —— 写失败已整体回滚，向调用方如实报错
            raise HTTPException(
                status_code=500, detail=f"补丁应用失败（未保留任何变更）：{type(exc).__name__}: {exc}"
            ) from exc

    @app.post("/api/rename")
    async def rename_symbol(req: RenameRequest) -> dict[str, Any]:
        """safe-rename 符号重命名（W28-B）：dry-run 预览 / 落盘，复用 audit.refactor.rename。

        - 治理链全部前置（F6-R1 教训，第一版即带全治理）：
          1. 鉴权/限流：跟随 /api/* 既有中间件（token 非空 401、POST 滑动窗口 429），
             本端点零额外代码（中间件按 /api/ 前缀 + 请求方法全覆盖）；
          2. SOURCE_ROOTS 白名单：越界 400（与创建/resume 同款 _ensure_source_allowed），
             判定先于存在性检查——不向调用方泄露越界路径在本机的存在性；
        - source_path 空 / 不存在 → 400；plan 阶段 errors 非空（非法名 / old==new /
          多定义点 / 非 UTF-8 或解析失败）→ 400 中文 detail，计划阶段即拒绝绝不落盘
          （apply=true 亦然，rename 模块 plan.ok=False 时不产出任何补丁）；
        - language 缺省 "python"（零变化，与 CLI --lang 同语义）透传 plan_rename；
          非法值（如 "go"）同样进 plan.errors → 400 中文 detail（含「暂不支持」），
          端点零新增分支（复用既有 plan.ok=False 拒绝路径）；
        - apply=true 且复检失败（目标内容与计划时不一致 / 替换后 AST 复检失败）→
          409 Conflict + 逐文件原因：all-or-nothing 已保证零落盘，409 = 请求前提
          （基于计划时的文件内容）与资源当前状态冲突的准确语义，区别于 500 的
          服务端故障（选型说明，对应任务卡「500 或 409 二选一」）；
        - 响应：{"ok", "applied", "files", "replace_points", "diffs", "errors"}；
          apply=false（缺省）= dry-run 只返回 diff 预览不落盘（applied=false）。
        """
        if not req.source_path.strip():
            raise HTTPException(status_code=400, detail="source_path 不能为空")
        source = Path(req.source_path)
        # W28-B：白名单先于存在性检查（与创建端点 create_audit 同口径、同顺序）
        _ensure_source_allowed(source)
        if not source.exists():
            raise HTTPException(status_code=400, detail=f"源路径不存在：{req.source_path}")
        # tree-sitter 解析与落盘为阻塞操作，照 apply_patch 先例挪线程池（不饿死事件循环）
        plan = await asyncio.to_thread(
            plan_rename, str(source), req.old_name, req.new_name, language=req.language
        )
        if not plan.ok:
            raise HTTPException(
                status_code=400,
                detail="重命名计划阶段即拒绝（未做任何修改）：" + "；".join(plan.errors),
            )
        result = await asyncio.to_thread(apply_rename, plan, dry_run=not req.apply)
        if not result.ok:
            raise HTTPException(
                status_code=409,
                detail="重命名应用失败（all-or-nothing，未写入任何文件）：" + "；".join(result.errors),
            )
        return {
            "ok": True,
            "applied": result.applied,
            "files": [p.file for p in plan.patches],
            "replace_points": plan.total_replace_points,
            "diffs": dict(result.diffs),
            "errors": [],
        }

    @app.get("/api/audits/{audit_id}/summary")
    async def get_summary(audit_id: str) -> dict[str, Any]:
        """任务摘要（仪表盘头）：健康分 / 严重度计数 / 耗时 / token 统计。

        audit_id 以任务表键（URL 中的 ID）为准——流水线报告内部的 audit_id
        与服务端任务 ID 是两个体系，前端所有 API 链接都按任务 ID 组装，
        此处若返回报告内部 ID 会导致报告下载链接 404。
        """
        report = _require_done_report(audit_id)
        stats = report.stats
        return {
            "audit_id": audit_id,
            "project_name": report.project_name,
            "health_score": report.health_score,
            "summary": dict(report.summary),
            "total_issues": sum(report.summary.values()) if report.summary else len(report.issues),
            "loc": report.loc,
            "duration_sec": stats.duration_sec,
            "tokens": {
                "llm_calls": stats.llm_calls,
                "prompt_tokens": stats.prompt_tokens,
                "completion_tokens": stats.completion_tokens,
                "cache_hits": stats.cache_hits,
                "cache_misses": stats.cache_misses,
            },
        }

    @app.get("/api/audits/{audit_id}/understand")
    async def get_understand(audit_id: str) -> dict[str, Any]:
        """架构理解卡片（契约 v1.7）：无架构产物时 architecture 为 null。"""
        report = _require_done_report(audit_id)
        return {
            "architecture": report.architecture.to_dict() if report.architecture else None
        }

    @app.get("/api/audits/{audit_id}/refactors")
    async def list_refactors(audit_id: str) -> dict[str, Any]:
        """重构方案列表（契约 v1.7 字段：id/title/target/kind/rationale/steps/
        benefits/related_issues/source/confidence）。"""
        report = _require_done_report(audit_id)
        return {
            "total": len(report.refactor_proposals),
            "proposals": [p.to_dict() for p in report.refactor_proposals],
        }

    @app.get("/")
    async def index() -> FileResponse:
        """根路径：SPA 构建产物优先，缺失则回退单文件演示页。"""
        spa_index = _SPA_DIST / "index.html"
        if spa_index.is_file():
            return FileResponse(spa_index, media_type="text/html")
        return FileResponse(_WEB_DIR / "index.html", media_type="text/html")

    # SPA 静态资源挂载（须先于 catch-all 注册，否则 /assets/* 会被兜底路由吞掉）
    if (_SPA_DIST / "assets").is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=_SPA_DIST / "assets"),
            name="spa-assets",
        )

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str) -> FileResponse:
        """catch-all：API 前缀 404 → dist 静态文件 → dist/index.html（SPA 客户端路由
        兜底）→ 单文件演示页。注册顺序在所有 API 路由之后。"""
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        dist = _SPA_DIST
        if dist.is_dir():
            candidate = (dist / full_path).resolve()
            try:
                candidate.relative_to(dist.resolve())  # 防目录穿越
            except ValueError:
                raise HTTPException(status_code=404, detail="Not Found") from None
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            spa_index = dist / "index.html"
            if spa_index.is_file():
                return FileResponse(spa_index, media_type="text/html")
        return FileResponse(_WEB_DIR / "index.html", media_type="text/html")

    return app


app = create_app()
