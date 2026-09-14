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

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from audit.models import AuditReport

_TERMINAL = ("done", "failed")

# 协作式取消哨兵：借用 error 列存值（契约 1.2，is_cancelled 语义以本值为准）
_CANCEL_SENTINEL = "__cancel_requested__"

# 启动 sweep（sweep_interrupted）对遗留非终态任务的失败原因
_SWEEP_ERROR = "服务重启中断"

# 任务元数据列（get/list 返回键与其一致；report_json 不在元数据之列，经 get_report 取）
_AUDIT_COLS = (
    "audit_id",
    "status",
    "error",
    "created_at",
    "source_path",
    "do_fix",
    "do_tests",
    "config_json",
)

_CREATE_AUDITS_SQL = """
CREATE TABLE IF NOT EXISTS audits (
    audit_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    source_path TEXT NOT NULL DEFAULT '',
    do_fix INTEGER NOT NULL DEFAULT 0,
    do_tests INTEGER NOT NULL DEFAULT 0,
    config_json TEXT NOT NULL DEFAULT '',
    report_json TEXT
)
"""

_CREATE_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS events (
    audit_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_json TEXT NOT NULL,
    PRIMARY KEY (audit_id, seq)
)
"""


class TaskStore:
    """SQLite 任务存储。一个进程一个实例（内部连接线程安全）。"""

    def __init__(self, db_path: Path | str) -> None:
        """打开（不存在则创建）数据库，建表，启用 WAL。"""
        self._db_path = Path(db_path)
        self._lock = threading.Lock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL：并发读不阻塞写；单连接 + 内部锁串行化全部访问
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_CREATE_AUDITS_SQL)
        self._conn.execute(_CREATE_EVENTS_SQL)
        self._conn.commit()
        self._closed = False

    # ---------------------------------------------------------------- 内部工具
    def _row_to_task(self, row: sqlite3.Row) -> dict[str, Any]:
        """行 -> 任务元数据 dict（do_fix/do_tests 转 bool，不含 report_json）。"""
        task = {col: row[col] for col in _AUDIT_COLS}
        task["do_fix"] = bool(task["do_fix"])
        task["do_tests"] = bool(task["do_tests"])
        return task

    def _select_task_sql(self, order: str = "") -> str:
        return f"SELECT {', '.join(_AUDIT_COLS)} FROM audits{order}"

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
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO audits"
                    " (audit_id, status, error, created_at, source_path, do_fix, do_tests, config_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        audit_id,
                        status,
                        error,
                        created_at,
                        source_path,
                        int(bool(do_fix)),
                        int(bool(do_tests)),
                        config_json,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise ValueError(f"audit_id 已存在: {audit_id}") from exc
            self._conn.commit()

    def get(self, audit_id: str) -> dict[str, Any] | None:
        """返回任务元数据行（不含 report 与 events；键与 audits 列一致，
        do_fix/do_tests 转 bool）；不存在返回 None。"""
        with self._lock:
            row = self._conn.execute(
                self._select_task_sql(" WHERE audit_id = ?"), (audit_id,)
            ).fetchone()
        return self._row_to_task(row) if row is not None else None

    def exists(self, audit_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM audits WHERE audit_id = ?", (audit_id,)
            ).fetchone()
        return row is not None

    def list(self, limit: int = 0, offset: int = 0) -> tuple[int, list[dict[str, Any]]]:
        """(total, items)：created_at 降序、同秒按插入序后建者在前（新→旧语义，
        与契约 v2 列表一致）；limit=0 不分页返回全部。"""
        with self._lock:
            total = int(self._conn.execute("SELECT COUNT(*) FROM audits").fetchone()[0])
            sql = self._select_task_sql(" ORDER BY created_at DESC, rowid DESC")
            params: list[Any] = []
            if limit > 0:
                sql += " LIMIT ? OFFSET ?"
                params.extend((limit, offset))
            elif offset:
                sql += " LIMIT -1 OFFSET ?"
                params.append(offset)
            rows = self._conn.execute(sql, params).fetchall()
        return total, [self._row_to_task(r) for r in rows]

    def delete(self, audit_id: str) -> bool:
        """删除任务行与其全部事件；返回是否存在过。"""
        with self._lock:
            cur = self._conn.execute("DELETE FROM audits WHERE audit_id = ?", (audit_id,))
            self._conn.execute("DELETE FROM events WHERE audit_id = ?", (audit_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def count_active(self) -> int:
        """非终态（queued+running）任务数（429 准入与 /api/health 口径）。"""
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) FROM audits WHERE status NOT IN ({', '.join('?' for _ in _TERMINAL)})",
                _TERMINAL,
            ).fetchone()
        return int(row[0])

    def set_status(self, audit_id: str, status: str, error: str | None = None) -> None:
        """更新状态；status 非终态时 error 置 None。任务不存在时静默忽略
        （DELETE 竞态下执行线程的收尾写不得报错）。"""
        if status not in _TERMINAL:
            error = None
        with self._lock:
            self._conn.execute(
                "UPDATE audits SET status = ?, error = ? WHERE audit_id = ?",
                (status, error, audit_id),
            )
            self._conn.commit()

    def set_report(self, audit_id: str, report: AuditReport) -> None:
        """落终态报告（to_dict 序列化存 report_json）。"""
        payload = json.dumps(report.to_dict(), ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "UPDATE audits SET report_json = ? WHERE audit_id = ?", (payload, audit_id)
            )
            self._conn.commit()

    def get_report(self, audit_id: str) -> AuditReport | None:
        """反序列化报告；未落报告或任务不存在返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT report_json FROM audits WHERE audit_id = ?", (audit_id,)
            ).fetchone()
        if row is None or row["report_json"] is None:
            return None
        return AuditReport.from_dict(json.loads(row["report_json"]))

    # ---------------------------------------------------------------- 事件流
    def append_event(self, audit_id: str, event: dict[str, Any]) -> int:
        """追加事件，返回分配的 seq（同任务内单调递增从 1 开始）。
        任务不存在时静默丢弃返回 -1（DELETE 竞态容忍）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM audits WHERE audit_id = ?", (audit_id,)
            ).fetchone()
            if row is None:
                return -1
            # 读-改-写组合在锁 + 事务内完成：并发 append 下 seq 连续不重
            seq = int(
                self._conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE audit_id = ?",
                    (audit_id,),
                ).fetchone()[0]
            )
            self._conn.execute(
                "INSERT INTO events (audit_id, seq, event_json) VALUES (?, ?, ?)",
                (audit_id, seq, json.dumps(event, ensure_ascii=False)),
            )
            self._conn.commit()
        return seq

    def get_events(self, audit_id: str, after_seq: int = 0) -> list[tuple[int, dict[str, Any]]]:
        """按 seq 升序返回 (seq, event) 列表；after_seq 游标语义供 SSE 跟随。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, event_json FROM events WHERE audit_id = ? AND seq > ? ORDER BY seq ASC",
                (audit_id, after_seq),
            ).fetchall()
        return [(int(r["seq"]), json.loads(r["event_json"])) for r in rows]

    # ---------------------------------------------------------------- 协作式取消（契约 1.2）
    def request_cancel(self, audit_id: str) -> bool:
        """置取消标志（error 列借用存哨兵值，或独立 meta 列——实现自定，语义保证：
        is_cancelled 变 True）；返回任务是否存在。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE audits SET error = ? WHERE audit_id = ?", (_CANCEL_SENTINEL, audit_id)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def is_cancelled(self, audit_id: str) -> bool:
        """取消标志或表项已删除（二者任一即 True——表项消失即取消，DELETE 竞态容忍）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT error FROM audits WHERE audit_id = ?", (audit_id,)
            ).fetchone()
        if row is None:
            return True
        return row["error"] == _CANCEL_SENTINEL

    # ---------------------------------------------------------------- 生命周期
    def sweep_interrupted(self) -> int:
        """启动清理：遗留 queued/running → failed(error=服务重启中断)；返回 sweep 数。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE audits SET status = 'failed', error = ? WHERE status IN ('queued', 'running')",
                (_SWEEP_ERROR,),
            )
            self._conn.commit()
        return cur.rowcount

    def prune(self, keep: int = 50) -> int:
        """容量淘汰：按 created_at 旧→新淘汰终态行直至总数 ≤ keep；返回淘汰数。
        只淘汰 done/failed；queued/running 不受影响（与契约 v2 FIFO 语义一致）。"""
        with self._lock:
            total = int(self._conn.execute("SELECT COUNT(*) FROM audits").fetchone()[0])
            excess = total - keep
            if excess <= 0:
                return 0
            placeholders = ", ".join("?" for _ in _TERMINAL)
            rows = self._conn.execute(
                f"SELECT rowid, audit_id FROM audits WHERE status IN ({placeholders})"
                " ORDER BY created_at ASC, rowid ASC",
                _TERMINAL,
            ).fetchall()
            evicted = 0
            for rowid, audit_id in rows:
                if evicted >= excess:
                    break
                self._conn.execute("DELETE FROM audits WHERE rowid = ?", (rowid,))
                self._conn.execute("DELETE FROM events WHERE audit_id = ?", (audit_id,))
                evicted += 1
            self._conn.commit()
        return evicted

    def close(self) -> None:
        """关闭连接（幂等）。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()
