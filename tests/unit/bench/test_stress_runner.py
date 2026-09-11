"""bench.stress.run_stress 单测：纯函数指标计算 / R1-8 预取原型等价性 / 报告渲染。"""

from __future__ import annotations

import pytest

from bench.stress.generator import generate_project
from bench.stress.run_stress import (
    LLM_CONCURRENCY,
    LLM_LATENCY_SEC,
    ScenarioResult,
    _has_done_event,
    _PrefetchedIndexStore,
    budget_verdict,
    expansion_stats,
    llm_scenario_files,
    parse_scale_arg,
    r18_profile_share,
    render_report,
    sec_per_kloc,
)


# ---------------------------------------------------------------- 纯函数


def test_sec_per_kloc_docs04_caliber():
    assert sec_per_kloc(10.0, 5000) == pytest.approx(2.0)  # 10s / 5 KLOC
    assert sec_per_kloc(1.0, 1) == pytest.approx(1000.0)  # 1 行 = 0.001 KLOC
    assert sec_per_kloc(1.0, 0) is None
    assert sec_per_kloc(1.0, -3) is None


def test_expansion_stats_perfect_concurrency():
    stats = expansion_stats(200, 8, 0.2, 5.0)
    assert stats["ideal_wall_sec"] == pytest.approx(5.0)  # ceil(200/8)*0.2
    assert stats["serial_wall_sec"] == pytest.approx(40.0)
    assert stats["expansion_factor"] == pytest.approx(8.0)
    assert stats["wall_over_ideal"] == pytest.approx(1.0)


def test_expansion_stats_degenerate_inputs():
    stats = expansion_stats(10, 8, 0.2, 0.0)
    assert stats["expansion_factor"] == 0.0
    assert stats["wall_over_ideal"] == 0.0


def test_parse_scale_arg():
    assert parse_scale_arg("500") == [500]
    assert parse_scale_arg(" 2000 ") == [2000]
    assert parse_scale_arg("all") == [500, 2000]
    assert parse_scale_arg("ALL") == [500, 2000]
    with pytest.raises(ValueError):
        parse_scale_arg("0")
    with pytest.raises(ValueError):
        parse_scale_arg("-4")
    with pytest.raises(ValueError):
        parse_scale_arg("abc")


def test_llm_scenario_files_scales():
    assert llm_scenario_files(500) == 200
    assert llm_scenario_files(2000) == 200
    assert llm_scenario_files(50) == 50
    assert llm_scenario_files(10) == 40  # 下限 40，保 sleep 阶段仍有意义
    assert LLM_CONCURRENCY == 8 and LLM_LATENCY_SEC == 0.2  # 任务书约定参数


def test_budget_verdict_tripped_and_not():
    tripped = budget_verdict(True, llm_calls=51, n_files=50, review_errors=50, rule_issues=2, llm_issues=0)
    assert tripped["budget_tripped"] is True
    assert tripped["partial_results"] is True
    # llm_calls 远超文件数 → 未熔断（预算充足，逐文件多轮）
    rich = budget_verdict(True, llm_calls=600, n_files=50, review_errors=0, rule_issues=2, llm_issues=3)
    assert rich["budget_tripped"] is False
    # 任务崩溃 → 无论证据如何都不算通过
    crashed = budget_verdict(False, llm_calls=50, n_files=50, review_errors=50, rule_issues=2, llm_issues=0)
    assert crashed["budget_tripped"] is False
    assert crashed["audit_completed"] is False


def test_r18_profile_share():
    rows = {
        "_resolve_edges": {"ncalls": 1.0, "tottime": 0.5, "cumtime": 2.0},
        "_language_of": {"ncalls": 24753.0, "tottime": 0.2, "cumtime": 0.4},
        "_aliases": {"ncalls": 24753.0, "tottime": 0.3, "cumtime": 0.6},
    }
    share = r18_profile_share(rows)
    assert share["n_plus_one_cum_sec"] == pytest.approx(1.0)
    assert share["n_plus_one_share"] == pytest.approx(0.5)
    assert share["language_of_calls"] == 24753
    empty = r18_profile_share({})
    assert empty["n_plus_one_share"] is None


def test_has_done_event_stage_or_type():
    assert _has_done_event([{"type": "progress", "stage": "done"}])
    assert _has_done_event([{"type": "done"}])
    assert not _has_done_event([{"type": "progress", "stage": "detect"}])
    assert not _has_done_event([])


# ---------------------------------------------------------------- R1-8 预取原型等价性


def test_prefetched_store_builds_identical_edges(tmp_path):
    """预取原型只改查询方式，不改语义：边数与解析边数必须与 base 完全一致。"""
    from audit.indexer import create_index
    from audit.ingest import ingest

    proj = generate_project(tmp_path / "proj", files=12)
    ws = ingest(str(proj), tmp_path / "ws")

    base = create_index(ws, tmp_path / "base.db")
    base.build()
    base_stats = dict(base.stats())
    base.close()

    proto = _PrefetchedIndexStore(ws, tmp_path / "proto.db")
    proto.build()
    proto_stats = dict(proto.stats())
    proto.close()

    assert proto_stats["call_edges"] == base_stats["call_edges"]
    assert proto_stats["resolved_edges"] == base_stats["resolved_edges"]
    assert proto_stats["call_edges"] > 0  # 12 文件也有真实跨文件调用边


# ---------------------------------------------------------------- 报告渲染（纯内存，不跑审计）


def _fake_scale_results() -> dict[int, dict[str, ScenarioResult]]:
    ingest_index = ScenarioResult(
        name="ingest_index",
        metrics={"files": 500, "symbols": 4397, "call_edges": 24752, "ingest_sec": 2.6,
                 "index_build_sec": 3.7, "db_size_mb": 7.1, "peak_memory_mb": 10.6},
    )
    rules = ScenarioResult(
        name="rules_audit",
        metrics={"wall_sec": 13.4, "loc": 51972, "sec_per_kloc": 0.257, "rule_hits": 11, "issues": 11},
    )
    budget = ScenarioResult(
        name="budget_circuit_breaker",
        metrics={"audit_completed": True, "budget_tripped": True, "llm_calls": 501,
                 "py_files": 500, "review_errors": 500, "rule_issues": 11, "llm_issues": 0,
                 "calls_per_file": 1.002, "done_event": True, "partial_results": True},
    )
    r18 = ScenarioResult(
        name="r18_quantify",
        metrics={"call_edges": 24752, "base_build_best3_sec": 3.7,
                 "prefetch_build_best3_sec": 3.3, "measured_saving_pct": 10.8,
                 "resolve_edges_cum_sec": 1.0, "n_plus_one_share_of_resolve": 0.43,
                 "prefetch_edges_identical": True},
    )
    return {500: {"ingest_index": ingest_index, "rules_audit": rules,
                  "budget_circuit_breaker": budget, "r18_quantify": r18}}


def _fake_shared() -> dict[str, ScenarioResult]:
    llm = ScenarioResult(
        name="llm_concurrency",
        metrics=expansion_stats(200, LLM_CONCURRENCY, LLM_LATENCY_SEC, 5.2) | {"review_errors": 0},
    )
    server = ScenarioResult(
        name="server_concurrency",
        metrics={"tasks": 5, "all_done": True, "no_crosstalk": True, "total_wall_sec": 6.2,
                 "tasks_detail": []},
    )
    return {"llm_concurrency": llm, "server_concurrency": server}


def test_render_report_structure_and_conclusions():
    from bench.stress.run_stress import env_header

    meta = env_header([500])
    markdown = render_report(meta, _fake_scale_results(), _fake_shared())
    for header in (
        "# 压力测试与性能基线报告",
        "## 环境",
        "## 摘要",
        "## 场景明细：500 文件档",
        "### 场景 1（500）：ingest + index",
        "### 场景 2（500）：纯规则审计吞吐",
        "### 场景 4（500）：预算熔断",
        "### 场景 6（500）：R1-8 索引 N+1 量化",
        "## 共享场景",
        "### 场景 3：LLM 并发扩展性",
        "### 场景 5：server 并发",
        "## 结论",
        "**吞吐结论**",
        "**LLM 并发扩展**",
        "**预算熔断**",
        "**R1-8 量化**",
        "**server 并发**",
        "**瓶颈 Top3**",
    ):
        assert header in markdown, f"报告缺少小节：{header}"
    assert "| 500 |" in markdown  # 摘要表分档行
    assert "满足 docs/04 §3.2" in markdown  # 吞吐达标结论由数据驱动


def test_render_report_tolerates_skip_and_error():
    from bench.stress.run_stress import env_header

    results = _fake_scale_results()
    results[500]["r18_quantify"] = ScenarioResult(
        name="r18_quantify", status="skip", notes=["调用边不足"])
    results[500]["budget_circuit_breaker"] = ScenarioResult(
        name="budget_circuit_breaker", status="error", error="boom")
    markdown = render_report(env_header([500]), results, {})
    assert "SKIP" in markdown and "ERROR" in markdown and "boom" in markdown
    assert "**R1-8 量化**" not in markdown  # skip/error 档不进结论


def test_scenario_result_badge():
    assert ScenarioResult(name="x").badge == "OK"
    assert ScenarioResult(name="x", status="skip").badge == "SKIP"
    assert ScenarioResult(name="x", status="error").badge == "ERROR"
