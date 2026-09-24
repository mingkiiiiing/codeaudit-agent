"""W24-E `codeaudit resume` 子命令自测：任务不存在 / 状态不可续跑 / 正常续跑三态。

P0-9 扩展：SIGKILL 级硬杀滞留 running 态的自愈分支——带进度且 updated_at 陈旧
（超 grace 窗口）先 sweep 自愈再续跑；窗口内有活动（新鲜）拒绝退出 1。

monkeypatch pipeline.run_audit 注入假流水线（不跑真实七阶段），TaskStore 用
tmp_path 隔离，全程离线。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import audit.orchestrator.pipeline as pipeline_module
import audit.config as audit_config
import cli
from audit.models import AuditReport, AuditStats
from audit.taskstore import TaskStore


@pytest.fixture(autouse=True)
def _isolate_db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离任务库定位环境变量：db 路径只随 --work-root 走（与 applyer 同口径）。"""
    monkeypatch.delenv("CODEAUDIT_DB_PATH", raising=False)
    monkeypatch.delenv("CODEAUDIT_SWEEP_GRACE_SEC", raising=False)


def _make_report(audit_id: str) -> AuditReport:
    return AuditReport(
        audit_id=audit_id,
        project_name="proj",
        health_score=90.0,
        summary={"high": 1},
        stats=AuditStats(),
    )


def _make_store(tmp_path: Path) -> TaskStore:
    work_root = tmp_path / "work"
    work_root.mkdir(exist_ok=True)
    return TaskStore(work_root / "audits.db", work_root=work_root)


def _create_task(
    store: TaskStore,
    audit_id: str,
    source: Path,
    work_root: Path,
    *,
    status: str = "interrupted",
    stage_done: tuple[str, ...] = (),
) -> None:
    """落一行任务：config_json 模拟 server 脱敏后的落库形态——api_key="<redacted>"
    在场（重建时必须被剔除、按环境变量/.env 口径补全），未知键在场（忽略）。"""
    store.create(
        audit_id,
        status=status,
        created_at="2026-01-01T00:00:00",
        source_path=str(source),
        do_fix=False,
        do_tests=False,
        config_json=json.dumps(
            {
                "source_path": str(source),
                "work_root": str(work_root),
                "api_key": "<redacted>",
                "unknown_future_key": 1,
            },
            ensure_ascii=False,
        ),
    )
    for stage in stage_done:
        store.record_stage_done(audit_id, stage)


# ---------------------------------------------------------------- 三态之一：任务不存在
def test_resume_missing_task_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "work").mkdir()
    assert cli.main(["resume", "ghost", "--work-root", str(tmp_path / "work")]) == 1
    err = capsys.readouterr().err
    assert "任务不存在" in err and "ghost" in err


# ---------------------------------------------------------------- 三态之二：状态不可续跑
@pytest.mark.parametrize(
    ("status", "stage_done"),
    [("done", ()), ("queued", ("ingest",)), ("running", ()), ("failed", ())],
)
def test_resume_non_resumable_status_exits_1(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    stage_done: tuple[str, ...],
) -> None:
    """非 interrupted（或 failed 且无进度）→ 中文报错退出 1，流水线不被拉起。"""
    work_root = tmp_path / "work"
    source = tmp_path / "proj"
    source.mkdir()
    store = _make_store(tmp_path)
    _create_task(store, "st1", source, work_root, status=status, stage_done=stage_done)

    async def _fail_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("不可续跑任务不应进入 run_audit")

    monkeypatch.setattr(pipeline_module, "run_audit", _fail_run)
    assert cli.main(["resume", "st1", "--work-root", str(work_root)]) == 1
    assert "不可续跑" in capsys.readouterr().err
    assert store.get("st1")["status"] == status  # 状态未被改动


# ---------------------------------------------------------------- 三态之三：正常续跑
@pytest.mark.parametrize("status", ["interrupted", "failed"])
def test_resume_happy_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    """interrupted / 带进度的 failed → 复位 running → run_audit(resume_stages=判定集)
    → 报告落库、状态置 done、中文摘要含跳过阶段与问题总数。"""
    work_root = tmp_path / "work"
    source = tmp_path / "proj"
    source.mkdir()
    (source / "a.py").write_text("x = 1\n", encoding="utf-8")
    store = _make_store(tmp_path)
    _create_task(store, "ok1", source, work_root, status=status, stage_done=("ingest",))
    (work_root / "ok1" / "src").mkdir(parents=True)  # 工作副本在盘 → resume 判定放行 ingest

    calls: dict[str, Any] = {}

    async def fake_run_audit(config: Any, emitter: Any, *, audit_id: Any = None, resume_stages: Any = None) -> Any:
        calls["audit_id"] = audit_id
        calls["resume_stages"] = frozenset(resume_stages or ())
        calls["source_path"] = config.source_path
        calls["api_key_not_redacted"] = config.api_key != "<redacted>"
        calls["status_at_run"] = store.get("ok1")["status"]  # 复位发生在 run 之前
        await emitter({"type": "progress", "stage": "init", "message": "fake 进度"})  # 与真实 run_audit 同构
        return _make_report(str(audit_id))

    monkeypatch.setattr(pipeline_module, "run_audit", fake_run_audit)
    assert cli.main(["resume", "ok1", "--work-root", str(work_root)]) == 0

    assert calls["audit_id"] == "ok1"
    assert calls["resume_stages"] == frozenset({"ingest"})  # resume_stage_done 判定结果
    assert calls["source_path"] == str(source)  # config_json 重建生效
    assert calls["api_key_not_redacted"] is True  # 脱敏值未回传（未知键同理被忽略）
    assert calls["status_at_run"] == "running"  # mark_resuming 复位先于 run_audit
    assert store.get("ok1")["status"] == "done"  # 终态收尾
    assert store.get_report("ok1") is not None
    captured = capsys.readouterr()
    assert "续跑完成" in captured.out
    assert "ingest" in captured.out  # 摘要含跳过阶段
    assert "问题总数：0" in captured.out  # 假报告 issues 为空
    assert "[提示] fake 进度" in captured.err  # 进度事件走 stderr（与 cmd_run 一致）


# ---------------------------------------------------------------- P0-9：SIGKILL 滞留 running 的 sweep 自愈

def _backdate_updated_at(store: TaskStore, audit_id: str, iso: str) -> None:
    """白盒构造「陈旧行」：updated_at 改写为指定历史时刻（形态同 taskstore grace 用例）。"""
    with store._lock:
        store._conn.execute(
            "UPDATE audits SET updated_at = ? WHERE audit_id = ?", (iso, audit_id)
        )
        store._conn.commit()


def _stale_iso(seconds_ago: int) -> str:
    """生成 seconds_ago 前的 UTC ISO 字符串（与 TaskStore._utc_now_iso 同格式）。"""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat(
        timespec="seconds"
    )


def test_resume_sigkill_stale_running_self_heals(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGKILL 滞留态自愈：running+进度+updated_at 陈旧（超缺省 30s 窗口）→
    sweep 置 interrupted → 正常续跑成功（不复读「不可续跑」旧拒绝分支）。"""
    work_root = tmp_path / "work"
    source = tmp_path / "proj"
    source.mkdir()
    (source / "a.py").write_text("x = 1\n", encoding="utf-8")
    store = _make_store(tmp_path)
    _create_task(store, "kill1", source, work_root, status="running", stage_done=("ingest",))
    _backdate_updated_at(store, "kill1", _stale_iso(60))  # 60s 前 > 缺省 30s 窗口
    (work_root / "kill1" / "src").mkdir(parents=True)  # 工作副本在盘 → 判定放行 ingest

    calls: dict[str, Any] = {}

    async def fake_run_audit(config: Any, emitter: Any, *, audit_id: Any = None, resume_stages: Any = None) -> Any:
        calls["audit_id"] = audit_id
        calls["resume_stages"] = frozenset(resume_stages or ())
        calls["status_at_run"] = store.get("kill1")["status"]
        await emitter({"type": "progress", "stage": "init", "message": "fake 进度"})
        return _make_report(str(audit_id))

    monkeypatch.setattr(pipeline_module, "run_audit", fake_run_audit)
    assert cli.main(["resume", "kill1", "--work-root", str(work_root)]) == 0
    assert calls["audit_id"] == "kill1"
    assert calls["resume_stages"] == frozenset({"ingest"})  # sweep 自愈后走正常续跑判定
    assert calls["status_at_run"] == "running"  # mark_resuming 复位先于 run_audit
    assert store.get("kill1")["status"] == "done"
    assert store.get_report("kill1") is not None
    assert "续跑完成" in capsys.readouterr().out


def test_resume_fresh_running_rejected(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """running+进度但 updated_at 新鲜（窗口内有活动，疑似真有执行体在跑）→
    中文报错退出 1（含「运行」），状态不被改动、流水线不被拉起。"""
    work_root = tmp_path / "work"
    source = tmp_path / "proj"
    source.mkdir()
    store = _make_store(tmp_path)
    _create_task(store, "live1", source, work_root, status="running", stage_done=("ingest",))
    # create/record_stage_done 刚 touch 过 updated_at = 当前时刻（新鲜），无需回写

    async def _fail_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("窗口内活跃任务不应进入 run_audit")

    monkeypatch.setattr(pipeline_module, "run_audit", _fail_run)
    assert cli.main(["resume", "live1", "--work-root", str(work_root)]) == 1
    err = capsys.readouterr().err
    assert "仍在运行" in err and "请稍后重试" in err  # 报错含「运行」口径
    assert "不可续跑" not in err  # 自愈拒绝与既有「不可续跑」是两条分支
    assert store.get("live1")["status"] == "running"  # sweep 未误伤，状态保持
    assert store.get_stage_done("live1") == ["ingest"]  # 进度保留


def test_resume_sweep_grace_env_zero_sweeps_fresh_running(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CODEAUDIT_SWEEP_GRACE_SEC=0（legacy 语义：凡非终态一律清扫）→ 新鲜 running
    滞留行也自愈成功——grace 环境变量接线生效的正面证据。"""
    work_root = tmp_path / "work"
    source = tmp_path / "proj"
    source.mkdir()
    (source / "a.py").write_text("x = 1\n", encoding="utf-8")
    store = _make_store(tmp_path)
    _create_task(store, "g0", source, work_root, status="running", stage_done=("ingest",))
    (work_root / "g0" / "src").mkdir(parents=True)
    monkeypatch.setenv("CODEAUDIT_SWEEP_GRACE_SEC", "0")

    async def fake_run_audit(config: Any, emitter: Any, *, audit_id: Any = None, resume_stages: Any = None) -> Any:
        return _make_report(str(audit_id))

    monkeypatch.setattr(pipeline_module, "run_audit", fake_run_audit)
    assert cli.main(["resume", "g0", "--work-root", str(work_root)]) == 0
    assert store.get("g0")["status"] == "done"
    assert "续跑完成" in capsys.readouterr().out


# ---------------------------------------------------------------- F7-R1（W27 卡 A）：无 --work-root 的 LLM 通道端到端


def _reset_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """F7-R1 用例隔离：GLM 三键清空 + 复位 .env 加载标志——cmd_resume 的
    from_env（work_root 派生）与 _rebuild_config_from_task_json（config 重建）
    链路读到的必须是本用例自己的假 .env，而非进程内早前用例的缓存/真实环境。"""
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("GLM_BASE_URL", raising=False)
    monkeypatch.delenv("GLM_MODEL", raising=False)
    monkeypatch.setattr(audit_config, "_ENV_LOADED", False)


def _seed_f7r1_task(tmp_path: Path) -> tuple[Path, TaskStore]:
    """无 --work-root 形态的 seed：任务库落在默认 work_root（.codeaudit）下，
    供 cli.main(["resume", ...]) 按 CWD 派生路径定位。"""
    work_root = tmp_path / ".codeaudit"
    source = tmp_path / "proj"
    source.mkdir()
    (source / "a.py").write_text("x = 1\n", encoding="utf-8")
    work_root.mkdir()
    store = TaskStore(work_root / "audits.db", work_root=work_root)
    _create_task(store, "f7r1", source, work_root, stage_done=("ingest",))
    (work_root / "f7r1" / "src").mkdir(parents=True)  # 工作副本在盘 → 判定放行 ingest
    return work_root, store


def test_resume_without_work_root_keeps_api_key(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端到端（审计踩中路径）：不带 --work-root 时 cmd_resume 先经 from_env 派生
    work_root，再 _rebuild_config_from_task_json 重建 config——修复前前置调用
    消耗进程首次 .env 加载，重建拿到空 api_key（pipeline 走 FakeLLM 静默降级）；
    修复后（F7-R1 缓存）重建链路 api_key 与首次一致，摘要标注 LLM 通道已启用。"""
    _reset_llm_env(monkeypatch)
    monkeypatch.chdir(tmp_path)  # CWD 即默认 work_root（.codeaudit）与 .env 发现基点
    (tmp_path / ".env").write_text("GLM_API_KEY=sk-resume-f7r1\n", encoding="utf-8")
    _work_root, store = _seed_f7r1_task(tmp_path)

    calls: dict[str, Any] = {}

    async def fake_run_audit(config: Any, emitter: Any, *, audit_id: Any = None, resume_stages: Any = None) -> Any:
        calls["api_key"] = config.api_key
        await emitter({"type": "progress", "stage": "init", "message": "fake 进度"})
        return _make_report(str(audit_id))

    monkeypatch.setattr(pipeline_module, "run_audit", fake_run_audit)
    assert cli.main(["resume", "f7r1"]) == 0  # 不带 --work-root
    assert calls["api_key"] == "sk-resume-f7r1"  # 修复前为 ""（重建链路丢 key）
    out = capsys.readouterr().out
    assert "LLM 通道：已启用" in out  # 摘要防御性标注
    assert store.get("f7r1")["status"] == "done"
    store.close()


def test_resume_without_key_summary_reports_rule_mode(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 GLM_API_KEY（env 与 .env 均无）：摘要如实标注「LLM 通道：未启用」，
    显式暴露本次为纯规则模式（修复前静默，用户无感知降级）。"""
    _reset_llm_env(monkeypatch)
    monkeypatch.chdir(tmp_path)  # CWD 无 .env
    _work_root, store = _seed_f7r1_task(tmp_path)

    calls: dict[str, Any] = {}

    async def fake_run_audit(config: Any, emitter: Any, *, audit_id: Any = None, resume_stages: Any = None) -> Any:
        calls["api_key"] = config.api_key
        await emitter({"type": "progress", "stage": "init", "message": "fake 进度"})
        return _make_report(str(audit_id))

    monkeypatch.setattr(pipeline_module, "run_audit", fake_run_audit)
    assert cli.main(["resume", "f7r1"]) == 0
    assert calls["api_key"] == ""
    out = capsys.readouterr().out
    assert "LLM 通道：未启用（未检测到 API Key，本次为纯规则模式）" in out
    assert store.get("f7r1")["status"] == "done"
    store.close()
