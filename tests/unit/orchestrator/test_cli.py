"""T5 CLI 自测：run 摘要输出与 index 错误降级（不触网、不依赖未集成模块）。"""

from __future__ import annotations

import json

import pytest

import cli
from audit.models import AuditReport


@pytest.fixture
def fake_run_simple(monkeypatch):
    calls: list = []

    async def fake_run_audit_simple(config, events=None):
        calls.append(config)
        return AuditReport(
            audit_id="clitest01",
            project_name="demo_proj",
            health_score=91.2,
            summary={"critical": 0, "high": 1, "medium": 2, "low": 3},
        )

    monkeypatch.setattr(cli, "run_audit_simple", fake_run_audit_simple)
    return calls


def test_run_prints_summary(capsys, fake_run_simple, tmp_path):
    rc = cli.main(["run", str(tmp_path), "--no-llm", "--out", str(tmp_path / "out")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "审计完成" in out
    assert "91.2" in out
    assert "critical 0" in out and "high 1" in out and "low 3" in out
    config = fake_run_simple[0]
    assert config.enable_llm_review is False
    assert config.out_dir == str(tmp_path / "out")


def test_run_json_output(capsys, fake_run_simple, tmp_path):
    rc = cli.main(["run", str(tmp_path), "--json", "--fix", "--tests"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["audit_id"] == "clitest01"
    assert data["health_score"] == 91.2
    assert fake_run_simple[0].do_fix and fake_run_simple[0].do_tests


def test_run_missing_source_exits_1(capsys):
    """源路径不存在 → 顶层捕获，中文提示 + 退出码 1。"""
    rc = cli.main(["run", "Z:/no/such/path"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "[错误]" in err


def test_index_without_ingest_module_exits_1(capsys, tmp_path, monkeypatch):
    """audit.ingest 未集成：index 子命令给出中文提示并退出码 1。"""
    import sys

    monkeypatch.setitem(sys.modules, "audit.ingest", None)  # 确定性屏蔽（无论 T2 是否已合入）
    rc = cli.main(["index", str(tmp_path)])
    assert rc == 1
    assert "[错误]" in capsys.readouterr().err


def test_run_warns_on_degraded_ingest(capsys, tmp_path, monkeypatch):
    """R4-10：ingest 失败（降级运行）时 CLI 在 stderr 打一行降级警告。"""
    import sys
    import types

    def broken_ingest(source, work_root, audit_id=None):
        raise RuntimeError("unzip failed")

    monkeypatch.setitem(
        sys.modules, "audit.ingest", _fake_ingest_module(broken_ingest)
    )
    rc = cli.main(["run", str(tmp_path), "--no-llm", "--out", str(tmp_path / "out")])
    assert rc == 0
    err = capsys.readouterr().err
    assert "[警告]" in err and "降级" in err


def _fake_ingest_module(broken_ingest):
    import types

    mod = types.ModuleType("audit.ingest")
    mod.ingest = broken_ingest
    return mod
