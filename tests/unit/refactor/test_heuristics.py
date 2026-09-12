"""W7-A2 启发式重构建议自测：长函数分解 / 重复模式归并 / 热点拆分 / 循环依赖。

全部离线：ctx 直接 fabricated（issue 手工构造 / 源码写入 tmp 工作副本 /
index 用 FakeIndex 桩），不跑 ingest/index/detect。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from audit.config import AuditConfig
from audit.llm.base import FakeLLMClient
from audit.models import Category, FileManifest, Issue, IssueSource, Severity
from audit.pipeline import PipelineContext
from audit.refactor.heuristics import generate_proposals
from audit.workspace import WorkspaceContext

# ---------------------------------------------------------------- 夹具工具


def _make_ctx(tmp_path: Path, files: dict[str, str]) -> PipelineContext:
    """构造带源文件工作副本与事件收集器的 PipelineContext（无索引）。"""
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    ws = WorkspaceContext(
        audit_id="refact01", src_root=src, work_root=tmp_path / "work", db_path=tmp_path / "i.db"
    )
    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)

    config = AuditConfig(source_path=str(src), enable_llm_review=False)
    ctx = PipelineContext(config=config, workspace=ws, llm=FakeLLMClient(), emitter=emit)
    ctx.extra["_events"] = events  # 供断言（可选）
    return ctx


def _long_function_source(blocks: int = 3, block_lines: int = 8) -> str:
    """生成 >80 行的长函数源码：签名 + docstring + blocks 个空行分隔的职责块。"""
    lines = ["def process_order(order, conn):", '    """处理订单。"""', ""]

    def seg(i: int) -> list[str]:
        return [
            f"    order_id = validate_order(order, {i})",
            f"    user = load_user(order, {i})",
            f"    save_order(conn, order, {i})",
            f"    audit_log(order, {i})",
            f"    notify_user(order, {i})",
            f"    result = finalize(order, {i})",
        ]

    for b in range(blocks):
        for i in range(block_lines):
            lines.extend(seg(b * block_lines + i))
        lines.append("")
    lines.append("    return result")
    return "\n".join(lines) + "\n"


def _rule_issue(rule_id: str, file: str, line_start: int, line_end: int = 0, **kw) -> Issue:
    base = dict(
        id="",
        category=Category.STYLE,
        severity=Severity.MEDIUM,
        title="命中",
        file=file,
        line_start=line_start,
        line_end=line_end or line_start,
        evidence=[f"rule:{rule_id}", f"loc:{file}:{line_start}-{line_end or line_start}"],
        confidence=0.7,
        source=IssueSource.RULE,
    )
    base.update(kw)
    return Issue(**base)


# ---------------------------------------------------------------- 1) 长函数分解


def test_long_function_decompose_produces_steps(tmp_path: Path) -> None:
    """LONG-FUNCTION（medium）命中 → decompose 方案：steps 非空、2~4 个子职责块。"""
    ctx = _make_ctx(tmp_path, {"app/big.py": _long_function_source()})
    issue = _rule_issue(
        "PY-LONG-FUNCTION",
        "app/big.py",
        1,
        len(_long_function_source().splitlines()),
        id="ISS-0001",
        title="函数 `process_order` 共 100 行（第 1~100 行），超过 80 行上限",
    )
    ctx.issues = [issue]

    proposals = generate_proposals(ctx)

    decompose = [p for p in proposals if p.kind == "decompose"]
    assert len(decompose) == 1
    p = decompose[0]
    assert p.id == "REF-0001"
    assert p.target == "app/big.py::process_order"
    assert p.related_issues == ["ISS-0001"]
    assert p.source == "heuristic"
    assert p.steps, "steps 必须非空"
    assert "编排入口" in p.steps[-1]
    # 依据空行切块：应有 ≥2 个「抽取子函数」步骤（不超过 4 块 + 收尾步骤）
    extract_steps = [s for s in p.steps if "抽为独立子函数" in s]
    assert 2 <= len(extract_steps) <= 4
    assert 0.5 <= p.confidence <= 0.8
    assert p.benefits


def test_long_function_requires_medium_severity(tmp_path: Path) -> None:
    """severity 非 medium 的 LONG-FUNCTION 命中不参与分解聚合。"""
    ctx = _make_ctx(tmp_path, {"app/big.py": _long_function_source()})
    issue = _rule_issue(
        "PY-LONG-FUNCTION", "app/big.py", 1, 100, id="ISS-0001", severity=Severity.HIGH
    )
    ctx.issues = [issue]
    assert [p for p in generate_proposals(ctx) if p.kind == "decompose"] == []


def test_long_function_steps_non_empty_even_without_source(tmp_path: Path) -> None:
    """源码不可读（区间越界）时仍给出通用拆分框架，保证 steps 非空。"""
    ctx = _make_ctx(tmp_path, {"app/big.py": "x = 1\n"})
    ctx.issues = [_rule_issue("PY-LONG-FUNCTION", "app/big.py", 1, 120, id="ISS-0001")]
    decompose = [p for p in generate_proposals(ctx) if p.kind == "decompose"]
    assert len(decompose) == 1
    assert decompose[0].steps


# ---------------------------------------------------------------- 2) 重复模式归并


def test_dedup_same_rule_same_file_recipe(tmp_path: Path) -> None:
    """同文件同类规则 ≥3 处命中 → 配方化归并方案，related_issues 关联正确。"""
    ctx = _make_ctx(tmp_path, {})
    ctx.issues = [
        _rule_issue("PY-SQL-INJECTION", "app/dao.py", ln, id=f"ISS-{n:04d}")
        for n, ln in enumerate((10, 22, 35, 48), 1)
    ]
    proposals = [p for p in generate_proposals(ctx) if p.kind == "dedup"]
    assert len(proposals) == 1
    p = proposals[0]
    assert "查询构造器" in p.title  # 配方：SQL 拼接 → query builder
    assert p.target == "app/dao.py"
    assert p.related_issues == [f"ISS-{n:04d}" for n in range(1, 5)]
    assert "盘点命中位置" in p.steps[0]
    assert 0.5 <= p.confidence <= 0.8


def test_dedup_generic_recipe_for_unknown_rule(tmp_path: Path) -> None:
    """无配方的规则走通用「统一工具函数收口」方案。"""
    ctx = _make_ctx(tmp_path, {})
    ctx.issues = [
        _rule_issue("PY-SHADOW-BUILTIN", "app/x.py", ln, id=f"ISS-{n:04d}")
        for n, ln in enumerate((3, 7, 11), 1)
    ]
    dedup = [p for p in generate_proposals(ctx) if p.kind == "dedup"]
    assert len(dedup) == 1
    assert "统一工具函数收口" in dedup[0].title


def test_dedup_below_threshold_ignored(tmp_path: Path) -> None:
    """同文件同类命中 <3 处不产出归并方案。"""
    ctx = _make_ctx(tmp_path, {})
    ctx.issues = [
        _rule_issue("PY-SQL-INJECTION", "app/dao.py", 10, id="ISS-0001"),
        _rule_issue("PY-SQL-INJECTION", "app/dao.py", 20, id="ISS-0002"),
    ]
    assert [p for p in generate_proposals(ctx) if p.kind == "dedup"] == []


# ---------------------------------------------------------------- 3) 热点模块拆分


class _HotspotIndex:
    """FakeIndex：monolith.py 被 4 个文件依赖；其余无依赖。"""

    IMPORTERS = {"app/monolith.py": ["a.py", "b.py", "c.py", "d.py"]}

    def dependencies(self, target: str, direction: str = "imports") -> list[str]:
        if direction == "imported_by":
            return list(self.IMPORTERS.get(target, []))
        return []


def test_hotspot_split_module(tmp_path: Path) -> None:
    """行数达标（≥200）且被依赖最多（≥3）的模块 → split-module 方案。"""
    ctx = _make_ctx(tmp_path, {})
    ctx.workspace.manifests = [
        FileManifest(path="app/monolith.py", language="python", loc=520),
        FileManifest(path="app/tiny.py", language="python", loc=30),
    ]
    ctx.index = _HotspotIndex()
    proposals = [p for p in generate_proposals(ctx) if p.kind == "split-module"]
    assert len(proposals) == 1
    p = proposals[0]
    assert p.target == "app/monolith.py"
    assert "4 个模块依赖" in p.rationale
    assert "re-export" in " ".join(p.steps)
    assert 0.5 <= p.confidence <= 0.8


def test_hotspot_requires_loc_and_importers(tmp_path: Path) -> None:
    """行数不足或被依赖不足的模块不产出拆分方案。"""
    ctx = _make_ctx(tmp_path, {})
    ctx.workspace.manifests = [
        FileManifest(path="app/small.py", language="python", loc=100),  # 行数不足
        FileManifest(path="app/lonely.py", language="python", loc=500),  # 无人依赖
    ]
    ctx.index = _HotspotIndex()
    assert [p for p in generate_proposals(ctx) if p.kind == "split-module"] == []


# ---------------------------------------------------------------- 4) 循环依赖


class _CycleIndex:
    """FakeIndex：a.py ↔ b.py 互相导入成环。"""

    GRAPH = {"app/a.py": ["app/b.py"], "app/b.py": ["app/a.py"], "app/c.py": ["app/a.py"]}

    def dependencies(self, target: str, direction: str = "imports") -> list[str]:
        if direction == "imports":
            return list(self.GRAPH.get(target, []))
        return []


def test_dependency_cycle_detected(tmp_path: Path) -> None:
    """依赖图存在环 → other 方案列出环路径；找不到环则不输出。"""
    ctx = _make_ctx(tmp_path, {})
    ctx.workspace.manifests = [
        FileManifest(path=f"app/{n}.py", language="python", loc=10) for n in "abc"
    ]
    ctx.index = _CycleIndex()
    cycles = [p for p in generate_proposals(ctx) if p.kind == "other"]
    assert len(cycles) == 1
    p = cycles[0]
    assert "循环依赖" in p.title
    assert "app/a.py -> app/b.py -> app/a.py" in p.rationale
    assert 0.5 <= p.confidence <= 0.8


def test_no_cycle_no_proposal(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, {})
    ctx.workspace.manifests = [
        FileManifest(path="app/a.py", language="python", loc=10),
        FileManifest(path="app/b.py", language="python", loc=10),
    ]

    class _AcyclicIndex:
        GRAPH = {"app/a.py": ["app/b.py"], "app/b.py": []}

        def dependencies(self, target: str, direction: str = "imports") -> list[str]:
            if direction == "imports":
                return list(self.GRAPH.get(target, []))
            return []

    ctx.index = _AcyclicIndex()  # a -> b 单向，无环
    assert [p for p in generate_proposals(ctx) if p.kind == "other"] == []


# ---------------------------------------------------------------- 综合约束


def test_ids_stable_and_confidence_bounded(tmp_path: Path) -> None:
    """id 用 make_id("REF", n) 顺序编号；confidence 全部落在 0.5~0.8。"""
    ctx = _make_ctx(tmp_path, {"app/big.py": _long_function_source()})
    ctx.issues = [
        _rule_issue("PY-LONG-FUNCTION", "app/big.py", 1, 100, id="ISS-0001"),
        *(
            _rule_issue("PY-BARE-EXCEPT", "app/big.py", ln, id=f"ISS-{n:04d}")
            for n, ln in enumerate((5, 9, 13), 2)
        ),
    ]
    proposals = generate_proposals(ctx)
    assert len(proposals) >= 2
    assert [p.id for p in proposals] == [f"REF-{n:04d}" for n in range(1, len(proposals) + 1)]
    assert all(0.5 <= p.confidence <= 0.8 for p in proposals)


def test_empty_project_no_proposals(tmp_path: Path) -> None:
    """干净项目（无命中/无索引）→ 不产出任何方案。"""
    ctx = _make_ctx(tmp_path, {"app/clean.py": "x = 1\n"})
    assert generate_proposals(ctx) == []


@pytest.mark.parametrize("rule_id", ["PY-LONG-FUNCTION", "JS-LONG-FUNCTION"])
def test_both_language_rules_supported(rule_id: str, tmp_path: Path) -> None:
    """Python 与 JS 的 LONG-FUNCTION 规则均参与分解聚合。"""
    ctx = _make_ctx(tmp_path, {})
    ctx.issues = [_rule_issue(rule_id, "app/a.js", 1, 90, id="ISS-0001")]
    assert [p for p in generate_proposals(ctx) if p.kind == "decompose"]
