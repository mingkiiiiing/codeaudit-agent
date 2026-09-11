"""T1 契约层自测：配置文件合成 from_sources 与问题指纹（契约 v1.4）。"""

from pathlib import Path

from audit.config import AuditConfig
from audit.models import Category, Issue
from audit.utils import issue_fingerprint


def _make(**kwargs) -> Issue:
    base = dict(
        id="ISS-0001",
        category=Category.SECURITY,
        title="SQL 拼接",
        file="a.py",
        line_start=11,
        line_end=11,
    )
    base.update(kwargs)
    return Issue(**base)


def test_from_sources_dotfile_discovery(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    (tmp_path / ".codeaudit.toml").write_text(
        'concurrency = 3\nmodel = "file-model"\nunknown_key = 1\n',
        encoding="utf-8",
    )
    cfg = AuditConfig.from_sources(search_root=tmp_path)
    assert cfg.concurrency == 3
    assert cfg.model == "file-model"
    assert any("unknown_key" in w for w in cfg.config_warnings)


def test_from_sources_pyproject_section(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.codeaudit]\nfail_on_severity = "high"\n',
        encoding="utf-8",
    )
    cfg = AuditConfig.from_sources(search_root=tmp_path)
    assert cfg.fail_on_severity == "high"


def test_from_sources_precedence(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "env-key")
    (tmp_path / ".codeaudit.toml").write_text('model = "file-model"\n', encoding="utf-8")
    cfg = AuditConfig.from_sources(
        cli_overrides={"model": "cli-model", "concurrency": 5},
        search_root=tmp_path,
    )
    assert cfg.model == "cli-model"  # CLI > 文件
    assert cfg.concurrency == 5
    assert cfg.api_key == "env-key"  # 环境变量生效
    # CLI 未传的字段回落文件值
    cfg2 = AuditConfig.from_sources(search_root=tmp_path)
    assert cfg2.model == "file-model"


def test_from_sources_explicit_file_missing(tmp_path: Path):
    cfg = AuditConfig.from_sources(config_file=str(tmp_path / "nope.toml"))
    assert any("不存在" in w for w in cfg.config_warnings)


def test_issue_fingerprint_stable_and_distinct():
    a = _make()
    assert issue_fingerprint(a) == issue_fingerprint(_make())
    # 描述/置信度变化不影响指纹
    assert issue_fingerprint(_make(description="x", confidence=0.5)) == issue_fingerprint(a)
    assert issue_fingerprint(_make(line_start=12)) != issue_fingerprint(a)
    assert issue_fingerprint(_make(title="别的")) != issue_fingerprint(a)


def test_report_schema_version_roundtrip():
    from audit.models import AuditReport, AuditStats

    r = AuditReport(audit_id="x")
    assert r.schema_version == "1.0"
    assert AuditReport.from_dict(r.to_dict()).schema_version == "1.0"
    s = AuditStats(suppressed=7)
    assert AuditStats.from_dict(s.to_dict()).suppressed == 7
