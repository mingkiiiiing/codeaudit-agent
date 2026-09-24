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
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import audit.detect.engine as detect_engine
import audit.ingest as ingest_module
import audit.indexer.store as indexer_store_module
import audit.refactor as refactor_module
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

    # —— W24-E：detect 断点产物校验 ——
    store.record_stage_done(audit_id, "detect")
    # stage_done 记了 detect 但产物不在盘 → 不放行（宁缺勿跳）
    assert resume_stage_done(store, audit_id, config) == frozenset({"ingest", "index"})
    state_dir = task_root / "state"
    state_dir.mkdir()
    artifact = state_dir / "detect.json"
    artifact.write_text(json.dumps({"issues": [], "stats": {}}), encoding="utf-8")
    # 产物合法（schema：issues 为列表 + stats 为 dict）→ 放行 detect
    assert resume_stage_done(store, audit_id, config) == frozenset({"ingest", "index", "detect"})
    # schema 不符（缺 stats）→ 剔除 detect，回落重跑
    artifact.write_text(json.dumps({"issues": []}), encoding="utf-8")
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


# ---------------------------------------------------------------- W24-E：detect 断点产物落盘与续跑
_DET_PROJECT = {
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


def _setup_interrupt_after_detect(
    tmp_path: Path,
    make_project: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    audit_id: str,
) -> tuple[AuditConfig, TaskStore, Path, dict[str, int]]:
    """公共编排：detect 完成后进程死亡（refactor 中点注入 _ProcessKilled）。

    返回 (config, store, work_root, detect_calls)——detect_calls 为 run_detection
    调用计数（「detect 未重跑」断言的证据）。第一次运行在本函数内完成并断言
    断点状态（stage_done 含 detect、detect.json 落盘、sweep 置 interrupted）。
    """
    project = make_project(dict(_DET_PROJECT), name=f"det_{audit_id}")
    work_root = tmp_path / "work"
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

    detect_calls = {"n": 0}
    orig_run_detection = detect_engine.run_detection

    def _counting_detection(ctx, **kwargs):
        detect_calls["n"] += 1
        return orig_run_detection(ctx, **kwargs)

    monkeypatch.setattr(detect_engine, "run_detection", _counting_detection)

    # 中断注入：refactor 阶段（detect 之后的下一站）首次调用抛 _ProcessKilled
    # （BaseException 直穿 _run_stage 的 Exception 兜底 = 进程死亡形态）
    orig_refactor = refactor_module.run_refactor_stage
    refactor_state = {"armed": True}

    def _refactor_maybe_boom(ctx):
        if refactor_state["armed"]:
            raise _ProcessKilled("模拟 detect 之后进程被杀（W24-E 测试注入）")
        return orig_refactor(ctx)

    monkeypatch.setattr(refactor_module, "run_refactor_stage", _refactor_maybe_boom)

    events1: list[dict[str, Any]] = []
    with pytest.raises(_ProcessKilled):
        asyncio.run(run_audit(config, _store_emitter(store, audit_id, events1), audit_id=audit_id))
    refactor_state["armed"] = False  # 解除注入（模拟重启后异常源不复存在）

    # 断点状态：detect 已完成并落盘产物
    assert detect_calls["n"] == 1
    assert store.get_stage_done(audit_id) == ["ingest", "index", "understand", "detect"]
    artifact = work_root / audit_id / "state" / "detect.json"
    assert artifact.is_file()
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert isinstance(payload["stats"], dict)
    assert len(payload["issues"]) > 0  # 靶点项目必有 SQL 注入问题
    assert set(payload["issues"][0]) >= {"title", "file", "severity"}  # Issue.to_dict 形态

    # 进程重启：sweep 置 interrupted（工作副本 / 索引 / 断点产物保留）
    assert store.sweep_interrupted() == 1
    assert store.get(audit_id)["status"] == "interrupted"
    return config, store, work_root, detect_calls


def test_resume_skips_completed_detect(
    tmp_path: Path, make_project: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """W24-E 主链路：detect 完成后中断 → resume 跳过整个 detect，issues 来自盘上。

    断言链：resume 判定放行 ingest/index/detect → 二次运行 run_detection 不再被
    调用（规则扫描 / LLM 审查 / 后处理扫描器全不跑）、事件流有「自断点恢复」且
    无「阶段 detect 开始」→ 最终报告 issues 与盘上产物数量一致、与全新对照审计
    一致（规则确定性）、报告完整落盘。
    """
    config, store, work_root, detect_calls = _setup_interrupt_after_detect(
        tmp_path, make_project, monkeypatch, "resume-det-1"
    )
    audit_id = "resume-det-1"
    n_issues = len(
        json.loads((work_root / audit_id / "state" / "detect.json").read_text(encoding="utf-8"))["issues"]
    )

    # resume 判定：三个盘上阶段全部放行；复位走 W24-E 的 mark_resuming（CLI 同款）
    done = resume_stage_done(store, audit_id, config)
    assert done == frozenset({"ingest", "index", "detect"})
    assert store.mark_resuming(audit_id) is True
    assert store.get(audit_id)["status"] == "running"

    events2: list[dict[str, Any]] = []
    report = asyncio.run(
        run_audit(config, _store_emitter(store, audit_id, events2), audit_id=audit_id, resume_stages=done)
    )

    # detect 整体跳过：检测入口零调用，事件流无阶段开始/完成样板
    assert detect_calls["n"] == 1
    detect_msgs = _stage_messages(events2, "detect")
    assert "阶段 detect 开始" not in detect_msgs
    assert not any("检测完成" in m for m in detect_msgs)
    init_msgs = [str(e.get("message", "")) for e in events2 if e.get("stage") == "init"]
    assert any("detect 阶段自断点恢复" in m and f"跳过 {n_issues} 条" in m for m in init_msgs)
    assert any("复用既有工作副本" in m for m in init_msgs)  # ingest 复用照旧
    # detect 之后的阶段正常重跑
    assert "阶段 report 开始" in _stage_messages(events2, "report")
    assert events2[-1]["stage"] == "done"

    # 报告来自盘上产物：数量一致 + 与全新对照审计的问题清单一致（规则确定性）
    assert len(report.issues) == n_issues
    assert report.audit_id == audit_id
    events_ctrl: list[dict[str, Any]] = []
    control = asyncio.run(run_audit(config, _collector(events_ctrl)))
    assert _issue_keys(report) == _issue_keys(control)
    assert (work_root / audit_id / "reports" / "report.json").is_file()

    # 断点清单：前序保留（detect 不因 resume 丢记），detect 之后阶段补记
    done_after = store.get_stage_done(audit_id)
    assert done_after[:4] == ["ingest", "index", "understand", "detect"]
    assert {"refactor", "report"} <= set(done_after)


def test_resume_detect_falls_back_when_artifact_corrupt(
    tmp_path: Path, make_project: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """detect.json 损坏的两道防线：判定剔除 + 执行回落（诚实降级，不吞错）。

    - 防线一（判定层）：resume_stage_done 校验产物 → detect 剔除出跳过集合，
      CLI 按此集合续跑时 detect 照常重跑（无额外事件，走正常阶段样板）；
    - 防线二（执行层）：调用方直接传入含 detect 的 resume_stages（模拟判定与
      执行之间产物被破坏的竞态 / 陈旧判定）→ run_audit 加载失败发「回落」
      warning 事件并全量重跑。
    """
    config, store, work_root, detect_calls = _setup_interrupt_after_detect(
        tmp_path, make_project, monkeypatch, "resume-det-2"
    )
    audit_id = "resume-det-2"
    (work_root / audit_id / "state" / "detect.json").write_text("{not-json", encoding="utf-8")

    # 防线一：判定剔除 detect；按判定集合续跑 → detect 正常重跑
    done = resume_stage_done(store, audit_id, config)
    assert done == frozenset({"ingest", "index"})
    assert store.mark_resuming(audit_id) is True

    events2: list[dict[str, Any]] = []
    report = asyncio.run(
        run_audit(config, _store_emitter(store, audit_id, events2), audit_id=audit_id, resume_stages=done)
    )
    assert detect_calls["n"] == 2
    assert "阶段 detect 开始" in _stage_messages(events2, "detect")
    assert any("检测完成" in m for m in _stage_messages(events2, "detect"))
    assert len(report.issues) > 0
    assert events2[-1]["stage"] == "done"

    # 防线二：陈旧判定（含 detect）直传 run_audit → 加载失败回落 + warning 事件
    # （上一轮 detect 重跑成功时已把断点产物重新写好，这里再次破坏它）
    (work_root / audit_id / "state" / "detect.json").write_text("{not-json", encoding="utf-8")
    events3: list[dict[str, Any]] = []
    stale = frozenset({"ingest", "index", "detect"})
    asyncio.run(
        run_audit(config, _store_emitter(store, audit_id, events3), audit_id=audit_id, resume_stages=stale)
    )
    assert detect_calls["n"] == 3  # 回落重跑（检测入口再次被调用）
    init_msgs = [str(e.get("message", "")) for e in events3 if e.get("stage") == "init"]
    assert any("回落全量重跑 detect" in m for m in init_msgs)
    assert "阶段 detect 开始" in _stage_messages(events3, "detect")
    assert events3[-1]["stage"] == "done"
    assert store.get_stage_done(audit_id).count("detect") == 1  # 幂等补记不重复
