"""任务持久化存储层（W11-A1，契约 docs/16 §1.1）：SQLite（WAL）承载审计任务表与事件流。

设计要点（实现者必须遵守）：
- 标准库 sqlite3，零第三方依赖；WAL 模式（并发读 + 单写）；
- check_same_thread=False：server 事件循环线程与任务工作线程（W11 §1.2 to_thread）
  并发访问同一实例，所有写操作经内部 threading.Lock 串行化；
- 行结构（W15-B4 起 audits 增加 updated_at 列，旧库由 __init__ 自动迁移回填；
  W24-C 起再增 stage_done 列，存已完成阶段的 JSON 数组，旧库同样自动迁移补列）：
    audits(audit_id TEXT PRIMARY KEY, status TEXT, error TEXT, created_at TEXT,
           source_path TEXT, do_fix INTEGER, do_tests INTEGER,
           config_json TEXT, report_json TEXT, updated_at TEXT,
           stage_done TEXT)
    events(audit_id TEXT, seq INTEGER, event_json TEXT,
           PRIMARY KEY (audit_id, seq))
    report_history(audit_id TEXT, seq INTEGER, created_at TEXT, report_json TEXT,
                   health_score REAL, issue_count INTEGER,
                   PRIMARY KEY (audit_id, seq))
  （report_history 为 W27-B 新增：结果版本化。CREATE TABLE IF NOT EXISTS 随构造
  建立，老库零破坏、无需列迁移；seq 每任务从 1 递增，单事务原子分配照
  append_event 形态；单一事实源仍为 audits.report_json，历史表只增不改不删，
  任务行删除时级联清除——与 events 的级联语义一致，同 ID 重建后 seq 重新从 1 计数）
- 状态机：queued | running | done | failed | interrupted。前四态与契约 v2 一致；
  interrupted 为 W24-C 新增（docs/23 卡 C 明确放宽 v2 的"无新状态"约束）：
  进程重启 sweep 时，带 stage_done 进度（已跑完至少一个阶段）的 running 任务
  改置 interrupted——阶段进度与工作副本保留，可续跑；queued 与无进度的
  running 仍按既有语义置 failed。
- 本模块只管存储：不启动线程、不触网、不解析 config_json / report_json 之外的对象图
  （report 用 audit.models.AuditReport.to_dict()/from_dict() 序列化）；
- 方法均为同步阻塞（调用方负责放入合适的执行上下文；store 内部锁保证线程安全）。

W15-B 存储一致性加固（契约 docs/20 §4.3，多 worker 三处竞态 A3 收口）：
- W15-B1：连接建立即 PRAGMA busy_timeout=5000（SQLite 层写锁忙等）；
- W15-B2：写操作遇 locked/busy 的 OperationalError 指数退避重试（0.05/0.1/0.2s，
  ≤3 次），实现为模块级装饰器 _retry_on_locked，避免逐方法复制；
- W15-B3：append_event 以 BEGIN IMMEDIATE 单事务原子分配 seq，消除跨连接
  （跨进程）读 MAX(seq)+1 与 INSERT 之间的竞态窗口；
- W15-B4：audits.updated_at 统一 touch（UTC ISO-8601 定宽字符串），sweep 支持看门窗
  sweep_interrupted(grace_seconds=0)，默认 0 与既有语义逐字节一致。

本文件为 W11-A5 集成人出具的接口骨架：签名与语义即契约，W11-A1 填实现，
W11-A2 按签名消费（单测用自建内存假 store，不依赖本实现）。
"""

from __future__ import annotations

import functools
import json
import logging
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from audit.models import AuditReport

_LOG = logging.getLogger(__name__)

_TERMINAL = ("done", "failed")

# W24-C：可续跑状态——sweep 对带 stage_done 进度的 running 任务置此态；
# 不属于终态（cleanup/prune 的终态守卫对其拒绝，工作副本与索引保留供 resume），
# 也不计入 count_active（无执行体在跑，不应占用 429 并发准入额度）
_INTERRUPTED = "interrupted"

# count_active 的排除集合：终态 + interrupted（queued/running 之外的都算"不活跃"）
_NON_ACTIVE = _TERMINAL + (_INTERRUPTED,)

# 协作式取消哨兵：借用 error 列存值（契约 1.2，is_cancelled 语义以本值为准）
_CANCEL_SENTINEL = "__cancel_requested__"

# 启动 sweep（sweep_interrupted）对遗留非终态任务的失败原因
_SWEEP_ERROR = "服务重启中断"

# W24-C：sweep 对带阶段进度的 running 任务的置态原因（可续跑，非失败）
_SWEEP_INTERRUPTED_ERROR = "服务重启中断（阶段进度已保留，可续跑）"

# W24-C：stage_done 事件的类型标记。编排层每阶段完成发
# {"type": "stage_done", "stage": <阶段名>}；append_event 识别该类型并在同一
# 事务把阶段名并入 audits.stage_done——server 的 emitter→append_event 既有
# 通路因此零改动获得断点持久化（server/app.py 本卡禁改，这是唯一不加参数的接线方式）
_STAGE_DONE_EVENT_TYPE = "stage_done"

# W15-B1：连接级 busy_timeout（毫秒）——SQLite 写锁争用的第一级缓冲，
# 应用层指数退避（W15-B2）为第二级；两级叠加后普通争用用户无感
_BUSY_TIMEOUT_MS = 5000

# W15-B2：写操作遇库锁（database is locked / table is locked / busy）的
# 指数退避重试序列；耗尽后原样抛出最后一次 OperationalError
_LOCK_RETRY_DELAYS: tuple[float, ...] = (0.05, 0.1, 0.2)
_LOCKED_MARKERS = ("locked", "busy")

# W15-B3：事件追加单事务 SQL——seq 经标量子查询取 COALESCE(MAX(seq),0)+1。
# 不用 INSERT...SELECT...WHERE 形态：空表时 SELECT 无行会导致整条 INSERT 落空
_APPEND_EVENT_SQL = """
INSERT INTO events (audit_id, seq, event_json)
SELECT ?, COALESCE((SELECT MAX(seq) FROM events WHERE audit_id = ?), 0) + 1, ?
"""

# W27-B：报告历史单事务 SQL——seq 经标量子查询取 COALESCE(MAX(seq),0)+1，
# 与 _APPEND_EVENT_SQL 同形（BEGIN IMMEDIATE 先取库写锁再执行，写锁持有期间
# 任何其他连接/进程无法插入同任务版本，MAX+1 即全局唯一，消除读 MAX 与
# INSERT 之间的竞态窗口）
_APPEND_REPORT_HISTORY_SQL = """
INSERT INTO report_history (audit_id, seq, created_at, report_json, health_score, issue_count)
SELECT ?, COALESCE((SELECT MAX(seq) FROM report_history WHERE audit_id = ?), 0) + 1, ?, ?, ?, ?
"""


def _utc_now_iso() -> str:
    """W15-B4：当前 UTC 时间的 ISO-8601 定宽字符串（updated_at 统一存此格式）。

    选型说明（见 sweep_interrupted docstring）：timespec=seconds 固定 25 字符、
    同一 +00:00 偏移，字典序即时间序，SQL 侧字符串比较等价时间比较。
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_stage_done(raw: str | None) -> list[str]:
    """W24-C：解析 audits.stage_done 列（JSON 字符串数组）为阶段名列表。

    容错优先：列空 / 非法 JSON / 形状不符一律返回空列表（老库该列全为 ''，
    解析失败按"无阶段进度"处理，只会让 sweep 走既有 failed 分支，方向保守）。
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(stage) for stage in data if str(stage).strip()]


def _merge_stage_done(raw: str | None, stage: str) -> str | None:
    """W24-C：把 stage 幂等并入 stage_done JSON；已存在返回 None（无需写库）。"""
    stages = _parse_stage_done(raw)
    if stage in stages:
        return None
    stages.append(stage)
    return json.dumps(stages, ensure_ascii=False)


def _is_lock_contention(exc: sqlite3.OperationalError) -> bool:
    """W15-B2：判定 OperationalError 是否为库锁/busy 争用（其余错误不重试）。"""
    msg = str(exc).lower()
    return any(marker in msg for marker in _LOCKED_MARKERS)


def _retry_on_locked(fn):
    """W15-B2：写操作库锁退避重试装饰器（create/set_status/append_event 等共用）。

    命中 locked/busy 的 OperationalError 时按 _LOCK_RETRY_DELAYS 指数退避重试
    （≤3 次，0.05/0.1/0.2s），耗尽后抛最后一次错误；其他异常原样上抛。
    每次重试前回滚半途事务——被装饰方法整体重放（内部自行重新获取实例锁），
    退避等待期间不持有实例锁，同进程其他线程不受阻塞。
    """

    @functools.wraps(fn)
    def wrapper(self: TaskStore, *args: Any, **kwargs: Any):
        for attempt in range(len(_LOCK_RETRY_DELAYS) + 1):
            try:
                return fn(self, *args, **kwargs)
            except sqlite3.OperationalError as exc:
                if attempt >= len(_LOCK_RETRY_DELAYS) or not _is_lock_contention(exc):
                    raise
                try:
                    self._conn.rollback()
                except sqlite3.Error:  # pragma: no cover —— 回滚失败不掩盖原始锁错误
                    pass
                delay = _LOCK_RETRY_DELAYS[attempt]
                _LOG.warning(
                    "W15-B2 写操作遇库锁，%.2fs 后重试（第 %d/%d 次）：%s",
                    delay,
                    attempt + 1,
                    len(_LOCK_RETRY_DELAYS),
                    exc,
                )
                time.sleep(delay)

    return wrapper

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
    report_json TEXT,
    updated_at TEXT NOT NULL DEFAULT '',
    stage_done TEXT NOT NULL DEFAULT ''
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

# W27-B（结果版本化）：报告历史表。新库随构造建立；老库无此表时 CREATE TABLE
# IF NOT EXISTS 同样即时建立（照 stage_done 列迁移的"构造时零破坏补齐"形态，
# 新表无存量行，不需要 ADD COLUMN 式迁移）。
_CREATE_REPORT_HISTORY_SQL = """
CREATE TABLE IF NOT EXISTS report_history (
    audit_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT '',
    report_json TEXT NOT NULL,
    health_score REAL,
    issue_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (audit_id, seq)
)
"""


class TaskStore:
    """SQLite 任务存储。一个进程一个实例（内部连接线程安全）。"""

    def __init__(self, db_path: Path | str, work_root: Path | str | None = None) -> None:
        """打开（不存在则创建）数据库，建表，启用 WAL。

        work_root（M-4，可选）：任务工作区根。配置后 enable 磁盘回收能力
        （cleanup_workdir / delete / prune / sweep_interrupted 会同步回收
        <work_root>/<audit_id>/ 工作目录与 <work_root>/uploads/<id>.zip）；
        None（缺省）时磁盘回收为 no-op——向后兼容既有构造与测试注入。
        """
        self._db_path = Path(db_path)
        self._work_root = Path(work_root) if work_root is not None else None
        self._lock = threading.Lock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # W15-B1：连接建立即设 busy_timeout——写锁忙等 5s 再交给应用层退避重试
        self._conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        # WAL：并发读不阻塞写；单连接 + 内部锁串行化全部访问
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_CREATE_AUDITS_SQL)
        self._conn.execute(_CREATE_EVENTS_SQL)
        # W27-B：结果版本化——report_history 随构造建立（IF NOT EXISTS：新库即建，
        # 老库补建且存量数据零影响；无列迁移诉求，照 stage_done 迁移的构造时形态）
        self._conn.execute(_CREATE_REPORT_HISTORY_SQL)
        # W15-B4：旧库迁移——audits 无 updated_at 列时补列并回填 created_at
        #（旧行从未被 touch，最后更新时间只能以创建时间近似；新库建表已含该列，跳过）
        existing_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(audits)")}
        if "updated_at" not in existing_cols:
            self._conn.execute(
                "ALTER TABLE audits ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''"
            )
            self._conn.execute("UPDATE audits SET updated_at = created_at WHERE updated_at = ''")
            _LOG.info("W15-B4 audits.updated_at 列迁移完成（回填 created_at）：%s", self._db_path)
        # W24-C：旧库迁移——audits 无 stage_done 列时补列（默认 '' = 无阶段进度，
        # 存量行只会走 sweep 既有 failed 分支，语义不变；ADD COLUMN 带默认值时
        # SQLite 对既有行立即按默认值生效，无需逐行回填）
        if "stage_done" not in existing_cols:
            self._conn.execute(
                "ALTER TABLE audits ADD COLUMN stage_done TEXT NOT NULL DEFAULT ''"
            )
            _LOG.info("W24-C audits.stage_done 列迁移完成：%s", self._db_path)
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
        return f"SELECT {', '.join(_AUDIT_COLS)} FROM audits{order}"  # codeaudit: ignore[PY-SQL-INJECTION] f-string 仅拼接内部常量列名与 order 片段，两处调用方均传内部常量字符串，无外部输入（W21 卡2 定性）

    # ---------------------------------------------------------------- 任务 CRUD
    @_retry_on_locked
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
        """插入任务行（audit_id 重复抛 ValueError）。config 以 JSON 序列化存 config_json。

        W15-B4：updated_at 不回填调用方 created_at（其可能是任意时区的历史值），
        新行即活性行，取当前 UTC 时间——grace 清扫窗口自创建时刻起算。
        """
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO audits"
                    " (audit_id, status, error, created_at, source_path, do_fix, do_tests,"
                    "  config_json, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        audit_id,
                        status,
                        error,
                        created_at,
                        source_path,
                        int(bool(do_fix)),
                        int(bool(do_tests)),
                        config_json,
                        _utc_now_iso(),
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

    @_retry_on_locked
    def delete(self, audit_id: str) -> bool:
        """删除任务行与其全部事件及报告历史（W27-B 起含 report_history 级联）；返回是否存在过。

        M-4：终态任务的删除同步回收工作目录与上传 zip；非终态（queued/running）
        是协作式取消语义——行消失即取消信号，此处只删行、绝不删文件（运行中的
        工作副本由执行体在幽灵收尾时回收，见 server.app._run_audit_sync）。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM audits WHERE audit_id = ?", (audit_id,)
            ).fetchone()
            cur = self._conn.execute("DELETE FROM audits WHERE audit_id = ?", (audit_id,))
            self._conn.execute("DELETE FROM events WHERE audit_id = ?", (audit_id,))
            # W27-B：报告历史随任务行级联清除（与 events 同语义——删行是权威操作，
            # 同 ID 重建后 seq 重新从 1 计数；「只增不改不删」指 set_report 写路径）
            self._conn.execute("DELETE FROM report_history WHERE audit_id = ?", (audit_id,))
            self._conn.commit()
        if cur.rowcount > 0 and row is not None and row["status"] in _TERMINAL:
            # 行已删（absent）→ cleanup_workdir 的终态守卫放行；失败仅 warning
            self.cleanup_workdir(audit_id)
        return cur.rowcount > 0

    def count_active(self) -> int:
        """非终态任务数（429 准入与 /api/health 口径）。

        W24-C：interrupted 同样不计入——该态无执行体在跑（等待显式 resume），
        不应占用并发准入额度；口径即"queued + running"。
        """
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) FROM audits WHERE status NOT IN ({', '.join('?' for _ in _NON_ACTIVE)})",  # codeaudit: ignore[PY-SQL-INJECTION] f-string 仅拼接 ? 占位符，_NON_ACTIVE 值全部参数化（W21 卡2 定性）
                _NON_ACTIVE,
            ).fetchone()
        return int(row[0])

    @_retry_on_locked
    def set_status(self, audit_id: str, status: str, error: str | None = None) -> None:
        """更新状态；status 非终态时 error 置 None。任务不存在时静默忽略
        （DELETE 竞态下执行线程的收尾写不得报错）。

        W15-B4：状态写统一 touch updated_at（grace 清扫的活性依据）。
        """
        if status not in _TERMINAL:
            error = None
        with self._lock:
            self._conn.execute(
                "UPDATE audits SET status = ?, error = ?, updated_at = ? WHERE audit_id = ?",
                (status, error, _utc_now_iso(), audit_id),
            )
            self._conn.commit()

    @_retry_on_locked
    def set_report(self, audit_id: str, report: AuditReport) -> None:
        """落终态报告（to_dict 序列化存 report_json），并同事务追加一条历史版本。

        W15-B4：报告落库同属行写路径，一并 touch updated_at。
        W27-B 结果版本化：BEGIN IMMEDIATE 单事务内完成「UPDATE audits.report_json
        + INSERT report_history」双写——resume/重跑再次落报告时旧版本不丢，当前
        报告永远等于历史最大 seq（单一事实源仍为 audits.report_json，历史表只增
        不改不删）。seq 按 COALESCE(MAX(seq),0)+1 在写锁内原子分配（照 append_event
        W15-B3 形态）；摘要列（created_at/health_score/issue_count）与 report_json
        同源同事务落库。任务行不存在时 UPDATE 命中 0 行、历史同样不写（与既有
        幽灵静默语义同口径，DELETE 竞态下执行线程的收尾写不得报错）。
        失败（含锁争用）回滚整个事务，由 _retry_on_locked 决定重试或上抛。
        """
        payload = json.dumps(report.to_dict(), ensure_ascii=False)
        now = _utc_now_iso()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cur = self._conn.execute(
                    "UPDATE audits SET report_json = ?, updated_at = ? WHERE audit_id = ?",
                    (payload, now, audit_id),
                )
                if cur.rowcount > 0:  # 任务存在才写历史；UPDATE 0 行 = 幽灵静默
                    self._conn.execute(
                        _APPEND_REPORT_HISTORY_SQL,
                        (
                            audit_id,
                            audit_id,
                            now,
                            payload,
                            float(report.health_score),
                            len(report.issues),
                        ),
                    )
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def get_report(
        self, audit_id: str, seq: int | None = None
    ) -> AuditReport | dict[str, Any] | None:
        """读报告。单参形态（seq 缺省）为既有读路径，行为零变化：返回 AuditReport
        对象，未落报告或任务不存在返回 None。

        W27-B：带 seq 读历史版本——返回该版本完整 report dict（形态与
        report.to_dict() 一致，即当前报告端点 JSON 的同源形状）；任务不存在 /
        无历史 / seq 不存在返回 None。当前报告 = 历史最大 seq（单一事实源为
        audits.report_json，本方法不写库）。
        """
        if seq is None:
            with self._lock:
                row = self._conn.execute(
                    "SELECT report_json FROM audits WHERE audit_id = ?", (audit_id,)
                ).fetchone()
            if row is None or row["report_json"] is None:
                return None
            return AuditReport.from_dict(json.loads(row["report_json"]))
        with self._lock:
            row = self._conn.execute(
                "SELECT report_json FROM report_history WHERE audit_id = ? AND seq = ?",
                (audit_id, int(seq)),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["report_json"])

    def list_reports(self, audit_id: str) -> list[dict[str, Any]]:
        """W27-B：报告历史版本摘要列表（seq 升序）：seq/created_at/health_score/
        issue_count，不含 report_json 全文；任务不存在或尚无任何版本返回空列表。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, created_at, health_score, issue_count FROM report_history"
                " WHERE audit_id = ? ORDER BY seq ASC",
                (audit_id,),
            ).fetchall()
        return [
            {
                "seq": int(r["seq"]),
                "created_at": r["created_at"],
                "health_score": r["health_score"],
                "issue_count": int(r["issue_count"]),
            }
            for r in rows
        ]

    # ---------------------------------------------------------------- 事件流
    @_retry_on_locked
    def append_event(self, audit_id: str, event: dict[str, Any]) -> int:
        """追加事件，返回分配的 seq（同任务内单调递增从 1 开始）。
        任务不存在时静默丢弃返回 -1（DELETE 竞态容忍）。

        W15-B3：seq 分配收进单事务——BEGIN IMMEDIATE 先取库写锁，再执行
        「标量子查询取 COALESCE(MAX(seq),0)+1 → INSERT」，写锁持有期间任何其他
        连接/进程的写事务无法插入同任务事件，MAX+1 即全局唯一，消除既有
        「SELECT MAX 与 INSERT 之间」的跨连接竞态窗口（原实现仅实例内锁保护）。
        失败（含锁争用）回滚整个事务，由 _retry_on_locked 决定重试或上抛。

        W15-B4：同一事务内 touch updated_at——事件流是运行活性的直接证据，
        长跑任务持续 append 时行保持「新鲜」，grace 清扫不会误杀兄弟 worker
        正在推进的任务。

        W24-C：event["type"] == "stage_done" 时在同一事务把 event["stage"] 幂等
        并入 audits.stage_done 列（JSON 数组）——断点进度的持久化通路。放在
        append_event 而非独立方法被编排层调用，是因为 server 的 emitter 只会把
        事件原样递到这里（server/app.py 禁改，不能给它加参数）；同事务保证
        「事件可见 ⇔ 进度可见」的原子性，进程在任意点被杀都不会出现事件流说
        阶段完成、进度列却没记上的分裂状态。
        """
        payload = json.dumps(event, ensure_ascii=False)
        stage_done_merge: str | None = None
        if event.get("type") == _STAGE_DONE_EVENT_TYPE:
            stage = str(event.get("stage") or "").strip()
            if stage:
                stage_done_merge = stage  # 先记下待并入的阶段名，取到旧行后再合并
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT stage_done FROM audits WHERE audit_id = ?", (audit_id,)
                ).fetchone()
                if row is None:
                    self._conn.rollback()
                    return -1
                if stage_done_merge is not None:
                    merged = _merge_stage_done(row["stage_done"], stage_done_merge)
                    if merged is not None:  # None = 该阶段已在列（幂等去重，不写）
                        self._conn.execute(
                            "UPDATE audits SET stage_done = ? WHERE audit_id = ?",
                            (merged, audit_id),
                        )
                self._conn.execute(_APPEND_EVENT_SQL, (audit_id, audit_id, payload))
                # 写锁未释放前读回 MAX(seq)：必为本次插入分配的 seq
                seq = int(
                    self._conn.execute(
                        "SELECT COALESCE(MAX(seq), 0) FROM events WHERE audit_id = ?",
                        (audit_id,),
                    ).fetchone()[0]
                )
                self._conn.execute(
                    "UPDATE audits SET updated_at = ? WHERE audit_id = ?",
                    (_utc_now_iso(), audit_id),
                )
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise
        return seq

    def get_events(self, audit_id: str, after_seq: int = 0) -> list[tuple[int, dict[str, Any]]]:
        """按 seq 升序返回 (seq, event) 列表；after_seq 游标语义供 SSE 跟随。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, event_json FROM events WHERE audit_id = ? AND seq > ? ORDER BY seq ASC",
                (audit_id, after_seq),
            ).fetchall()
        return [(int(r["seq"]), json.loads(r["event_json"])) for r in rows]

    # ---------------------------------------------------------------- 断点续跑（W24-C）
    @_retry_on_locked
    def record_stage_done(self, audit_id: str, stage: str) -> bool:
        """记录阶段完成（幂等）：stage 幂等并入 audits.stage_done，返回任务是否存在。

        直接调用入口（供测试与无事件流的调用方）；生产通路由 append_event 的
        stage_done 事件类型触发，两处共享 _merge_stage_done 的幂等语义。
        W15-B4 同口径：记录即 touch updated_at（行写路径统一）。
        """
        stage = str(stage).strip()
        if not stage:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT stage_done FROM audits WHERE audit_id = ?", (audit_id,)
            ).fetchone()
            if row is None:
                return False
            merged = _merge_stage_done(row["stage_done"], stage)
            if merged is not None:
                self._conn.execute(
                    "UPDATE audits SET stage_done = ?, updated_at = ? WHERE audit_id = ?",
                    (merged, _utc_now_iso(), audit_id),
                )
                self._conn.commit()
        return True

    def get_stage_done(self, audit_id: str) -> list[str]:
        """返回已完成阶段名列表（记录序）；任务不存在或无进度返回空列表。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT stage_done FROM audits WHERE audit_id = ?", (audit_id,)
            ).fetchone()
        return _parse_stage_done(row["stage_done"]) if row is not None else []

    @_retry_on_locked
    def mark_resuming(self, audit_id: str) -> bool:
        """W24-E（resume 扩展）：interrupted / failed → running（续跑前的状态复位）。

        状态机守卫：仅这两种可续跑态放行并返回 True；queued / running / 终态
        （done）一律拒绝返回 False；任务不存在返回 False。带阶段进度的 failed
        也放行（CLI 侧已先校验 stage_done 非空，这里复位本身无害——进度列保留，
        resume_stage_done 会按盘上产物逐阶段把关）。复位同时清空 error（与
        set_status 对非终态强制 error=None 同口径）并 touch updated_at。
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE audits SET status = 'running', error = NULL, updated_at = ?"
                " WHERE audit_id = ? AND status IN (?, ?)",
                (_utc_now_iso(), audit_id, _INTERRUPTED, "failed"),
            )
            self._conn.commit()
        return cur.rowcount > 0

    # ---------------------------------------------------------------- 协作式取消（契约 1.2）
    @_retry_on_locked
    def request_cancel(self, audit_id: str) -> bool:
        """置取消标志（error 列借用存哨兵值，或独立 meta 列——实现自定，语义保证：
        is_cancelled 变 True）；返回任务是否存在。

        W15-B4：取消写一并 touch updated_at（行写路径统一口径）。
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE audits SET error = ?, updated_at = ? WHERE audit_id = ?",
                (_CANCEL_SENTINEL, _utc_now_iso(), audit_id),
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

    # ---------------------------------------------------------------- 磁盘工作区回收（M-4）
    @property
    def work_root(self) -> Path | None:
        """任务工作区根（磁盘回收基座）；构造时未配置返回 None。"""
        return self._work_root

    def cleanup_workdir(self, audit_id: str) -> bool:
        """回收任务的磁盘痕迹（M-4）：工作目录 <work_root>/<audit_id>/ 与上传 zip
        （<work_root>/uploads/<audit_id>.zip 及其落盘中的 .part）。

        安全约束（实现者必须遵守）：
        - 只回收终态（done/failed）或已不存在（行已删）任务的目录——行存在且非终态
          （queued/running/interrupted，interrupted 自 W24-C 起为可续跑态，工作副本
          与索引是 resume 的前提产物）时拒绝执行，绝不 rmtree 运行中/待续跑任务的工作副本；
        - 只删除 resolve 后确位于 work_root 直接之下、且路径恰为 <work_root>/<audit_id>
          的目录（audit_id 必须是单一安全路径段，防路径注入）；形状不符一律不动；
        - work_root 未配置时 no-op（返回 False）；
        - 清理失败（Windows 文件占用/权限常见）不抛错：记 warning 并返回 False——
          「删行是权威操作」，磁盘回收失败不得影响表操作。

        返回是否全部回收成功（无东西可回收也视为成功）。
        """
        if (
            not audit_id
            or audit_id in {".", ".."}
            or Path(audit_id).name != audit_id
            or audit_id != audit_id.strip()
        ):
            return False  # 非单一安全路径段：拒绝（防路径注入）
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM audits WHERE audit_id = ?", (audit_id,)
            ).fetchone()
        if row is not None and row["status"] not in _TERMINAL:
            return False  # queued/running：绝不回收运行中任务的工作副本
        if self._work_root is None:
            return False
        ok = True
        work_root = self._work_root.resolve()
        workdir = self._work_root / audit_id
        try:
            resolved = workdir.resolve()
        except OSError:  # pragma: no cover —— resolve 失败按形状不符处理，不动
            resolved = None
        if resolved is not None and resolved.parent == work_root and resolved.is_dir():
            try:
                shutil.rmtree(resolved)
            except OSError as exc:
                _LOG.warning("任务工作目录回收失败（保留文件，不影响删行）：%s：%s", resolved, exc)
                ok = False
        uploads = self._work_root / "uploads"
        for name in (f"{audit_id}.zip", f"{audit_id}.zip.part"):
            candidate = uploads / name
            try:
                if candidate.is_file():
                    candidate.unlink()
            except OSError as exc:
                _LOG.warning("上传 zip 回收失败（保留文件，不影响删行）：%s：%s", candidate, exc)
                ok = False
        return ok

    # ---------------------------------------------------------------- 生命周期
    @_retry_on_locked
    def sweep_interrupted(self, grace_seconds: int = 0) -> int:
        """启动清理：遗留 queued/running 置终态；返回 sweep 数。

        W15-B4 新增关键字参数 grace_seconds（默认 0 = 既有语义：凡 queued/running
        一律清扫，与 W11/W14 行为逐字节一致）；>0 时仅清扫 updated_at 早于
        now-grace_seconds 的行——多 worker 部署下（server 侧 CODEAUDIT_SWEEP_GRACE_SEC，
        接线归卡A）兄弟 worker 仍在推进的任务 updated_at 持续被 touch，不会因本进程
        重启而被误置 failed（修复 A3 的 sweep 复活竞态）。

        时间表示选型（决策记录）：updated_at 存 UTC ISO-8601 定宽字符串
        （_utc_now_iso：timespec=seconds、固定 25 字符、同一 +00:00 偏移），
        grace 判定用 SQL 字符串比较（updated_at < cutoff）——该格式下字典序即
        时间序，免解析、无需新列类型；不选 epoch 整数是为与 created_at 的 ISO
        风格同族、便于人工查库排查。旧库迁移（__init__）补列时回填 created_at：
        迁移当刻的存量非终态行本就是 sweep 目标，以其创建时间近似最后更新时间，
        只会提前（而非漏掉）清扫，方向保守安全。

        W24-C（resume 断点续跑）：置态按阶段进度分流——
        - running 且 stage_done 非空（至少跑完一个阶段）→ 置 interrupted（可续跑），
          工作副本与索引**不回收**（resume 重建的前提产物）；
        - queued、以及无任何阶段进度的 running → 置 failed（既有语义逐字节保留：
          一行未跑完的任务没有可续跑的进度，中断即全损，与历史行为一致）。
        只有置 failed 的任务才同步回收磁盘痕迹（M-4 语义不变）。

        M-4：被 sweep 置 failed 的任务在落态后同步回收工作目录与上传 zip——服务重启后
        本进程不再有执行体持有这些目录（多 worker 下其他 worker 仍在跑的任务同样会被
        置 failed，这是契约 docs/16 §1.1 既有的重启语义，磁盘回收与其保持同口径）。
        """
        with self._lock:
            if grace_seconds > 0:
                cutoff = (
                    datetime.now(timezone.utc) - timedelta(seconds=grace_seconds)
                ).isoformat(timespec="seconds")
                where = "status IN ('queued', 'running') AND updated_at < ?"
                where_params: tuple[Any, ...] = (cutoff,)
            else:
                where = "status IN ('queued', 'running')"
                where_params = ()
            rows = self._conn.execute(
                "SELECT audit_id, status, stage_done FROM audits WHERE " + where,  # codeaudit: ignore[PY-SQL-INJECTION] where 为分支内硬编码字面量片段，cutoff 参数化（W21 卡2 定性）
                where_params,
            ).fetchall()
            # W24-C：置态分流在 Python 侧按 stage_done 解析结果判定（列值解析
            # 容错见 _parse_stage_done；SQL 侧不做 JSON 判断，保持方言最小面）
            interrupted_ids = [
                r["audit_id"]
                for r in rows
                if r["status"] == "running" and _parse_stage_done(r["stage_done"])
            ]
            interrupted_set = set(interrupted_ids)
            failed_ids = [r["audit_id"] for r in rows if r["audit_id"] not in interrupted_set]
            now = _utc_now_iso()
            for ids, status, error in (
                (interrupted_ids, _INTERRUPTED, _SWEEP_INTERRUPTED_ERROR),
                (failed_ids, "failed", _SWEEP_ERROR),
            ):
                for audit_id in ids:  # 启动 sweep 一次，行数量级小，逐行 UPDATE 最直白
                    self._conn.execute(
                        "UPDATE audits SET status = ?, error = ?, updated_at = ? WHERE audit_id = ?",
                        (status, error, now, audit_id),
                    )
            self._conn.commit()
        # 只有 failed 组回收磁盘；interrupted 组保留工作副本与索引供 resume（其
        # 状态非终态，即便误入 cleanup_workdir 也会被终态守卫拒绝，双保险）
        for audit_id in failed_ids:
            self.cleanup_workdir(audit_id)
        return len(interrupted_ids) + len(failed_ids)

    @_retry_on_locked
    def prune(self, keep: int = 50) -> int:
        """容量淘汰：按 created_at 旧→新淘汰终态行直至总数 ≤ keep；返回淘汰数。
        只淘汰 done/failed；queued/running 不受影响（与契约 v2 FIFO 语义一致）。
        M-4：被淘汰行的工作目录与上传 zip 同步回收（提交后锁外执行，行已删 →
        cleanup 的终态守卫放行）；回收失败仅记 warning，不影响淘汰本身。"""
        with self._lock:
            total = int(self._conn.execute("SELECT COUNT(*) FROM audits").fetchone()[0])
            excess = total - keep
            if excess <= 0:
                return 0
            placeholders = ", ".join("?" for _ in _TERMINAL)
            rows = self._conn.execute(
                f"SELECT rowid, audit_id FROM audits WHERE status IN ({placeholders})"  # codeaudit: ignore[PY-SQL-INJECTION] f-string 仅拼接 ? 占位符，_TERMINAL 值全部参数化（W21 卡2 定性）
                " ORDER BY created_at ASC, rowid ASC",
                _TERMINAL,
            ).fetchall()
            evicted = 0
            evicted_ids: list[str] = []
            for rowid, audit_id in rows:
                if evicted >= excess:
                    break
                self._conn.execute("DELETE FROM audits WHERE rowid = ?", (rowid,))
                self._conn.execute("DELETE FROM events WHERE audit_id = ?", (audit_id,))
                self._conn.execute("DELETE FROM report_history WHERE audit_id = ?", (audit_id,))
                evicted_ids.append(audit_id)
                evicted += 1
            self._conn.commit()
        for audit_id in evicted_ids:
            self.cleanup_workdir(audit_id)
        return evicted

    def close(self) -> None:
        """关闭连接（幂等）。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()
