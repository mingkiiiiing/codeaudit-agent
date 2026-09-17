"""W24-B CLI 实时进度自测：事件流逐阶段打印 stderr、--quiet 关闭、stdout 纯净不回退。

monkeypatch cli.run_audit_simple 注入事件流替身（append / extend 两种入列路径都覆盖），
全程离线。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import cli
from audit.config import AuditConfig
from audit.models import AuditReport, AuditStats

# 编排层事件样例（与 audit/pipeline.py emit 的字段口径一致）
_SAMPLE_EVENTS: list[dict[str, Any]] = [
    {"type": "progress", "stage": "ingest", "message": "阶段 ingest 开始"},
    {"type": "progress", "stage": "ingest", "message": "工作副本就绪：/tmp/src", "files": 3},
    {"type": "progress", "stage": "index", "message": "阶段 index 开始"},
    {"type": "progress", "stage": "detect", "message": "LLM 审查进度", "current": 3, "total": 12},
    {"type": "progress", "stage": "init", "message": "LLM 未配置，运行纯规则模式", "warning": True},
    {"type": "progress", "stage": "report", "message": "阶段 report 完成"},
    {"type": "progress", "stage": "done", "message": "审计完成：summary={'high': 0} issues=0"},
    {"type": "stage_done", "stage": "report"},  # resume 断点标记事件（无 message，不进进度行）
]


def _patch_run(monkeypatch: pytest.MonkeyPatch, payload: list[dict[str, Any]]) -> None:
    """注入假流水线：干净报告 + 把给定事件写进 cmd_run 传入的 events 列表。"""
    report = AuditReport(
        audit_id="progtest1",
        project_name="demo_proj",
        health_score=100.0,
        summary={"critical": 0, "high": 0, "medium": 0, "low": 0},
        issues=[],
        stats=AuditStats(),
    )

    async def fake(config: AuditConfig, events: list[dict[str, Any]] | None = None) -> AuditReport:
        if events is not None:
            events.extend(payload)  # 测试替身路径：extend 批量注入
        return report

    monkeypatch.setattr(cli, "run_audit_simple", fake)


def test_progress_lines_go_to_stderr_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """默认打印 [stage n/7] 进度行到 stderr；stdout 只有摘要，不含进度行。"""
    _patch_run(monkeypatch, _SAMPLE_EVENTS)
    assert cli.main(["run", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    err, out = captured.err, captured.out
    assert "[stage 1/7] 接入与工作副本开始" in err
    assert "[stage 2/7] 符号索引构建开始" in err
    assert "[stage 4/7] LLM 审查进度 (3/12)" in err  # current/total 计数
    assert "[警告] LLM 未配置" in err
    assert "[stage 7/7] 报告聚合与落盘结束" in err  # 样板"阶段 X 完成"译作"结束"
    assert "[完成] 审计完成" in err
    assert "[stage" not in out  # stdout 纯净契约：进度绝不进 stdout
    # 非 progress 类型（resume 的 stage_done 标记）只入列不打印：无空描述行
    assert not any(line.strip() == "[stage 7/7]" for line in err.splitlines())


def test_quiet_suppresses_progress_lines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--quiet 关闭进度：stderr 无 [stage]/[警告] 进度行，命令行为零变化。"""
    _patch_run(monkeypatch, _SAMPLE_EVENTS)
    assert cli.main(["run", str(tmp_path), "--quiet"]) == 0
    err = capsys.readouterr().err
    assert "[stage" not in err
    assert "[警告]" not in err
    assert "[完成]" not in err


def test_progress_keeps_check_stdout_pure_for_json_loads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--json --check（门禁失败）组合下：进度照常打 stderr，stdout 仍可整体 json.loads。"""
    report = AuditReport(
        audit_id="progcheck1",
        project_name="demo_proj",
        health_score=60.0,
        summary={"critical": 0, "high": 2, "medium": 0, "low": 0},
        issues=[],
        stats=AuditStats(),
    )

    async def fake(config: AuditConfig, events: list[dict[str, Any]] | None = None) -> AuditReport:
        if events is not None:
            events.extend(_SAMPLE_EVENTS)
        return report

    monkeypatch.setattr(cli, "run_audit_simple", fake)
    rc = cli.main(["run", str(tmp_path), "--json", "--check", "--fail-on", "high"])
    assert rc == 3
    captured = capsys.readouterr()
    data = json.loads(captured.out)  # stdout 保持纯净（进度行不得混入）
    assert data["project_name"] == "demo_proj"
    assert "[stage 1/7]" in captured.err


def test_progress_covers_realtime_append_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """真实流水线路径（run_audit_simple 收集器逐条 append）同样逐条实时打印。"""

    async def fake_run_audit_simple(
        config: AuditConfig, events: list[dict[str, Any]] | None = None
    ) -> AuditReport:
        # 模拟 run_audit_simple 内部 collector 的逐条 append（非 extend）
        for event in _SAMPLE_EVENTS[:2]:
            if events is not None:
                events.append(event)
        return AuditReport(
            audit_id="progtest2",
            project_name="demo_proj",
            health_score=100.0,
            summary={"critical": 0, "high": 0, "medium": 0, "low": 0},
            issues=[],
            stats=AuditStats(),
        )

    monkeypatch.setattr(cli, "run_audit_simple", fake_run_audit_simple)
    assert cli.main(["run", str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "[stage 1/7] 接入与工作副本开始" in err


def test_unknown_stage_falls_back_without_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """未知阶段名 / init 提示通道：前向兼容，不编号原样展示，不中断运行。"""
    _patch_run(
        monkeypatch,
        [
            {"type": "progress", "stage": "future_stage", "message": "新阶段事件"},
            {"type": "progress", "stage": "init", "message": "diff 增量仅支持本地 git 目录输入，本次跳过增量"},
        ],
    )
    assert cli.main(["run", str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "[进度] 新阶段事件" in err
    assert "[提示] diff 增量仅支持本地 git 目录输入" in err


def test_quiet_argument_documented_in_help(capsys: pytest.CaptureFixture[str]) -> None:
    """run --help 展示 --quiet（中文说明）。"""
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--help"])
    assert exc.value.code == 0
    assert "--quiet" in capsys.readouterr().out
