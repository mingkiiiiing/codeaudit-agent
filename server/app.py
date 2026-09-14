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
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
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
from audit.orchestrator.pipeline import run_audit  # noqa: F401 —— 模块级引用，供测试注入替身
from audit.report.render import render_html, render_markdown
from audit.taskstore import TaskStore
from audit.utils import new_audit_id

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


# F5 准入控制上限（模块级常量，便于测试 monkeypatch；运行时按模块全局读取）：
# - 同时运行中的任务上限（后台任务经 _RUN_GATE 排队，超出的停留既有 queued 状态；
#   per-worker 口径，多 worker 下全局并发上限 = workers × 该值，契约 docs/16 §1.2）；
# - 非终态（queued+running）任务总数上限，达到后 POST 建任务返回 429
#   （W11 起经 store 计数，多 worker 下即全局准确）。
_MAX_RUNNING_AUDITS = _int_from_env("CODEAUDIT_MAX_RUNNING", 4, minimum=1)
_MAX_PENDING_AUDITS = _int_from_env("CODEAUDIT_MAX_PENDING", 20, minimum=_MAX_RUNNING_AUDITS)

# 运行闸门（模块级全局，与 _TASKS/_RUNNING 同层；create_app 每次建实例但闸门模块级共享，
# gate 亦同）。容量在创建时固化，测试排队语义可直接 monkeypatch 替换为新 Semaphore。
_RUN_GATE = asyncio.Semaphore(_MAX_RUNNING_AUDITS)

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
        _STORE = TaskStore(db_path)
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
    """
    if _get_store().count_active() >= _MAX_PENDING_AUDITS:
        raise HTTPException(
            status_code=429,
            detail=f"服务器并发审计数已达上限（{_MAX_PENDING_AUDITS}），请稍后重试",
        )


class CreateAuditRequest(BaseModel):
    """POST /api/audits 请求体。"""

    source_path: str = Field(..., description="待审计项目路径或 zip")
    do_fix: bool = Field(False, description="是否生成修复补丁（Wave 2）")
    do_tests: bool = Field(False, description="是否生成单测（Wave 2）")


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


def _run_audit_sync(audit_id: str, config: AuditConfig) -> None:
    """任务工作线程主体（W11 §1.2）：独立事件循环执行流水线，状态与产物落 store。

    run_audit 经 ``import server.app as _self`` 延迟取模块属性（而非闭包捕获模块级
    全局），保证测试在任务线程启动后 monkeypatch.setattr(server.app, "run_audit", ...)
    注入的假流水线同样生效。任何 BaseException（含 emitter 边界的 CancelledError）
    兜底落 failed——R4-9：任务表不残留 running 僵尸项；行已被 DELETE 删除时
    set_status 幽灵任务静默，不报错不回写。
    F6：入参 config 是创建请求时构造的原始对象（内存中带真实 api_key），
    绝不自 store 的 config_json 反序列化——落库值已脱敏为 "<redacted>"。
    """
    import server.app as _self  # 运行期取模块属性，供测试注入替身（monkeypatch 生效）

    store = _get_store()
    try:
        report = asyncio.run(_self.run_audit(config, _wrapped_emitter(store, audit_id)))
    except BaseException as exc:  # noqa: BLE001 —— CancelledError 等异常路径也必须落终态
        store.set_status(audit_id, "failed", error=f"{type(exc).__name__}: {exc}")
        return
    if report is not None:
        store.set_report(audit_id, report)
    store.set_status(audit_id, "done")


async def _run_audit_task(audit_id: str, config: AuditConfig) -> None:
    """后台执行审计任务（外层 task）：闸门排队 → 置 running → 线程执行。

    W11 §1.2：审计任务经 asyncio.to_thread 迁到独立线程（线程内独立事件循环，
    见 _run_audit_sync），服务事件循环不再被 CPU 密集段饿死。_RUN_GATE 语义保留：
    acquire 在 to_thread 之前于事件循环内 await（超出的任务停留既有 queued 状态）；
    acquired 标志 + finally 严格配对 release 防闸门泄漏——外层 task 被 cancel 时
    （含排队中/执行中被取消）release 仍须执行。任务状态与产物的落库全部在
    _run_audit_sync 内完成（DELETE 竞态由 store 的幽灵静默语义兜底）。
    """
    acquired = False
    try:
        await _RUN_GATE.acquire()
        acquired = True
        _get_store().set_status(audit_id, "running")
        await asyncio.to_thread(_run_audit_sync, audit_id, config)
    finally:
        if acquired:
            _RUN_GATE.release()


def _start_audit(audit_id: str, config: AuditConfig) -> None:
    """建任务公共流程（POST /api/audits 与 /api/audits/upload 共用）。

    报告目录按任务隔离（R1-30）→ 任务行落 store → 容量淘汰（store.prune）→
    拉起后台任务并登记本 worker 运行句柄（_RUNNING）。
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
    task = asyncio.create_task(_run_audit_task(audit_id, config))
    _RUNNING[audit_id] = task
    _TASKS.add(task)

    def _on_done(done_task: asyncio.Task) -> None:
        # R1-7：任务结束释放弱引用集与本 worker 运行句柄，防泄漏
        _TASKS.discard(done_task)
        if _RUNNING.get(audit_id) is done_task:
            _RUNNING.pop(audit_id, None)

    task.add_done_callback(_on_done)


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
        # uvicorn 实例/import string 两种形态与 TestClient 上下文管理器都会执行 lifespan。
        _get_store().sweep_interrupted()
        yield

    app = FastAPI(title="CodeAudit Agent", version=_AUDIT_VERSION, lifespan=_lifespan)

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
        """
        if not _get_store().delete(audit_id):
            raise HTTPException(status_code=404, detail=f"任务不存在：{audit_id}")
        return Response(status_code=204)

    @app.get("/api/audits/{audit_id}/events")
    async def audit_events(audit_id: str) -> EventSourceResponse:
        if not _get_store().exists(audit_id):
            raise HTTPException(status_code=404, detail=f"任务不存在：{audit_id}")

        async def event_stream():
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
