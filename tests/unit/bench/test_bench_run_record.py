"""bench.run 扩展件单元测试：write_run_record / run_ablation / sample_projects。

write_run_record 按 docs/04 §5 模板产出 run 记录（头部日期/model/prompt_version/
配置 + 指标表 + 备注节）；run_ablation 对可表达配置（full / rules_only）真实执行
run_bench（本测试强制离线），占位配置跳过并表注"待接入"。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from audit.config import DEFAULT_MODEL

from bench.run import (
    build_goldset,
    render_ablation_table,
    render_result_markdown,
    run_ablation,
    sample_projects,
    write_run_record,
)

ROOT = Path(__file__).resolve().parents[3]
DEMO_PROJ = ROOT / "tests" / "samples" / "demo_proj"
MINUS = "\u2212"  # 消融配置名中的减号（U+2212）


def _synthetic_result() -> dict[str, Any]:
    """与 run_bench 输出同构的最小结果 dict（无需 orchestrator）。"""
    return {
        "level": "critical+high",
        "projects": [
            {
                "project": "p",
                "path": "x",
                "duration_sec": 1.0,
                "loc": 1000,
                "issue_count": 2,
                "severity_summary": {"high": 2},
                "issues": [],
            }
        ],
        "detection": {
            "level": "critical+high",
            "precision": 0.86,
            "recall": 0.63,
            "f1": 0.73,
            "counts": {
                "reports_total": 78,
                "reports_in_level": 50,
                "reports_matched": 43,
                "goldens_total": 63,
                "goldens_in_level": 63,
                "goldens_matched": 40,
            },
        },
        "timing": {
            "n": 1,
            "sec_per_kloc_p50": 24.0,
            "sec_per_kloc_p90": 41.0,
            "sec_per_kloc_mean": 30.0,
            "items": [],
        },
        "cost": {
            "kloc": 1.0,
            "prompt_tokens_per_kloc": 58000.0,
            "completion_tokens_per_kloc": 4000.0,
            "cache_hit_ratio": None,
        },
        "fix": {"total": 0, "syntax_ok_ratio": None, "verified_ratio": None, "status_counts": {}},
        "unmatched_goldens": [],
    }


# ---------------------------------------------------------------- write_run_record


def test_write_run_record_contains_required_sections(tmp_path: Path) -> None:
    """产出含必需小节的 md：头部（日期/model/prompt_version/配置）+ 指标表 + 备注。"""
    out = write_run_record(
        _synthetic_result(),
        tmp_path / "nested" / "run.md",
        meta={
            "model": "glm-5.3-flash",
            "prompt_version": "v7",
            "config": "full",
            "commit": "abc1234",
            "notes": ["prompt v7 将 severity 定义前置", "本机 CPU/并发=8"],
        },
    )
    md = out.read_text(encoding="utf-8")
    assert md.startswith("## Run ")
    assert "model=glm-5.3-flash" in md
    assert "prompt_ver=v7" in md
    assert "配置=full" in md
    assert "commit=abc1234" in md
    assert "| Precision (critical+high) | 0.860 | 报告 50 条 / 金标 63 条 |" in md
    assert "| Recall (critical+high) | 0.630 | 金标 63 条 |" in md
    assert "| F1 (critical+high) | 0.730 | — |" in md
    assert "| 耗时 P50 / P90 | 24.0 / 41.0 s/KLOC | 1 项目 |" in md
    assert "| tokens/KLOC | 58k prompt + 4.0k completion | 1 项目 |" in md
    assert "## 备注" in md
    assert "- prompt v7 将 severity 定义前置" in md
    assert "- 本机 CPU/并发=8" in md


def test_write_run_record_defaults_and_dir_out_with_ablation(tmp_path: Path) -> None:
    """meta 缺省（prompt_ver=v2 / model=DEFAULT_MODEL / 配置=full）；out 为目录时自动命名；
    result 带 ablation 时附消融对比小节；None 指标渲染 N/A。"""
    result = _synthetic_result()
    result["detection"]["precision"] = None
    result["cost"]["prompt_tokens_per_kloc"] = None
    result["cost"]["completion_tokens_per_kloc"] = None
    result["ablation"] = {
        "level": "critical+high",
        "rows": [
            {"config": "full", "status": "ok", "note": "", "result": _synthetic_result()},
            {"config": f"{MINUS}cache", "status": "待接入", "note": "关闭两级缓存", "result": None},
        ],
    }
    out_dir = tmp_path / "results"
    out_dir.mkdir()
    out = write_run_record(result, out_dir)  # 目录 → run_YYYYMMDD_HHMMSS.md
    assert out.parent == out_dir
    assert out.name.startswith("run_") and out.suffix == ".md"

    md = out.read_text(encoding="utf-8")
    assert f"model={DEFAULT_MODEL}" in md
    assert "prompt_ver=v2" in md  # meta 未提供时的缺省 prompt 版本
    assert "配置=full" in md
    assert "| Precision (critical+high) | N/A |" in md
    assert "| tokens/KLOC | N/A |" in md
    assert "## 消融对比" in md
    assert f"| {MINUS}cache | — | — | — | — | — | — | 待接入 关闭两级缓存 |" in md


# ---------------------------------------------------------------- run_ablation


def test_run_ablation_two_real_configs_and_placeholder(tmp_path: Path, monkeypatch) -> None:
    """--ablation：契约 v1.3 后 7 组配置全部真实执行，不再有占位。"""
    monkeypatch.delenv("GLM_API_KEY", raising=False)  # 强制离线（FakeLLM 纯规则路径）
    goldens = build_goldset(None)
    ablation = run_ablation(
        [DEMO_PROJ],
        goldens,
        names=["full", "rules_only", f"{MINUS}rule_hints"],
        base_overrides={"api_key": ""},
    )
    rows = {r["config"]: r for r in ablation["rows"]}
    assert rows["full"]["status"] == "ok"
    assert rows["rules_only"]["status"] == "ok"
    # 可表达配置各自带回了完整指标 dict
    assert rows["full"]["result"] is not None
    assert rows["full"]["result"]["detection"]["counts"]["goldens_total"] == 12
    assert rows["rules_only"]["result"] is not None
    # 契约 v1.3：−rule_hints 已真实化（开关接线见 audit/detect/engine.py）
    assert rows[f"{MINUS}rule_hints"]["status"] == "ok"
    assert rows[f"{MINUS}rule_hints"]["result"] is not None

    table = render_ablation_table(ablation)
    assert "| 配置 | Precision | Recall | F1 | 耗时 P50 | 耗时 P90 | tokens/KLOC | 备注 |" in table
    assert "| full |" in table
    assert "| rules_only |" in table
    # 契约 v1.3：−rule_hints 真实执行，渲染为完整指标行
    assert f"| {MINUS}rule_hints |" in table
    assert "待接入" not in table
    # 契约 v1.3 后零占位：表尾不再有"待接入"注记

    # render_result_markdown 对带 ablation 的结果追加消融小节
    result = rows["full"]["result"]
    result["ablation"] = ablation
    assert "## 消融对比" in render_result_markdown(result)


def test_run_ablation_unknown_config_name_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        run_ablation([], [], names=["no_such_config"])


# ---------------------------------------------------------------- sample_projects


def test_sample_projects_even_stride_deterministic() -> None:
    paths = [Path(name) for name in ("a", "b", "c", "d", "e", "f")]
    assert sample_projects(paths, 2) == [Path("a"), Path("d")]
    assert sample_projects(paths, 6) == paths  # n >= len 原样返回
    assert sample_projects(paths, 10) == paths
    assert sample_projects(paths, 1) == [Path("a")]
    assert sample_projects(paths, 0) == []
    assert sample_projects([], 3) == []
