"""bench.real_run 单元测试：无 GLM_API_KEY 退出码 2、--dry-run 预检零网络零审计。

真跑分支（需要真实 key 与网络）不在单测覆盖范围，由 W2-周2 真跑评估执行。
"""

from __future__ import annotations

from pathlib import Path

from bench import real_run

ROOT = Path(__file__).resolve().parents[3]
DEMO_PROJ = ROOT / "tests" / "samples" / "demo_proj"


def test_real_run_without_key_exits_2(monkeypatch, capsys, tmp_path: Path) -> None:
    """未配置 GLM_API_KEY（monkeypatch delenv）→ 中文提示 + 退出码 2。"""
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    rc = real_run.main(["--projects", str(tmp_path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "GLM_API_KEY" in err
    assert "--dry-run" in err  # 提示里给出预估成本的无 key 替代路径


def test_real_run_dry_run_without_key_prints_estimate(monkeypatch, capsys) -> None:
    """--dry-run 无 key 也可执行：打印项目清单/金标规模/预估调用与成本，退出 0。"""
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    rc = real_run.main(["--projects", str(DEMO_PROJ), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "项目清单" in out and "demo_proj" in out
    assert "金标规模" in out and "12" in out  # demo_proj 金标 12 条
    assert "critical+high" in out
    assert "预估 LLM 审查调用" in out
    assert "tokens/KLOC" in out
    assert "预估成本" in out


def test_real_run_dry_run_missing_project_returns_1(monkeypatch, capsys) -> None:
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    rc = real_run.main(["--projects", "Z:/definitely/not/exists_xyz", "--dry-run"])
    assert rc == 1
    assert "不存在" in capsys.readouterr().err


def test_real_run_dry_run_invalid_max_projects_exits_2(monkeypatch) -> None:
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    try:
        real_run.main(["--projects", str(DEMO_PROJ), "--dry-run", "--max-projects", "0"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("argparse 用法错误应抛 SystemExit(2)")
