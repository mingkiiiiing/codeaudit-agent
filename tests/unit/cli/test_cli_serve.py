"""W11-A3 CLI 自测：serve --workers 多 worker 启动形态与参数校验。

不触网、不启动真实服务；uvicorn.run 全部经 monkeypatch 替身捕获调用参数。

覆盖点（契约 docs/16 §1.3）：

- --workers <1：中文报错 + 退出码 2，且不触碰 uvicorn.run；
- 默认（不传 --workers）：保持现状实例路径——create_app() 实例 + 不带 workers
  形参（与 W10 及之前行为零变化）；
- --workers 2：uvicorn.run 以 import string ``"server.app:app"`` + workers=2 +
  log_level="info" 启动（实例形式与 workers>1 不兼容），横幅注明实验特性与
  全局并发上限（workers × CODEAUDIT_MAX_RUNNING，默认 4）。
"""

from __future__ import annotations

import pytest

import cli


# ---------------------------------------------------------------- 参数校验


@pytest.mark.parametrize("bad", ["0", "-1", "-3"])
def test_serve_workers_below_one_exits_2(monkeypatch, capsys, bad):
    """--workers <1 属参数错误：退出码 2、中文报错，且不触碰 uvicorn.run。"""
    import uvicorn

    called: list = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: called.append((a, k)))
    rc = cli.main(["serve", "--host", "127.0.0.1", "--port", "8921", "--workers", bad])
    assert rc == 2
    assert not called
    err = capsys.readouterr().err
    assert "[错误]" in err and "--workers" in err and bad in err


# ---------------------------------------------------------------- 默认单 worker（现状不变）


def test_serve_default_runs_app_instance_without_workers_kwarg(monkeypatch, capsys):
    """默认（不传 --workers）保持现状：create_app() 实例交给 uvicorn.run，不带 workers 形参。"""
    import uvicorn

    captured: dict = {}

    def fake_run(app, host, port, **kwargs):
        captured.update(app=app, host=host, port=port, kwargs=kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    rc = cli.main(["serve", "--host", "127.0.0.1", "--port", "8921"])
    assert rc == 0
    assert captured["app"].title == "CodeAudit Agent"  # FastAPI 实例（非 import string）
    assert "workers" not in captured["kwargs"]  # 与现状逐字一致
    assert captured["kwargs"]["log_level"] == "info"
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8921" in out
    assert "worker 数：1" in out


def test_serve_workers_1_same_instance_path(monkeypatch, capsys):
    """显式 --workers 1 与默认等价：仍走实例路径（单 worker 是默认与推荐形态）。"""
    import uvicorn

    captured: dict = {}

    def fake_run(app, host, port, **kwargs):
        captured.update(app=app, kwargs=kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    assert cli.main(["serve", "--workers", "1"]) == 0
    assert captured["app"].title == "CodeAudit Agent"
    assert "workers" not in captured["kwargs"]


# ---------------------------------------------------------------- 多 worker（实验特性）


def test_serve_workers_2_uses_import_string(monkeypatch, capsys):
    """--workers 2 时 uvicorn.run 以 import string "server.app:app" + workers=2 启动。

    W22-D 起非回环绑定要求 token：本用例焦点在 workers 路径，补 token 环境保持原意图。
    """
    import uvicorn

    captured: dict = {}

    def fake_run(app, host, port, **kwargs):
        captured.update(app=app, host=host, port=port, kwargs=kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setenv("CODEAUDIT_API_TOKEN", "test-token")
    rc = cli.main(["serve", "--host", "0.0.0.0", "--port", "8921", "--workers", "2"])
    assert rc == 0
    assert captured["app"] == "server.app:app"  # import string，非实例
    assert captured["host"] == "0.0.0.0" and captured["port"] == 8921
    assert captured["kwargs"]["workers"] == 2
    assert captured["kwargs"]["log_level"] == "info"


def test_serve_workers_2_banner_notes_experimental_and_global_cap(monkeypatch, capsys):
    """多 worker 横幅注明实验特性与全局并发上限（默认口径 workers × 4）。"""
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.delenv("CODEAUDIT_MAX_RUNNING", raising=False)
    assert cli.main(["serve", "--workers", "2"]) == 0
    out = capsys.readouterr().out
    assert "worker 数：2" in out
    assert "实验特性" in out
    assert "2 × 4 = 8" in out  # 全局并发上限 = workers × CODEAUDIT_MAX_RUNNING(默认 4)


def test_serve_workers_banner_reads_max_running_env(monkeypatch, capsys):
    """横幅的全局并发上限按 CODEAUDIT_MAX_RUNNING 环境变量实算（与 server.app 同口径）。"""
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setenv("CODEAUDIT_MAX_RUNNING", "7")
    assert cli.main(["serve", "--workers", "3"]) == 0
    assert "3 × 7 = 21" in capsys.readouterr().out


def test_serve_workers_banner_respects_invalid_max_running_env(monkeypatch, capsys):
    """CODEAUDIT_MAX_RUNNING 非法时横幅回退默认 4（不因打印横幅而崩溃）。"""
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setenv("CODEAUDIT_MAX_RUNNING", "not-a-number")
    assert cli.main(["serve", "--workers", "2"]) == 0
    assert "2 × 4 = 8" in capsys.readouterr().out


# ---------------------------------------------------------------- W22-D 非回环绑定强制 token


def test_serve_loopback_without_token_allowed(monkeypatch, capsys):
    """回环绑定（默认 host）无需 token：行为与 W22-D 之前完全一致。"""
    import uvicorn

    called: list = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: called.append((a, k)))
    monkeypatch.delenv("CODEAUDIT_API_TOKEN", raising=False)
    assert cli.main(["serve", "--host", "127.0.0.1", "--port", "8921"]) == 0
    assert called  # 正常进入 uvicorn.run


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::"])
def test_serve_non_loopback_without_token_rejected(monkeypatch, capsys, host):
    """非回环绑定 && 无 token && 未豁免 → 退出码 1、中文报错、不触碰 uvicorn.run。

    ::1 是回环（IPv6 loopback），单独在下方正向用例覆盖；这里只列真非回环地址。
    """
    import uvicorn

    called: list = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: called.append((a, k)))
    monkeypatch.delenv("CODEAUDIT_API_TOKEN", raising=False)
    rc = cli.main(["serve", "--host", host, "--port", "8921"])
    assert rc == 1
    assert not called
    err = capsys.readouterr().err
    assert "[错误]" in err and "CODEAUDIT_API_TOKEN" in err and host in err


def test_serve_non_loopback_with_token_allowed(monkeypatch, capsys):
    """非回环绑定但配置了 token → 放行（token 生效由 server 侧中间件保证）。"""
    import uvicorn

    called: list = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: called.append((a, k)))
    monkeypatch.setenv("CODEAUDIT_API_TOKEN", "secret-token")
    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "8921"]) == 0
    assert called


def test_serve_non_loopback_allow_insecure_bypasses(monkeypatch, capsys):
    """非回环 && 无 token 但显式 --allow-insecure → 放行并在 stderr 无错误。"""
    import uvicorn

    called: list = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: called.append((a, k)))
    monkeypatch.delenv("CODEAUDIT_API_TOKEN", raising=False)
    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "8921", "--allow-insecure"]) == 0
    assert called
    assert "[错误]" not in capsys.readouterr().err


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "127.8.8.8", "[::1]", ""])
def test_is_loopback_host_accepts_loopback_forms(host):
    """回环判定：localhost / 127.0.0.0/8 任意段 / [::1] / 空值均按回环。"""
    assert cli._is_loopback_host(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.com"])
def test_is_loopback_host_rejects_non_loopback(host):
    """回环判定：unspecified / 私网 / 主机名（非 localhost）均非回环。"""
    assert cli._is_loopback_host(host) is False


# ---------------------------------------------------------------- help


def test_serve_help_lists_workers(capsys):
    """serve --help 展示 --workers 及其默认值说明。"""
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--workers" in out and "实验特性" in out
