"""审计 P0-5：测试覆盖盲区（untested_hotspots）计算与渲染自测。

覆盖：有触达豁免 / 无触达入清单 / 测试文件问题豁免 / 无 critical-high 空节 /
占比计算 / methodology 与 resolved_ratio / 私有符号不豁免 / 索引缺失降级 /
TOP 20 截断 / 三格式渲染含节 / 空节结构稳定 / ctx.extra 通道。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from audit.config import AuditConfig
from audit.indexer import create_index
from audit.llm.base import FakeLLMClient
from audit.models import Category, Issue, IssueSource, Severity
from audit.pipeline import PipelineContext
from audit.report.builder import build_report
from audit.report.render import render_html, render_markdown
from audit.report.testcoverage import compute_untested_hotspots
from audit.workspace import WorkspaceContext

ORDERS = "app/orders.py"
BILLING = "app/billing.py"
PRIVATE = "app/private_mod.py"
TEST_FILE = "tests/test_orders.py"

_FILES: dict[str, str] = {
    "app/__init__.py": "",
    ORDERS: "def create_order(conn, item):\n    return conn, item\n",
    BILLING: (
        "def charge(amount):\n"
        "    return amount * 2\n"
        "\n"
        "\n"
        "def refund(amount):\n"
        "    return -amount\n"
    ),
    PRIVATE: "def _helper(x):\n    return x + 1\n",
    TEST_FILE: (
        "from app.orders import create_order\n"
        "from app.private_mod import _helper\n"
        "\n"
        "\n"
        "def test_create_order():\n"
        "    assert create_order(None, 1) == (None, 1)\n"
        "\n"
        "\n"
        "def test_private_helper():\n"
        "    assert _helper(1) == 2\n"
    ),
}


@pytest.fixture
def coverage_ctx(tmp_path: Path, fake_emitter) -> PipelineContext:
    """小型覆盖样例：orders 被测试触达；billing 无触达；private_mod 仅私有符号被测试调用。"""
    src = tmp_path / "src"
    for rel, text in _FILES.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    ws = WorkspaceContext(
        audit_id="p50001", src_root=src, work_root=tmp_path, db_path=tmp_path / "index.db"
    )
    store = create_index(ws)
    store.build()
    config = AuditConfig(source_path=str(src))
    ctx = PipelineContext(config=config, workspace=ws, llm=FakeLLMClient(), emitter=fake_emitter)
    ctx.index = store
    yield ctx
    store.close()


def make_issue(file: str, severity: Severity = Severity.CRITICAL, **kw: Any) -> Issue:
    base: dict[str, Any] = dict(
        id="ISS-P5",
        category=Category.BUG,
        severity=severity,
        title="缺陷",
        file=file,
        line_start=1,
        confidence=0.9,
        source=IssueSource.RULE,
    )
    base.update(kw)
    return Issue(**base)


# ---------------------------------------------------------------- 计算


def test_reached_file_not_listed(coverage_ctx: PipelineContext):
    """orders.py 的 create_order 被测试文件调用 → 不入盲区清单；billing 入清单。"""
    coverage_ctx.issues = [
        make_issue(ORDERS, Severity.CRITICAL),
        make_issue(BILLING, Severity.HIGH),
    ]
    result = compute_untested_hotspots(coverage_ctx)
    assert result["available"] is True
    files = [it["file"] for it in result["items"]]
    assert files == [BILLING]
    assert result["eligible_files"] == 2
    assert result["untested_files"] == 1


def test_unreachable_file_lists_public_symbols(coverage_ctx: PipelineContext):
    """盲区清单项携带公开符号与问题聚合信息。"""
    coverage_ctx.issues = [make_issue(BILLING, Severity.HIGH, id="ISS-B1")]
    result = compute_untested_hotspots(coverage_ctx)
    item = result["items"][0]
    assert item["file"] == BILLING
    assert set(item["public_symbols"]) == {"charge", "refund"}
    assert item["public_symbol_count"] == 2
    assert item["issue_ids"] == ["ISS-B1"]
    assert item["severities"] == ["high"]
    assert item["issue_count"] == 1


def test_issue_in_test_file_is_exempt(coverage_ctx: PipelineContext):
    """测试文件自身的问题不进 eligible（测试文件豁免口径）。"""
    coverage_ctx.issues = [make_issue(TEST_FILE, Severity.CRITICAL)]
    result = compute_untested_hotspots(coverage_ctx)
    assert result["available"] is True
    assert result["eligible_files"] == 0
    assert result["items"] == []
    assert result["ratio"] == 0.0


def test_no_critical_high_yields_empty_section(coverage_ctx: PipelineContext):
    """无 critical/high 问题 → 空节（结构稳定，available 仍为 True）。"""
    coverage_ctx.issues = [
        make_issue(ORDERS, Severity.MEDIUM),
        make_issue(BILLING, Severity.LOW),
    ]
    result = compute_untested_hotspots(coverage_ctx)
    assert result["available"] is True
    assert result["eligible_files"] == 0
    assert result["untested_files"] == 0
    assert result["items"] == []
    assert result["ratio"] == 0.0


def test_ratio_is_untested_over_eligible(coverage_ctx: PipelineContext):
    """占比 = 盲区数 / eligible 数：3 个 eligible、1 个有触达 → 2/3。"""
    coverage_ctx.issues = [
        make_issue(ORDERS, Severity.CRITICAL),  # 有触达
        make_issue(BILLING, Severity.CRITICAL),  # 盲区
        make_issue(PRIVATE, Severity.HIGH),  # 盲区
    ]
    result = compute_untested_hotspots(coverage_ctx)
    assert result["eligible_files"] == 3
    assert result["untested_files"] == 2
    assert result["ratio"] == round(2 / 3, 4)


def test_methodology_and_resolved_ratio(coverage_ctx: PipelineContext):
    """methodology 说明口径与 resolved 局限；resolved_ratio 取自索引 stats。"""
    coverage_ctx.issues = [make_issue(BILLING)]
    result = compute_untested_hotspots(coverage_ctx)
    assert "口径" in result["methodology"]
    assert "偏乐观" in result["methodology"]
    assert "resolved_ratio" in result["methodology"]
    assert result["resolved_ratio"] == coverage_ctx.index.stats()["resolved_ratio"]
    assert result["top_limit"] == 20


def test_private_symbol_called_by_tests_still_untested(coverage_ctx: PipelineContext):
    """仅私有符号（'_' 前缀）被测试调用不构成触达：公开符号集为空 → 仍盲区。"""
    coverage_ctx.issues = [make_issue(PRIVATE, Severity.CRITICAL)]
    result = compute_untested_hotspots(coverage_ctx)
    item = result["items"][0]
    assert item["file"] == PRIVATE
    assert item["public_symbol_count"] == 0
    assert item["public_symbols"] == []


def test_index_missing_degrades(coverage_ctx: PipelineContext):
    """索引不可用（ctx.index 与 workspace.index 均为 None）→ available=False 空节。"""
    coverage_ctx.issues = [make_issue(BILLING)]
    coverage_ctx.index = None
    coverage_ctx.workspace.index = None
    result = compute_untested_hotspots(coverage_ctx)
    assert result["available"] is False
    assert result["items"] == []
    assert "索引不可用" in result["methodology"]


def test_top_limit_truncation(tmp_path: Path, fake_emitter):
    """超过 TOP 20 的盲区只展示 20 条，其余计入 omitted。"""
    src = tmp_path / "src"
    issues: list[Issue] = []
    for i in range(25):
        rel = f"app/mod_{i:02d}.py"
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(f"def unique_func_{i}(x):\n    return x + {i}\n", encoding="utf-8")
        issues.append(make_issue(rel, Severity.HIGH, id=f"ISS-{i:02d}"))
    ws = WorkspaceContext(
        audit_id="p50002", src_root=src, work_root=tmp_path, db_path=tmp_path / "index.db"
    )
    store = create_index(ws)
    store.build()
    try:
        config = AuditConfig(source_path=str(src))
        ctx = PipelineContext(config=config, workspace=ws, llm=FakeLLMClient(), emitter=fake_emitter)
        ctx.index = store
        ctx.issues = issues
        result = compute_untested_hotspots(ctx)
        assert result["eligible_files"] == 25
        assert result["untested_files"] == 25
        assert len(result["items"]) == 20
        assert result["omitted"] == 5
        assert result["ratio"] == 1.0
    finally:
        store.close()


# ---------------------------------------------------------------- 渲染


def test_render_all_three_formats_contain_section(coverage_ctx: PipelineContext):
    """JSON / Markdown / HTML 三格式均输出该节（build_report 接线 + 渲染）。"""
    coverage_ctx.issues = [
        make_issue(ORDERS, Severity.CRITICAL),
        make_issue(BILLING, Severity.HIGH, id="ISS-B1"),
    ]
    report = build_report(coverage_ctx)
    # JSON：模型字段进 to_dict
    payload = json.dumps(report.to_dict(), ensure_ascii=False)
    assert report.untested_hotspots is not None
    assert report.untested_hotspots["available"] is True
    assert "untested_hotspots" in payload
    assert BILLING in payload
    # extra 通道（对齐 verify_stats 模式）
    assert coverage_ctx.extra["untested_hotspots"] is report.untested_hotspots
    # Markdown / HTML
    md = render_markdown(report)
    assert "## 八、测试覆盖盲区" in md
    assert BILLING in md and "charge" in md
    assert "resolved_ratio" in md  # 诚实口径标注
    assert "## 十、耗时与 Token 统计" in md  # 原第八节顺延
    html = render_html(report)
    assert "八、测试覆盖盲区" in html
    assert BILLING in html and "refund" in html


def test_render_empty_section_structure_stable(coverage_ctx: PipelineContext):
    """空清单也输出节（结构稳定）：标题存在 + 未发现文案，原章节号不变。"""
    coverage_ctx.issues = [make_issue(ORDERS, Severity.LOW)]
    report = build_report(coverage_ctx)
    assert report.untested_hotspots is not None
    assert report.untested_hotspots["untested_files"] == 0
    md = render_markdown(report)
    assert "## 八、测试覆盖盲区" in md
    assert "本次未发现测试覆盖盲区" in md
    assert "## 十、耗时与 Token 统计" in md
    html = render_html(report)
    assert "<h2>八、测试覆盖盲区</h2>" in html
    assert "本次未发现测试覆盖盲区" in html


def test_render_index_unavailable_section(coverage_ctx: PipelineContext):
    """索引不可用时渲染降级文案（不渲染空表格）。"""
    coverage_ctx.issues = [make_issue(BILLING)]
    coverage_ctx.index = None
    coverage_ctx.workspace.index = None
    report = build_report(coverage_ctx)
    assert report.untested_hotspots["available"] is False
    md = render_markdown(report)
    assert "本节未计算（索引不可用或未建索引）" in md
    html = render_html(report)
    assert "本节未计算" in html
