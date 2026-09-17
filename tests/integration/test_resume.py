"""W24-C resume 断点续跑集成测试（docs/23 卡 C 验收口径）。

场景：真实七阶段流水线 + TaskStore 事件落库（server 的 emitter 接线形态，
但不起 server 进程）——detect 阶段中点抛 KeyboardInterrupt 模拟进程被杀 →
启动 sweep 把带阶段进度的 running 任务置 interrupted（工作副本与索引保留）→
resume 重建（resume_stage_done 判定 + run_audit 传 resume_stages）→
断言已完成阶段未重跑（事件流 + ingest/build 调用计数）+ 最终报告完整。

全程零网络零真实 LLM（conftest autouse 消毒 GLM 环境变量，纯规则模式）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import audit.detect.engine as detect_engine
import audit.ingest as ingest_module
import audit.indexer.store as indexer_store_module
from audit.config import AuditConfig
from audit.orchestrator.pipeline import resume_stage_done, run_audit
from audit.report.comparator import compare
from audit.taskstore import TaskStore


def _make_config(project: Path, work_root: Path, audit_id: str) -> AuditConfig:
    return AuditConfig(
        source_path=str(project),
        work_root=str(work_root),
        out_dir=str(work_root / audit_id / "reports"),
        enable_llm_review=False,  # 纯规则模式（零网络）
        do_fix=False,
        do_tests=False,
    )


def _store_emitter(store: TaskStore, audit_id: str, collected: list[dict[str, Any]]):
    """server._wrapped_emitter 同形态：事件落库 + 本地收集（供事件流断言）。"""

    async def emitter(event: dict[str, Any]) -> None:
        if store.is_cancelled(audit_id):
            raise asyncio.CancelledError()
        collected.append(dict(event))
        store.append_event(audit_id, dict(event))

    return emitter


class _ProcessKilled(BaseException):
    """进程被杀的确定性模拟（W24-C 测试注入）。

    不用 KeyboardInterrupt：asyncio Task 对 KI/SystemExit 有特判（loop.stop +
    重抛路径），pytest 又把它当会话中止信号，逃逸行为不确定。自定义
    BaseException 子类传播路径完全相同——pipeline 的 _run_stage 只兜底
    Exception，BaseException 直穿 = 阶段中点进程死亡形态。
    """


def _stage_messages(events: list[dict[str, Any]], stage: str) -> list[str]:
    return [str(e.get("message", "")) for e in events if e.get("stage") == stage]


def _issue_keys(report: Any) -> list[tuple[str, int, str]]:
    """(file, line, rule) 提取（rule 取 evidence 的 rule: 标记，缺省 title）。"""
    keys = []
    for i in report.issues:
        rule = next((str(e)[5:] for e in i.evidence if str(e).startswith("rule:")), i.title)
        keys.append((i.file, int(i.line_start), rule))
    return sorted(keys)


# ---------------------------------------------------------------- 主链路：中断 → sweep → resume
def test_interrupt_then_resume_skips_completed_stages(
    tmp_path: Path, make_project: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(
        {
            "app.py": (
                "import sqlite3\n"
                "\n"
                "\n"
                "def find_user(conn, username):\n"
                "    query = \"SELECT * FROM users WHERE name = '\" + username + \"'\"\n"
                "    return conn.execute(query).fetchall()\n"
            ),
            "util.py": "def slugify(text):\n    return text.strip().lower()\n",
        },
        name="resume_proj",
    )
    work_root = tmp_path / "work"
    audit_id = "resume-e2e-1"
    config = _make_config(project, work_root, audit_id)
    store = TaskStore(tmp_path / "audits.db", work_root=work_root)
    store.create(
        audit_id,
        status="running",
        created_at="2026-01-01T00:00:00",
        source_path=str(project),
        do_fix=False,
        do_tests=False,
        config_json="{}",
    )

    # 计数器：ingest 物化调用数 / 索引 build 次数（resume 断言"未重跑"的证据）
    ingest_calls = {"n": 0}
    orig_ingest = ingest_module.ingest

    def _counting_ingest(source, work_root_, audit_id=None, **kwargs):
        ingest_calls["n"] += 1
        return orig_ingest(source, work_root_, audit_id=audit_id, **kwargs)

    monkeypatch.setattr(ingest_module, "ingest", _counting_ingest)

    build_calls = {"n": 0}
    orig_build = indexer_store_module.SqliteIndexStore.build

    def _counting_build(self):
        build_calls["n"] += 1
        return orig_build(self)

    monkeypatch.setattr(indexer_store_module.SqliteIndexStore, "build", _counting_build)

    # 中断注入：detect 中点抛 _ProcessKilled（_run_stage 只兜底 Exception，
    # BaseException 直穿 = 进程被杀形态）；首次调用后恢复正常（模拟重启后续跑）
    orig_run_detection = detect_engine.run_detection
    interrupt_state = {"armed": True}

    def _run_detection_maybe_boom(ctx, **kwargs):
        if interrupt_state["armed"]:
            raise _ProcessKilled("模拟进程被杀（W24-C 测试注入）")
        return orig_run_detection(ctx, **kwargs)

    monkeypatch.setattr(detect_engine, "run_detection", _run_detection_maybe_boom)

    # -------- 第一次运行：ingest/index/understand 完成，detect 中断
    events1: list[dict[str, Any]] = []
    with pytest.raises(_ProcessKilled):
        asyncio.run(run_audit(config, _store_emitter(store, audit_id, events1), audit_id=audit_id))
    interrupt_state["armed"] = False  # 解除注入（模拟重启后异常源不复存在）

    assert ingest_calls["n"] == 1
    assert build_calls["n"] == 1
    assert store.get_stage_done(audit_id) == ["ingest", "index", "understand"]  # 断点已持久化（detect 在 understand 之后被杀）
    assert (work_root / audit_id / "src").is_dir()  # 工作副本存活
    assert (work_root / audit_id / "index.db").is_file()

    # -------- 进程重启：sweep 置 interrupted（而非 failed），磁盘保留
    assert store.sweep_interrupted() == 1
    row = store.get(audit_id)
    assert row["status"] == "interrupted"
    assert "可续跑" in row["error"]
    assert (work_root / audit_id / "src").is_dir()  # interrupted 不回收工作副本

    # -------- resume 重建：恢复判定 + 状态复位 + 带 resume_stages 重跑
    done = resume_stage_done(store, audit_id, config)
    assert done == frozenset({"ingest", "index"})
    store.set_status(audit_id, "running")  # interrupted → running（集成人接线姿势）

    events2: list[dict[str, Any]] = []
    report = asyncio.run(
        run_audit(config, _store_emitter(store, audit_id, events2), audit_id=audit_id, resume_stages=done)
    )

    # 已完成阶段未重跑：事件流无 ingest/index 阶段开始，调用计数不增长
    assert "阶段 ingest 开始" not in _stage_messages(events2, "ingest")
    assert "阶段 index 开始" not in _stage_messages(events2, "index")
    assert any("复用既有工作副本" in m for m in _stage_messages(events2, "init"))
    assert any("索引库复用" in m for m in _stage_messages(events2, "init"))
    assert ingest_calls["n"] == 1  # 工作副本未重拷
    assert build_calls["n"] == 1  # 索引未重建
    # 后续阶段正常重跑
    assert "阶段 understand 开始" in _stage_messages(events2, "understand")
    assert "阶段 detect 开始" in _stage_messages(events2, "detect")
    assert "阶段 report 开始" in _stage_messages(events2, "report")
    assert events2[-1]["stage"] == "done"  # 最终完整跑完

    # 最终报告完整：与同项目全新对照审计的问题清单一致（规则确定性）
    events_ctrl: list[dict[str, Any]] = []
    control = asyncio.run(run_audit(config, _collector(events_ctrl)))
    assert _issue_keys(report) == _issue_keys(control)
    assert len(report.issues) > 0
    assert report.audit_id == audit_id

    # 断点清单继续推进：前序断点保留，重跑阶段补记（fix/testgen 未启用、未完成不记）
    done_after = store.get_stage_done(audit_id)
    assert done_after[:2] == ["ingest", "index"]
    assert {"understand", "detect", "report"} <= set(done_after)

    # server 收尾形态：报告落库 + 终态
    store.set_report(audit_id, report)
    store.set_status(audit_id, "done")
    assert store.get(audit_id)["status"] == "done"
    assert store.get_report(audit_id) is not None


def _collector(events: list[dict[str, Any]]):
    async def emit(event: dict[str, Any]) -> None:
        events.append(dict(event))

    return emit


# ---------------------------------------------------------------- 恢复判定的防御分支
def test_resume_stage_done_validations(tmp_path: Path) -> None:
    """恢复判定逐条防御：缺任务行 / 缺进度 / 源不一致 / 副本或索引缺失。"""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.py").write_text("x = 1\n", encoding="utf-8")
    work_root = tmp_path / "work"
    audit_id = "valid-1"
    config = _make_config(project, work_root, audit_id)
    store = TaskStore(tmp_path / "audits.db", work_root=work_root)

    # 任务行不存在 → 空集
    assert resume_stage_done(store, audit_id, config) == frozenset()

    store.create(
        audit_id,
        status="interrupted",
        created_at="2026-01-01T00:00:00",
        source_path=str(project),
        do_fix=False,
        do_tests=False,
        config_json="{}",
    )
    # 无 stage_done 进度 → 空集
    assert resume_stage_done(store, audit_id, config) == frozenset()

    store.record_stage_done(audit_id, "ingest")
    store.record_stage_done(audit_id, "index")
    # 盘上产物不在 → 空集（宁可全量重跑）
    assert resume_stage_done(store, audit_id, config) == frozenset()

    task_root = work_root / audit_id
    (task_root / "src").mkdir(parents=True)
    # 只有工作副本、索引缺 → 只放行 ingest
    assert resume_stage_done(store, audit_id, config) == frozenset({"ingest"})
    (task_root / "index.db").write_bytes(b"")
    # 副本 + 索引齐 → 放行两个盘上阶段
    assert resume_stage_done(store, audit_id, config) == frozenset({"ingest", "index"})

    # 源不一致（换了项目路径）→ 不放行（MVP 同源校验：resolve 后路径一致）
    other = tmp_path / "proj2"
    other.mkdir()
    config_other = _make_config(other, work_root, audit_id)
    assert resume_stage_done(store, audit_id, config_other) == frozenset()


# ---------------------------------------------------------------- 端到端：两次审计报告 compare
def test_two_runs_compare_reports_changes(tmp_path: Path, make_project: Callable[..., Path]) -> None:
    """流水线 × comparator 端到端：修复一个文件后重审，compare 反映问题消长。"""
    source = {
        "app.py": (
            "import sqlite3\n"
            "\n"
            "\n"
            "def find_user(conn, username):\n"
            "    query = \"SELECT * FROM users WHERE name = '\" + username + \"'\"\n"
            "    return conn.execute(query).fetchall()\n"
        ),
        "util.py": "def slugify(text):\n    return text.strip().lower()\n",
    }
    work_root = tmp_path / "work"
    project = make_project(dict(source), name="cmp_run1")
    config = _make_config(project, work_root, "cmp-a")
    report_a = asyncio.run(run_audit(config, _collector([])))

    # 第二次：修复 SQL 拼接（换项目目录重审，模拟修复后代码）
    fixed = dict(source)
    fixed["app.py"] = (
        "import sqlite3\n"
        "\n"
        "\n"
        "def find_user(conn, username):\n"
        "    query = \"SELECT * FROM users WHERE name = ?\"\n"
        "    return conn.execute(query, (username,)).fetchall()\n"
    )
    project2 = make_project(fixed, name="cmp_run2")
    config2 = _make_config(project2, work_root, "cmp-b")
    report_b = asyncio.run(run_audit(config2, _collector([])))

    result = compare(report_a, report_b)
    # 自反性预检：同一报告自比 → 全部 persisted，无 fixed/new（compare 基线口径）
    self_result = compare(report_a, report_a)
    assert self_result["fixed"] == [] and self_result["new"] == []
    assert len(self_result["persisted"]) == len(report_a.issues)

    # 注入点修复后：原 SQL 拼接问题消失（fixed），不再 persisted
    fixed_keys = {(i.file, int(i.line_start)) for i in result["fixed"]}
    persisted_keys = {(i.file, int(i.line_start)) for i in result["persisted"]}
    assert len(report_a.issues) > 0
    assert ("app.py", 5) in fixed_keys  # 修复点行号的问题被判定为 fixed
    assert ("app.py", 5) not in persisted_keys
    # 未改动文件的问题两轮同现 → persisted（util.py 无问题则该清单为空，不变式仍成立）
    for issue in report_a.issues:
        if issue.file == "util.py":
            assert (issue.file, int(issue.line_start)) in persisted_keys
