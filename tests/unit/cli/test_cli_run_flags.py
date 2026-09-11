"""W2-A6 CLI 自测：run 子命令新参数（--review-mode/--no-verify/--fix-max/--testgen-max）透传。

monkeypatch cli.run_audit_simple 捕获 AuditConfig，断言契约 v1.2 新字段。
"""

from __future__ import annotations

import pytest

import cli
from audit.models import AuditReport


@pytest.fixture
def fake_run_simple(monkeypatch):
    calls: list = []

    async def fake_run_audit_simple(config):
        calls.append(config)
        return AuditReport(audit_id="flagtest1", project_name="demo_proj", health_score=90.0)

    monkeypatch.setattr(cli, "run_audit_simple", fake_run_audit_simple)
    return calls


def test_run_w2_flags_passthrough(fake_run_simple, tmp_path, capsys):
    rc = cli.main(
        [
            "run", str(tmp_path),
            "--review-mode", "tools",
            "--no-verify",
            "--fix-max", "5",
            "--testgen-max", "3",
        ]
    )
    assert rc == 0
    config = fake_run_simple[0]
    assert config.review_mode == "tools"
    assert config.enable_verify is False
    assert config.fix_max_patches == 5
    assert config.testgen_max_functions == 3


def test_run_defaults_keep_contract_values(fake_run_simple, tmp_path):
    """不传新参数时保持 AuditConfig 默认（simple / verify 开 / 50 / 30）。"""
    assert cli.main(["run", str(tmp_path)]) == 0
    config = fake_run_simple[0]
    assert config.review_mode == "simple"
    assert config.enable_verify is True
    assert config.fix_max_patches == 50
    assert config.testgen_max_functions == 30


def test_run_fix_max_only(fake_run_simple, tmp_path):
    assert cli.main(["run", str(tmp_path), "--fix-max", "7"]) == 0
    config = fake_run_simple[0]
    assert config.fix_max_patches == 7
    assert config.testgen_max_functions == 30  # 未指定的保持默认


def test_run_rejects_unknown_review_mode(tmp_path):
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", str(tmp_path), "--review-mode", "agent"])
    assert exc.value.code == 2  # argparse choices 校验
