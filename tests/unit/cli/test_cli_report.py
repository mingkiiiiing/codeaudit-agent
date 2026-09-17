"""W2-A6 CLI 自测：report 子命令（json → md/html）、serve --help、run 新参数透传。

不触网、不启动真实服务；run 通过 monkeypatch 注入假流水线捕获 AuditConfig。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import cli
from audit.models import AuditReport

FIXTURE_REPORT = Path(__file__).resolve().parent / "fixtures" / "report.json"


# ---------------------------------------------------------------- report 子命令


def test_fixture_report_is_loadable():
    """fixture 本身可被 AuditReport.from_dict 还原（保护测试数据）。"""
    data = json.loads(FIXTURE_REPORT.read_text(encoding="utf-8"))
    report = AuditReport.from_dict(data)
    assert report.audit_id == "fixt0001" and len(report.issues) == 3 and len(report.patches) == 1


def test_report_markdown_to_stdout(capsys):
    rc = cli.main(["report", str(FIXTURE_REPORT)])
    assert rc == 0
    out = capsys.readouterr().out
    # 小节标题齐全（W7-A2：新增「六、重构方案」章节，原六/七顺延为七/八）
    assert "# 代码审计报告：fixture_proj" in out
    for title in ("## 一、项目概览", "## 二、健康分", "## 三、问题总表", "## 四、重点问题详情", "## 六、重构方案", "## 七、Patch 与测试统计"):
        assert title in out, title
    assert "76.4 / 100" in out
    assert "ISS-0001" in out and "FIX-0001" in out


def test_report_html_written_to_out_file(capsys, tmp_path):
    out_file = tmp_path / "nested" / "report.html"
    rc = cli.main(["report", str(FIXTURE_REPORT), "--format", "html", "--out", str(out_file)])
    assert rc == 0
    assert out_file.exists()
    html = out_file.read_text(encoding="utf-8")
    assert "<html" in html and "ISS-0001" in html and "fixture_proj" in html
    stdout = capsys.readouterr().out
    assert "报告已写入" in stdout and "html" in stdout


def test_report_md_written_to_out_file(tmp_path):
    out_file = tmp_path / "report.md"
    assert cli.main(["report", str(FIXTURE_REPORT), "--format", "md", "--out", str(out_file)]) == 0
    assert "## 二、健康分" in out_file.read_text(encoding="utf-8")


def test_report_missing_file_exits_1(capsys, tmp_path):
    rc = cli.main(["report", str(tmp_path / "nope.json")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "[错误]" in err and "不存在" in err


def test_report_invalid_json_exits_1(capsys, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    rc = cli.main(["report", str(bad)])
    assert rc == 1
    assert "合法 JSON" in capsys.readouterr().err


def test_report_rejects_unknown_format():
    with pytest.raises(SystemExit) as exc:
        cli.main(["report", str(FIXTURE_REPORT), "--format", "pdf"])
    assert exc.value.code == 2  # argparse choices 校验


# ---------------------------------------------------------------- serve 子命令


def test_serve_help_lists_host_and_port(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--host" in out and "--port" in out
    assert "127.0.0.1" in out and "8000" in out


def test_serve_invokes_uvicorn_with_create_app(monkeypatch, capsys):
    """serve 把 create_app() 实例交给 uvicorn.run（uvicorn 被替身替换，不真正监听）。"""
    import uvicorn

    captured: dict = {}

    def fake_run(app, host, port, **kwargs):
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr(uvicorn, "run", fake_run)
    # W22-D：非回环绑定要求 token（本用例焦点在 uvicorn 实例路径，补 token 保持原意图）
    monkeypatch.setenv("CODEAUDIT_API_TOKEN", "test-token")
    rc = cli.main(["serve", "--host", "0.0.0.0", "--port", "9001"])
    assert rc == 0
    assert captured["host"] == "0.0.0.0" and captured["port"] == 9001
    assert captured["app"].title == "CodeAudit Agent"  # FastAPI 实例
    out = capsys.readouterr().out
    assert "http://0.0.0.0:9001" in out


def test_top_level_help_lists_all_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for cmd in ("run", "index", "report", "serve"):
        assert cmd in out
