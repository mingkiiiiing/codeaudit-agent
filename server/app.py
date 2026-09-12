"""FastAPI 服务：创建/查询审计任务、SSE 事件流、报告下载、演示页。

内存任务表 AUDITS: dict[audit_id -> {status, report, events, error}]。
后台任务通过 asyncio.create_task 执行 audit.orchestrator.pipeline.run_audit；
模块级引用 run_audit 便于测试注入假流水线（monkeypatch.setattr(server.app, "run_audit", ...)）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
)
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from audit.config import AuditConfig
from audit.models import AuditReport, Category, Severity
from audit.orchestrator.pipeline import run_audit  # noqa: F401 —— 模块级引用，供测试注入替身
from audit.report.render import render_html, render_markdown
from audit.utils import new_audit_id

# 项目根目录（web/ 静态演示页所在）
_ROOT = Path(__file__).resolve().parent.parent
_WEB_DIR = _ROOT / "web"

# 内存任务表：{audit_id: {"status": queued|running|done|failed, "report": AuditReport|None,
#                         "events": list[dict], "error": str|None, "config": AuditConfig}}
AUDITS: dict[str, dict[str, Any]] = {}

_TERMINAL_STATUSES = ("done", "failed")

# 任务表上限（R1-6）：超过时在新建任务后按插入序淘汰最旧的终态项（FIFO 容量淘汰）
_AUDITS_MAX = 50

# 后台任务引用（R1-7）：create_task 只返回弱引用，必须由本集合持有，
# 任务结束的 done callback 中 discard，防止被 GC 中途回收。
_TASKS: set[asyncio.Task] = set()

# 合法过滤值（与 audit.models 枚举保持一致）
_SEVERITY_VALUES = {s.value for s in Severity}
_CATEGORY_VALUES = {c.value for c in Category}
_DEFAULT_ISSUES_LIMIT = 50


def _sev_str(severity: Any) -> str:
    return severity.value if isinstance(severity, Severity) else str(severity)


def _cat_str(category: Any) -> str:
    return category.value if isinstance(category, Category) else str(category)


def _require_done_report(audit_id: str) -> tuple[dict[str, Any], AuditReport]:
    """按既有 404 语义取已完成任务的报告：任务不存在 / 任务未完成。"""
    entry = AUDITS.get(audit_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{audit_id}")
    report: AuditReport | None = entry["report"]
    if entry["status"] != "done" or report is None:
        raise HTTPException(status_code=404, detail=f"任务未完成：{entry['status']}")
    return entry, report


class CreateAuditRequest(BaseModel):
    """POST /api/audits 请求体。"""

    source_path: str = Field(..., description="待审计项目路径或 zip")
    do_fix: bool = Field(False, description="是否生成修复补丁（Wave 2）")
    do_tests: bool = Field(False, description="是否生成单测（Wave 2）")


def _emitter_for(events: list[dict[str, Any]]):
    """构造把事件 append 进列表的 EventEmitter。"""

    async def emitter(event: dict[str, Any]) -> None:
        events.append(dict(event))

    return emitter


async def _run_audit_task(audit_id: str, config: AuditConfig) -> None:
    """后台执行审计任务：更新 AUDITS[audit_id] 的状态与产物。

    R4-9：finally 兜底保证任何异常路径（含 asyncio.CancelledError 等
    BaseException）都落终态 failed，任务表不残留 running 僵尸项；
    取消异常记录后原样向外传播（保持取消语义）。
    """
    entry = AUDITS[audit_id]
    entry["status"] = "running"
    try:
        report = await run_audit(config, _emitter_for(entry["events"]))
        entry["report"] = report
        entry["status"] = "done"
    except BaseException as exc:  # noqa: BLE001 —— CancelledError 等异常路径也必须落终态
        entry["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if entry["status"] not in _TERMINAL_STATUSES:
            entry["status"] = "failed"


def _prune_audits() -> None:
    """任务表终态清理（R1-6）：超过上限时按插入序淘汰最旧的 done/failed 项。

    只淘汰终态条目；running/queued 任务不受影响。淘汰后 SSE 轮询端
    （event_stream 中 entry is None → return）自然收流。
    """
    if len(AUDITS) <= _AUDITS_MAX:
        return
    overflow = len(AUDITS) - _AUDITS_MAX
    evicted = 0
    for audit_id, entry in list(AUDITS.items()):
        if evicted >= overflow:
            break
        if entry.get("status") in _TERMINAL_STATUSES:
            AUDITS.pop(audit_id, None)
            evicted += 1


def create_app() -> FastAPI:
    """构建 FastAPI 应用（每个测试可独立创建实例，任务表为模块级共享）。"""
    app = FastAPI(title="CodeAudit Agent", version="0.1.0")

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
        if not config.out_dir:
            # R1-30：报告目录按任务隔离，避免多任务相互覆盖 report.json
            config.out_dir = str(Path(config.work_root) / audit_id / "reports")
        AUDITS[audit_id] = {
            "status": "queued",
            "report": None,
            "events": [],
            "error": None,
            "config": config,
        }
        _prune_audits()
        task = asyncio.create_task(_run_audit_task(audit_id, config))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
        return {"audit_id": audit_id}

    @app.get("/api/audits/{audit_id}")
    async def get_audit(audit_id: str) -> dict[str, Any]:
        entry = AUDITS.get(audit_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"任务不存在：{audit_id}")
        payload: dict[str, Any] = {
            "audit_id": audit_id,
            "status": entry["status"],
            "error": entry["error"],
        }
        if entry["status"] == "done" and entry["report"] is not None:
            payload["report"] = entry["report"].to_dict()
        return payload

    @app.get("/api/audits/{audit_id}/events")
    async def audit_events(audit_id: str) -> EventSourceResponse:
        if audit_id not in AUDITS:
            raise HTTPException(status_code=404, detail=f"任务不存在：{audit_id}")

        async def event_stream():
            idx = 0
            while True:
                entry = AUDITS.get(audit_id)
                if entry is None:  # 表项被清理
                    return
                events = entry["events"]
                # 回放历史事件 / 跟随新事件（0.05s 轮询，测试友好）
                while idx < len(events):
                    yield {"data": json.dumps(events[idx], ensure_ascii=False)}
                    idx += 1
                if entry["status"] in _TERMINAL_STATUSES:
                    yield {"data": json.dumps({"type": "done"}, ensure_ascii=False)}
                    return
                await asyncio.sleep(0.05)

        return EventSourceResponse(event_stream())

    @app.get("/api/audits/{audit_id}/report")
    async def get_report(audit_id: str, format: str = "json"):  # noqa: A002
        entry = AUDITS.get(audit_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"任务不存在：{audit_id}")
        report: AuditReport | None = entry["report"]
        if entry["status"] != "done" or report is None:
            raise HTTPException(status_code=404, detail=f"任务未完成：{entry['status']}")
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
        _, report = _require_done_report(audit_id)

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
        _, report = _require_done_report(audit_id)
        return {
            "total": len(report.patches),
            "patches": [p.to_dict() for p in report.patches],
        }

    @app.get("/api/audits/{audit_id}/summary")
    async def get_summary(audit_id: str) -> dict[str, Any]:
        """任务摘要（仪表盘头）：健康分 / 严重度计数 / 耗时 / token 统计。"""
        _, report = _require_done_report(audit_id)
        stats = report.stats
        return {
            "audit_id": report.audit_id,
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

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_WEB_DIR / "index.html", media_type="text/html")

    return app


app = create_app()
