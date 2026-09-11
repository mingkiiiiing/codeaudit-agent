"""W4-A1 CI 门禁与新 CLI 参数自测：--check/--fail-on、--format sarif、
--baseline/--diff/--report-baseline/--config 透传、--version。

monkeypatch cli.run_audit_simple（cli 模块内引用点）注入预填 AuditReport（1 high 2 low），
全程离线、不触网。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import cli
from audit.config import AuditConfig
from audit.models import AuditReport, AuditStats, Category, Issue, Severity


def make_report(suppressed: int = 0) -> AuditReport:
    """预填报告：1 high + 2 low（summary 与 issues 一致），stats.suppressed 可调。"""
    issues = [
        Issue(
            id="ISS-0001",
            category=Category.SECURITY,
            severity=Severity.HIGH,
            title="SQL 拼接注入风险",
            file="app/a.py",
            line_start=3,
            line_end=3,
            description="用户输入未参数化",
            evidence=["rule:PY-BARE-EXCEPT"],
        ),
        Issue(
            id="ISS-0002",
            category=Category.STYLE,
            severity=Severity.LOW,
            title="魔法数字",
            file="app/b.py",
            line_start=8,
            line_end=9,
            description="硬编码超时秒数",
        ),
        Issue(
            id="ISS-0003",
            category=Category.BUG,
            severity=Severity.LOW,
            title="未判空即访问属性",
            file="app/c.py",
            line_start=12,
            line_end=14,
            description="可能返回 None",
        ),
    ]
    return AuditReport(
        audit_id="gatetest01",
        project_name="demo_proj",
        health_score=72.5,
        summary={"critical": 0, "high": 1, "medium": 0, "low": 2},
        issues=issues,
        stats=AuditStats(suppressed=suppressed),
    )


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> list[AuditConfig]:
    """注入假流水线：捕获 AuditConfig 并返回 1 high / 2 low 的预填报告。"""
    calls: list[AuditConfig] = []

    async def fake_run_audit_simple(config: AuditConfig) -> AuditReport:
        calls.append(config)
        return make_report()

    monkeypatch.setattr(cli, "run_audit_simple", fake_run_audit_simple)
    return calls


# ---------------------------------------------------------------- 门禁判定


def test_gate_fails_on_high_with_exit_3(fake_run, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--check --fail-on high：存在 1 个 high → 退出码 3，输出含「门禁」与阈值。"""
    rc = cli.main(["run", str(tmp_path), "--check", "--fail-on", "high"])
    assert rc == 3
    out = capsys.readouterr().out
    assert "门禁" in out
    assert "阈值 high" in out
    assert "退出码 3" in out


def test_gate_passes_when_no_critical(fake_run, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--fail-on critical：仅有 high/low → 门禁通过，退出码 0。"""
    rc = cli.main(["run", str(tmp_path), "--check", "--fail-on", "critical"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "门禁" in out and "通过" in out


def test_gate_bare_check_defaults_to_high(fake_run, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """仅 --check（未给阈值）：默认 high（与 SARIF error 档对齐），1 个 high → 退出码 3。"""
    rc = cli.main(["run", str(tmp_path), "--check"])
    assert rc == 3
    assert "阈值 high" in capsys.readouterr().out


def test_gate_disabled_by_default(fake_run, tmp_path: Path) -> None:
    """不带 --check/--fail-on：发现问题也正常完成（上报与门禁分离）。"""
    assert cli.main(["run", str(tmp_path)]) == 0


def test_gate_fail_message_reports_counts_and_suppressed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """失败信息包含阈值、各级计数、问题总数与 stats.suppressed。"""
    report = make_report(suppressed=4)

    async def fake(config: AuditConfig) -> AuditReport:
        return report

    monkeypatch.setattr(cli, "run_audit_simple", fake)
    rc = cli.main(["run", str(tmp_path), "--check", "--fail-on", "high"])
    assert rc == 3
    out = capsys.readouterr().out
    for token in ("阈值 high", "critical 0", "high 1", "low 2", "问题总数 3", "基线抑制 4"):
        assert token in out, token


def test_gate_threshold_from_config_file(fake_run, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """配置文件提供 fail_on_severity=low 时，--check 采用文件阈值（2 个 low → 失败）。"""
    config_file = tmp_path / ".codeaudit.toml"
    config_file.write_text('fail_on_severity = "low"\n', encoding="utf-8")
    rc = cli.main(["run", str(tmp_path), "--check", "--config", str(config_file)])
    assert rc == 3
    assert "阈值 low" in capsys.readouterr().out
    assert fake_run[0].fail_on_severity == "low"  # 配置文件值写入 config 字段


# ---------------------------------------------------------------- --format sarif


def test_run_format_sarif_writes_report_sarif(fake_run, tmp_path: Path) -> None:
    """--format sarif：out 目录出现 report.sarif，可 json.load 且结构合法。"""
    out_dir = tmp_path / "out"
    rc = cli.main(["run", str(tmp_path), "--format", "sarif", "--out", str(out_dir)])
    assert rc == 0
    sarif_path = out_dir / "report.sarif"
    assert sarif_path.exists()
    data = json.loads(sarif_path.read_text(encoding="utf-8"))
    assert data["version"] == "2.1.0"
    assert data["runs"][0]["tool"]["driver"]["name"] == "codeaudit-agent"
    assert len(data["runs"][0]["results"]) == 3


def test_run_format_sarif_written_even_when_gate_fails(fake_run, tmp_path: Path) -> None:
    """门禁失败（退出码 3）时 report.sarif 也已落盘（CI 可继续 upload-sarif）。"""
    out_dir = tmp_path / "out"
    rc = cli.main(["run", str(tmp_path), "--format", "sarif", "--out", str(out_dir), "--check", "--fail-on", "high"])
    assert rc == 3
    assert (out_dir / "report.sarif").exists()


def test_run_rejects_unknown_format(fake_run, tmp_path: Path) -> None:
    """--format 非法取值由 argparse choices 拒绝（SystemExit 2）。"""
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", str(tmp_path), "--format", "pdf"])
    assert exc.value.code == 2


# ---------------------------------------------------------------- 参数透传


def test_gate_and_baseline_flags_passthrough(fake_run, tmp_path: Path) -> None:
    """--fail-on/--baseline/--diff/--report-baseline 写入对应 config 契约字段。"""
    rc = cli.main(
        [
            "run", str(tmp_path),
            "--fail-on", "medium",
            "--baseline", "baseline.json",
            "--diff", "HEAD~1",
            "--report-baseline", "new_baseline.json",
        ]
    )
    assert rc == 3  # 1 个 high ≥ medium 阈值
    config = fake_run[0]
    assert config.fail_on_severity == "medium"
    assert config.baseline_path == "baseline.json"
    assert config.diff_ref == "HEAD~1"
    assert config.report_baseline_out == "new_baseline.json"


def test_config_flag_feeds_from_sources(fake_run, tmp_path: Path) -> None:
    """--config 显式配置文件：其键值经由 from_sources 合入 AuditConfig。"""
    config_file = tmp_path / "ca.toml"
    config_file.write_text("concurrency = 3\nmax_files = 123\n", encoding="utf-8")
    rc = cli.main(["run", str(tmp_path), "--config", str(config_file)])
    assert rc == 0
    config = fake_run[0]
    assert config.concurrency == 3
    assert config.max_files == 123


def test_config_missing_file_warns_on_stderr_but_runs(fake_run, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--config 指向不存在的文件：stderr 打印配置警告，审计照常完成（退出码 0）。"""
    rc = cli.main(["run", str(tmp_path), "--config", str(tmp_path / "nope.toml")])
    assert rc == 0
    err = capsys.readouterr().err
    assert "[配置警告]" in err and "不存在" in err


def test_implicit_flags_keep_defaults(fake_run, tmp_path: Path) -> None:
    """不传新参数时契约字段保持默认：fail_on_severity=off、基线/增量空。"""
    assert cli.main(["run", str(tmp_path)]) == 0
    config = fake_run[0]
    assert config.fail_on_severity == "off"
    assert config.baseline_path == "" and config.diff_ref == "" and config.report_baseline_out == ""


# ---------------------------------------------------------------- --version


@pytest.mark.parametrize("argv", [["--version"], ["run", "--version"]])
def test_version_prints_and_exits_0(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """顶层与 run 子命令的 --version：打印 codeaudit-agent 0.2.0 并返回 0。"""
    assert cli.main(argv) == 0
    assert "codeaudit-agent 0.2.0" in capsys.readouterr().out


def test_run_help_documents_new_flags(capsys: pytest.CaptureFixture[str]) -> None:
    """run --help 展示全部新参数（中文帮助文本）。"""
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--format", "--check", "--fail-on", "--baseline", "--report-baseline", "--diff", "--config"):
        assert flag in out, flag
