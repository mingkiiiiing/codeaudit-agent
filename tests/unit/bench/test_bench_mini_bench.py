"""bench.mini_bench 单元测试：mini 校准集构建 + 离线快速回归（prompt/规则改动后必跑）。

run_mini_bench 强制离线（enable_llm_review=False + api_key=""，FakeLLM 纯规则路径），
其阈值（P≥0.6 / R≥0.5）是纯规则离线基线的宽松回归线，与 docs/04 §3.1 的正式
阈值（P≥0.85 / R≥0.60，LLM 真跑口径）不同，两者不可混用。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from bench.goldset import load_goldset
from bench.mini_bench import (
    MINI_GOLDENS_TARGET,
    MINI_PASS_PRECISION,
    MINI_PASS_RECALL,
    VARIANT_DIRNAME,
    build_mini_dataset,
    run_mini_bench,
)


def test_build_mini_dataset_into_tmp(tmp_path: Path) -> None:
    """mini 数据集可构建到任意 work_dir：金标 ≥30 条、JSONL 落盘、变体项目就位。"""
    work_dir, goldens = build_mini_dataset(tmp_path / "mini")
    assert len(goldens) >= MINI_GOLDENS_TARGET
    assert (work_dir / "goldset.jsonl").is_file()
    assert load_goldset(work_dir / "goldset.jsonl") == goldens
    assert (work_dir / VARIANT_DIRNAME).is_dir()
    # 组合金标 = demo 原有 + 变体继承 + 注入，两个项目名都有金标覆盖
    projects = {g.project for g in goldens}
    assert projects == {"demo_proj", VARIANT_DIRNAME}
    assert {g.origin for g in goldens} >= {"manual", "injected"}


def test_run_mini_bench_offline_pass(monkeypatch) -> None:
    """离线跑 mini 校准集：纯规则基线达到宽松回归阈值（P≥0.6 / R≥0.5）。"""
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    result = asyncio.run(run_mini_bench())
    assert result["precision"] is not None
    assert result["recall"] is not None
    assert result["precision"] >= MINI_PASS_PRECISION
    assert result["recall"] >= MINI_PASS_RECALL
    assert result["pass"] is True
    details = result["details"]
    assert len(details["projects"]) == 2  # demo_proj + mini_variant
