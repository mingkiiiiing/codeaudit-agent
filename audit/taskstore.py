"""任务持久化存储层（W11-A1，契约 docs/16 §1.1）：SQLite（WAL）承载审计任务表与事件流。

设计要点（实现者必须遵守）：
- 标准库 sqlite3，零第三方依赖；WAL 模式（并发读 + 单写）；
- check_same_thread=False：server 事件循环线程与任务工作线程（W11 §1.2 to_thread）
  并发访问同一实例，所有写操作经内部 threading.Lock 串行化；
- 行结构：
    audits(audit_id TEXT PRIMARY KEY, status TEXT, error TEXT, created_at TEXT,
           source_path TEXT, do_fix INTEGER, do_tests INTEGER,
           config_json TEXT, report_json TEXT)
    events(audit_id TEXT, seq INTEGER, event_json TEXT,
           PRIMARY KEY (audit_id, seq))
- 状态机：queued | running | done | failed（与契约 v2 一致，无新状态）；
- 本模块只管存储：不启动线程、不触网、不解析 config_json / report_json 之外的对象图
  （report 用 audit.models.AuditReport.to_dict()/from_dict() 序列化）；
- 方法均为同步阻塞（调用方负责放入合适的执行上下文；store 内部锁保证线程安全）。

本文件为 W11-A5 集成人出具的接口骨架：签名与语义即契约，W11-A1 填实现，
W11-A2 按签名消费（单测用自建内存假 store，不依赖本实现）。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from audit.models import AuditReport

_TERMINAL = ("done", "failed")


class TaskStore:
    """SQLite 任务存储。一个进程一个实例（内部连接线程安全）。"""

    def __init__(self, db_path: Path | str) -> None:
        """打开（不存在则创建）数据库，建表，启用 WAL。"""
        self._db_path = Path(db_path)
        self._lock = threading.Lock()
        raise NotImplementedError("W11-A1")

    # ---------------------------------------------------------------- 任务 CRUD
    def create(
        self,
        audit_id: str,
        *,
        status: str = "queued",
        error: str | None = None,
        created_at: str,
        source_path: str,
        do_fix: bool,
        do_tests: bool,
        config_json: str = "",
    ) -> None:
        """插入任务行（audit_id 重复抛 ValueError）。config 以 JSON 序列化存 config_json。"""
        raise NotImplementedError("W11-A1")

    def get(self, audit_id: str) -> dict[str, Any] | None:
        """返回任务元数据行（不含 report 与 events；键与 audits 列一致，
        do_fix/do_tests 转 bool）；不存在返回 None。"""
        raise NotImplementedError("W11-A1")

    def exists(self, audit_id: str) -> bool:
        raise NotImplementedError("W11-A1")

    def list(self, limit: int = 0, offset: int = 0) -> tuple[int, list[dict[str, Any]]]:
        """(total, items)：created_at 降序、同秒按插入序后建者在前（新→旧语义，
        与契约 v2 列表一致）；limit=0 不分页返回全部。"""
        raise NotImplementedError("W11-A1")

    def delete(self, audit_id: str) -> bool:
        """删除任务行与其全部事件；返回是否存在过。"""
        raise NotImplementedError("W11-A1")

    def count_active(self) -> int:
        """非终态（queued+running）任务数（429 准入与 /api/health 口径）。"""
        raise NotImplementedError("W11-A1")

    def set_status(self, audit_id: str, status: str, error: str | None = None) -> None:
        """更新状态；status 非终态时 error 置 None。任务不存在时静默忽略
        （DELETE 竞态下执行线程的收尾写不得报错）。"""
        raise NotImplementedError("W11-A1")

    def set_report(self, audit_id: str, report: AuditReport) -> None:
        """落终态报告（to_dict 序列化存 report_json）。"""
        raise NotImplementedError("W11-A1")

    def get_report(self, audit_id: str) -> AuditReport | None:
        """反序列化报告；未落报告或任务不存在返回 None。"""
        raise NotImplementedError("W11-A1")

    # ---------------------------------------------------------------- 事件流
    def append_event(self, audit_id: str, event: dict[str, Any]) -> int:
        """追加事件，返回分配的 seq（同任务内单调递增从 1 开始）。
        任务不存在时静默丢弃返回 -1（DELETE 竞态容忍）。"""
        raise NotImplementedError("W11-A1")

    def get_events(self, audit_id: str, after_seq: int = 0) -> list[tuple[int, dict[str, Any]]]:
        """按 seq 升序返回 (seq, event) 列表；after_seq 游标语义供 SSE 跟随。"""
        raise NotImplementedError("W11-A1")

    # ---------------------------------------------------------------- 协作式取消（契约 1.2）
    def request_cancel(self, audit_id: str) -> bool:
        """置取消标志（error 列借用存哨兵值，或独立 meta 列——实现自定，语义保证：
        is_cancelled 变 True）；返回任务是否存在。"""
        raise NotImplementedError("W11-A1")

    def is_cancelled(self, audit_id: str) -> bool:
        """取消标志或表项已删除（二者任一即 True——表项消失即取消，DELETE 竞态容忍）。"""
        raise NotImplementedError("W11-A1")

    # ---------------------------------------------------------------- 生命周期
    def sweep_interrupted(self) -> int:
        """启动清理：遗留 queued/running → failed(error=服务重启中断)；返回 sweep 数。"""
        raise NotImplementedError("W11-A1")

    def prune(self, keep: int = 50) -> int:
        """容量淘汰：按 created_at 旧→新淘汰终态行直至总数 ≤ keep；返回淘汰数。
        只淘汰 done/failed；queued/running 不受影响（与契约 v2 FIFO 语义一致）。"""
        raise NotImplementedError("W11-A1")

    def close(self) -> None:
        """关闭连接（幂等）。"""
        raise NotImplementedError("W11-A1")
