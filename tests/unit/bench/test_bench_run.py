"""bench.run 单元测试：不依赖 orchestrator 是否真实存在（用 sys.modules 屏蔽模拟缺失）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bench.goldset import GoldenIssue
from bench.run import (
    DEFAULT_GOLDSET_JSONL,
    build_goldset,
    main,
    render_result_markdown,
    run_bench,
)


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


# ---------------------------------------------------------------- CLI 缺省金标与 --offline


def _synthetic_cli_result(n_projects: int) -> dict:
    """让 main 走完渲染/落盘全流程的最小 result dict（不真跑审计）。"""
    return {
        "generated_at": "",
        "level": "critical+high",
        "projects": [
            {
                "project": f"p{i}",
                "path": "x",
                "duration_sec": 1.0,
                "loc": 1000,
                "issue_count": 0,
                "severity_summary": {},
                "issues": [],
            }
            for i in range(n_projects)
        ],
        "detection": {
            "level": "critical+high",
            "precision": 1.0,
            "recall": 0.5,
            "f1": 2.0 / 3.0,
            "counts": {"reports_total": 0, "goldens_total": 240},
        },
        "timing": {"n": 1, "sec_per_kloc_p50": 1.0, "sec_per_kloc_p90": 1.0, "sec_per_kloc_mean": 1.0, "items": []},
        "cost": {"kloc": 1.0, "cache_hit_ratio": None},
        "fix": {"total": 0, "syntax_ok_ratio": None, "verified_ratio": None, "status_counts": {}},
        "unmatched_goldens": [],
    }


def _patch_run_bench(monkeypatch: pytest.MonkeyPatch, captured: dict):
    def fake_run_bench(project_paths, goldens, config_overrides=None, level="critical+high"):
        captured["goldens"] = goldens
        captured["overrides"] = config_overrides
        captured["level"] = level
        return _synthetic_cli_result(len(project_paths))

    monkeypatch.setattr("bench.run.run_bench", fake_run_bench)


def test_cli_default_goldset_is_official_240(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """--goldset 缺省解析为仓库根锚定的 goldset.jsonl（240 条），任意 CWD 均正确。"""
    monkeypatch.chdir(tmp_path)  # 换到任意 CWD，验证锚定不受工作目录影响
    assert DEFAULT_GOLDSET_JSONL.is_absolute()
    assert DEFAULT_GOLDSET_JSONL.name == "goldset.jsonl"
    assert DEFAULT_GOLDSET_JSONL.exists()

    captured: dict = {}
    _patch_run_bench(monkeypatch, captured)
    out = tmp_path / "out.md"
    rc = main(["--projects", str(tmp_path), "--out", str(out)])
    assert rc == 0
    assert len(captured["goldens"]) == 240
    assert out.exists()


def test_cli_offline_passes_empty_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """--offline 时 run_bench 收到 api_key='' 的 config_overrides。"""
    captured: dict = {}
    _patch_run_bench(monkeypatch, captured)
    rc = main(["--projects", str(tmp_path), "--offline", "--out", str(tmp_path / "out.md")])
    assert rc == 0
    assert captured["overrides"] == {"api_key": ""}


def test_cli_without_offline_keeps_overrides_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """不加 --offline 保持现行为：config_overrides 为 None（在线可选）。"""
    captured: dict = {}
    _patch_run_bench(monkeypatch, captured)
    rc = main(["--projects", str(tmp_path), "--out", str(tmp_path / "out.md")])
    assert rc == 0
    assert captured["overrides"] is None


def test_cli_offline_applies_to_ablation_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """--offline 时消融也以 api_key='' 为底层覆盖，避免 LLM 通道混入离线评测。"""
    captured: dict = {}

    def fake_ablation(project_paths, goldens, level="critical+high", names=None, base_overrides=None):
        captured["base_overrides"] = base_overrides
        return {"level": level, "rows": []}

    monkeypatch.setattr("bench.run.run_ablation", fake_ablation)
    _patch_run_bench(monkeypatch, captured)
    rc = main(["--projects", str(tmp_path), "--offline", "--ablation", "--out", str(tmp_path / "out.md")])
    assert rc == 0
    assert captured["base_overrides"] == {"api_key": ""}
