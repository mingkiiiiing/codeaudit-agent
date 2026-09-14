"""tests/unit/server 公共设施（W11-A2）：TaskStore 注入与公共 fixture。

W11 起任务表迁移为 TaskStore（SQLite WAL，audit/taskstore.py），server.app 的内存
AUDITS dict 已移除。本目录所有用例经 autouse ``store`` fixture 注入独立的临时库实例
（monkeypatch server_app._STORE），用例结束自动还原——用例间零串扰，测试数据也不落
默认工作区（<work_root>/audits.db，即仓库根 .codeaudit），配合 integration/test_hygiene。

执行模型提示（W11 §1.2）：审计任务经 asyncio.to_thread 在独立线程执行。TestClient
须用上下文管理器（``with TestClient(...)``）持有跨请求存活的 portal，否则 POST 返回
时挂起的后台任务会随该请求的临时事件循环一起被清理取消（上下文管理器同时触发
lifespan——含启动 sweep，对本目录的空库是无害 no-op）。
"""

from __future__ import annotations

import pytest
from audit.config import AuditConfig
from audit.taskstore import TaskStore
from server import app as server_app

# seed_task 使用的默认 created_at（列表新→旧排序按 created_at；seed 行不参与排序断言，
# 需要控制淘汰顺序的用例显式传入递增的 created_at）
_SEED_CREATED_AT = "2026-01-01T00:00:00+08:00"


@pytest.fixture(autouse=True)
def store(monkeypatch, tmp_path):
    """为每个用例注入全新 TaskStore（tmp 库），前后无串扰。"""
    st = TaskStore(tmp_path / "test.sqlite")
    monkeypatch.setattr(server_app, "_STORE", st)
    yield st
    try:
        st.close()
    except Exception:  # noqa: BLE001 —— 用例任务线程收尾写与关库的良性竞态，静默即可
        pass


@pytest.fixture
def seed_task(store):
    """返回任务行注入工厂：seed_task(audit_id, status=..., report=..., created_at=...)。

    供「预置任务表」类用例使用（已完成任务 + 报告、遗留 running 等），
    替代内存表时代直接写 AUDITS dict 的做法。
    """

    def _seed(
        audit_id: str,
        *,
        status: str = "done",
        report=None,
        created_at: str = _SEED_CREATED_AT,
        error: str | None = None,
    ):
        store.create(
            audit_id,
            status=status,
            error=error,
            created_at=created_at,
            source_path="seeded",
            do_fix=False,
            do_tests=False,
        )
        if report is not None:
            store.set_report(audit_id, report)

    return _seed


@pytest.fixture
def work_root_tmp(monkeypatch, tmp_path):
    """把 from_env 的 work_root 重定向到 tmp_path，上传落盘不污染仓库工作区。"""
    real_from_env = AuditConfig.from_env.__func__

    def patched(cls, source_path=None, **overrides):
        overrides.setdefault("work_root", str(tmp_path))
        return real_from_env(cls, source_path=source_path, **overrides)

    monkeypatch.setattr(AuditConfig, "from_env", classmethod(patched))
    return tmp_path
