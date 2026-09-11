"""bench.run 单元测试：不依赖 orchestrator 是否真实存在（用 sys.modules 屏蔽模拟缺失）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bench.goldset import GoldenIssue
from bench.run import build_goldset, main, render_result_markdown, run_bench


@pytest.fixture
def no_orchestrator(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制 audit.orchestrator 导入失败，模拟 T5 未集成的环境（确定性）。"""
    monkeypatch.setitem(sys.modules, "audit.orchestrator", None)
    monkeypatch.setitem(sys.modules, "audit.orchestrator.pipeline", None)


def _one_golden() -> list[GoldenIssue]:
    return [
        GoldenIssue(
            project="p", file="a.py", line_start=1, line_end=1,
            category="bug", severity="high", description="G",
        )
    ]


def test_run_bench_raises_runtime_error_without_orchestrator(tmp_path: Path, no_orchestrator):
    with pytest.raises(RuntimeError, match="orchestrator 未集成"):
        run_bench([tmp_path], _one_golden())


def test_cli_help_runs():
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_cli_missing_projects_is_usage_error():
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_cli_exit_code_2_without_orchestrator(tmp_path: Path, no_orchestrator, capsys):
    rc = main(["--projects", str(tmp_path)])
    assert rc == 2
    captured = capsys.readouterr()
    assert "orchestrator" in captured.err


def test_cli_missing_project_path_returns_1(capsys):
    rc = main(["--projects", "Z:/definitely/not/exists_dir_xyz"])
    assert rc == 1
    assert "不存在" in capsys.readouterr().err


def test_build_goldset_default_demo_proj():
    goldens = build_goldset(None)
    assert len(goldens) == 12
    assert all(g.project == "demo_proj" for g in goldens)


def test_build_goldset_jsonl(tmp_path: Path):
    path = tmp_path / "g.jsonl"
    path.write_text(
        '{"project":"p","file":"a.py","line_start":1,"line_end":2,'
        '"category":"bug","severity":"high","description":"d","origin":"manual"}\n',
        encoding="utf-8",
    )
    goldens = build_goldset(path)
    assert len(goldens) == 1
    assert goldens[0].file == "a.py"


def test_render_result_markdown_from_synthetic_result():
    result = {
        "generated_at": "2026-09-11T00:00:00+00:00",
        "level": "critical+high",
        "projects": [
            {
                "project": "p",
                "path": "x",
                "duration_sec": 1.0,
                "loc": 1000,
                "issue_count": 1,
                "severity_summary": {"high": 1},
                "issues": [],
            }
        ],
        "detection": {
            "level": "critical+high",
            "precision": 0.5,
            "recall": None,
            "f1": None,
            "counts": {"reports_total": 1},
        },
        "timing": {"n": 1, "sec_per_kloc_p50": 1.0, "sec_per_kloc_p90": 1.0, "sec_per_kloc_mean": 1.0, "items": []},
        "cost": {"kloc": 1.0, "cache_hit_ratio": None},
        "fix": {"total": 0, "syntax_ok_ratio": None, "verified_ratio": None, "status_counts": {}},
        "unmatched_goldens": ["G9 未命中描述"],
    }
    md = render_result_markdown(result)
    assert md.startswith("# Bench 运行结果")
    assert "precision" in md
    assert "N/A" in md  # None 值渲染
    assert "| p | 1000 | 1.00 | 1.00 | 1 |" in md  # 分项目明细行
    assert "G9 未命中描述" in md
