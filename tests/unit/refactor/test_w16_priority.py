"""W16 重构方案优先级与工作量估算自测：P0/P1/P2 分级 + effort 口径 + 零回归。

全部离线：ctx 直接 fabricated（对齐 test_heuristics.py 的手工构造风格），
不跑 ingest/index/detect。旧字段零变化通过对 generate_proposals 输出的
手工期望 dict 对照断言。
"""

from __future__ import annotations

from pathlib import Path

from audit.config import AuditConfig
from audit.llm.base import FakeLLMClient
from audit.models import (
    Category,
    FileManifest,
    Issue,
    IssueSource,
    RefactorProposal,
    Severity,
)
from audit.pipeline import PipelineContext
from audit.refactor.heuristics import _prioritize, generate_proposals
from audit.workspace import WorkspaceContext

# ---------------------------------------------------------------- 夹具工具

_LONG_FUNCTION_SOURCE_LINES = 100  # _long_function_source 的行数（含签名与收尾）


def _make_ctx(tmp_path: Path) -> PipelineContext:
    """构造无索引、无源文件的 PipelineContext（与 test_heuristics 同构）。"""
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    ws = WorkspaceContext(
        audit_id="refact01", src_root=src, work_root=tmp_path / "work", db_path=tmp_path / "i.db"
    )

    async def emit(event: dict) -> None:
        pass

    config = AuditConfig(source_path=str(src), enable_llm_review=False)
    return PipelineContext(config=config, workspace=ws, llm=FakeLLMClient(), emitter=emit)


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


def _long_function_source(blocks: int = 3, block_lines: int = 8) -> str:
    """生成 >80 行的长函数源码（与 test_heuristics 相同配方）。"""
    lines = ["def process_order(order, conn):", '    """处理订单。"""', ""]
    for b in range(blocks):
        for i in range(block_lines):
            n = b * block_lines + i
            lines.extend(
                [
                    f"    order_id = validate_order(order, {n})",
                    f"    user = load_user(order, {n})",
                    f"    save_order(conn, order, {n})",
                    f"    audit_log(order, {n})",
                    f"    notify_user(order, {n})",
                    f"    result = finalize(order, {n})",
                ]
            )
        lines.append("")
    lines.append("    return result")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- P0 判定


def test_cycle_proposal_is_p0_effort_8(tmp_path: Path) -> None:
    """循环依赖方案 → P0 + effort=8.0；confidence 0.75 保持不变。"""
    ctx = _make_ctx(tmp_path)
    ctx.workspace.manifests = [
        FileManifest(path=f"app/{n}.py", language="python", loc=10) for n in "ab"
    ]

    class _CycleIndex:
        GRAPH = {"app/a.py": ["app/b.py"], "app/b.py": ["app/a.py"]}

        def dependencies(self, target: str, direction: str = "imports") -> list[str]:
            if direction == "imports":
                return list(self.GRAPH.get(target, []))
            return []

    ctx.index = _CycleIndex()
    cycles = [p for p in generate_proposals(ctx) if p.kind == "other"]
    assert len(cycles) == 1
    p = cycles[0]
    assert p.priority == "P0"
    assert p.estimated_effort_hours == 8.0
    assert p.confidence == 0.75  # W16 不改既有字段


def test_security_critical_related_issue_p0(tmp_path: Path) -> None:
    """关联 issue 为 security/critical → dedup 方案 P0。

    注意：decompose 方案只从 severity=medium 的长函数命中聚合，因此
    critical/high 关联的真实载体是 dedup 方案（related_issues 指向原命中）。
    """
    ctx = _make_ctx(tmp_path)
    ctx.issues = [
        _rule_issue(
            "PY-SQL-INJECTION", "app/dao.py", ln, id=f"ISS-{n:04d}",
            category=Category.SECURITY, severity=Severity.CRITICAL,
        )
        for n, ln in enumerate((10, 22, 35), 1)
    ]
    dedup = [p for p in generate_proposals(ctx) if p.kind == "dedup"]
    assert len(dedup) == 1
    assert dedup[0].priority == "P0"


def test_security_high_related_issue_p0(tmp_path: Path) -> None:
    """关联 issue 为 security/high → P0；security/medium 不触发 P0。"""
    ctx = _make_ctx(tmp_path)
    ctx.issues = [
        _rule_issue(
            "PY-SQL-INJECTION", "app/dao.py", ln, id=f"ISS-{n:04d}",
            category=Category.SECURITY, severity=Severity.HIGH,
        )
        for n, ln in enumerate((10, 22, 35), 1)
    ]
    dedup = [p for p in generate_proposals(ctx) if p.kind == "dedup"]
    assert dedup[0].priority == "P0"

    ctx2 = _make_ctx(tmp_path)
    ctx2.issues = [
        _rule_issue(
            "PY-SQL-INJECTION", "app/dao.py", ln, id=f"ISS-{n:04d}",
            category=Category.SECURITY, severity=Severity.MEDIUM,
        )
        for n, ln in enumerate((10, 22, 35), 1)
    ]
    dedup2 = [p for p in generate_proposals(ctx2) if p.kind == "dedup"]
    assert dedup2[0].priority != "P0"


def test_title_contains_security_p0() -> None:
    """标题含「安全」→ P0（无需关联 issue，直接单测纯函数 _prioritize）。"""
    proposal = RefactorProposal(
        title="加固安全配置", target="app/config.py", kind="simplify",
        rationale="配置散落", steps=["集中配置"], source="heuristic", confidence=0.6,
    )
    _prioritize([proposal])
    assert proposal.priority == "P0"


# ---------------------------------------------------------------- P1 判定


def test_performance_related_issue_p1(tmp_path: Path) -> None:
    """关联 issue 为 performance 类（severity 不限）→ dedup 方案 P1。"""
    ctx = _make_ctx(tmp_path)
    ctx.issues = [
        _rule_issue(
            "PY-LOOP-QUERY", "app/repo.py", ln, id=f"ISS-{n:04d}",
            category=Category.PERFORMANCE, severity=Severity.LOW,
        )
        for n, ln in enumerate((3, 8, 15), 1)
    ]
    dedup = [p for p in generate_proposals(ctx) if p.kind == "dedup"]
    assert len(dedup) == 1
    assert dedup[0].priority == "P1"


def test_non_security_high_related_issue_p1(tmp_path: Path) -> None:
    """关联 issue 为非安全类 severity=high → dedup 方案 P1。"""
    ctx = _make_ctx(tmp_path)
    ctx.issues = [
        _rule_issue(
            "PY-RACE-IO", "app/jobs.py", ln, id=f"ISS-{n:04d}",
            category=Category.BUG, severity=Severity.HIGH,
        )
        for n, ln in enumerate((4, 9, 16), 1)
    ]
    dedup = [p for p in generate_proposals(ctx) if p.kind == "dedup"]
    assert dedup[0].priority == "P1"


def test_long_function_over_200_lines_p1(tmp_path: Path) -> None:
    """长函数超 200 行（行区间取自关联 issue）→ P1；effort=超限/40。"""
    ctx = _make_ctx(tmp_path, )
    ctx.issues = [
        _rule_issue("PY-LONG-FUNCTION", "app/big.py", 1, 260, id="ISS-0001")
    ]
    decompose = [p for p in generate_proposals(ctx) if p.kind == "decompose"]
    p = decompose[0]
    assert p.priority == "P1"
    assert p.estimated_effort_hours == round((260 - 80) / 40.0, 1)  # 4.5


def test_long_function_within_200_lines_p2(tmp_path: Path) -> None:
    """长函数 ≤200 行且无高危关联 → P2；effort 下限 1.0。"""
    ctx = _make_ctx(tmp_path)
    ctx.issues = [_rule_issue("PY-LONG-FUNCTION", "app/big.py", 1, 100, id="ISS-0001")]
    decompose = [p for p in generate_proposals(ctx) if p.kind == "decompose"]
    p = decompose[0]
    assert p.priority == "P2"
    assert p.estimated_effort_hours == 1.0  # (100-80)/40=0.5 → 下限 1.0


def test_plain_proposal_p2(tmp_path: Path) -> None:
    """普通 dedup 方案（style/medium 关联）→ P2。"""
    ctx = _make_ctx(tmp_path)
    ctx.issues = [
        _rule_issue("PY-BARE-EXCEPT", "app/x.py", ln, id=f"ISS-{n:04d}")
        for n, ln in enumerate((5, 9, 13), 1)
    ]
    dedup = [p for p in generate_proposals(ctx) if p.kind == "dedup"]
    assert len(dedup) == 1
    assert dedup[0].priority == "P2"


# ---------------------------------------------------------------- effort 口径


def test_split_module_effort_loc_over_100(tmp_path: Path) -> None:
    """热点拆分 effort=热点行数/100（行数解析自 rationale「共 N 行」）。"""
    ctx = _make_ctx(tmp_path)
    ctx.workspace.manifests = [
        FileManifest(path="app/monolith.py", language="python", loc=520),
    ]

    class _HotspotIndex:
        def dependencies(self, target: str, direction: str = "imports") -> list[str]:
            return ["a.py", "b.py", "c.py"] if direction == "imported_by" else []

    ctx.index = _HotspotIndex()
    split = [p for p in generate_proposals(ctx) if p.kind == "split-module"]
    assert len(split) == 1
    assert split[0].estimated_effort_hours == 5.2


def test_dedup_effort_hit_count(tmp_path: Path) -> None:
    """dedup effort=命中数×1.0（4 处 → 4.0；下限 0.5 由纯函数路径覆盖）。"""
    ctx = _make_ctx(tmp_path)
    ctx.issues = [
        _rule_issue("PY-PRINT-DEBUG", "app/x.py", ln, id=f"ISS-{n:04d}")
        for n, ln in enumerate((2, 6, 10, 14), 1)
    ]
    dedup = [p for p in generate_proposals(ctx) if p.kind == "dedup"]
    assert dedup[0].estimated_effort_hours == 4.0


def test_prioritize_pure_function_effort_fallbacks() -> None:
    """纯函数路径：无关联、无行数文案时的兜底（dedup 下限 0.5 / 其他 1.0）。"""
    dedup = RefactorProposal(title="统一收口：归并 x.py 中 0 处", kind="dedup")
    other = RefactorProposal(title="整理模块边界", kind="other")
    no_loc = RefactorProposal(title="分解长函数 f", kind="decompose")  # 无关联无文案
    _prioritize([dedup, other, no_loc])
    assert dedup.estimated_effort_hours == 0.5
    assert other.estimated_effort_hours == 1.0
    assert no_loc.estimated_effort_hours == 1.0
    assert no_loc.priority == "P2"  # 行数拿不到 → 跳过超限条件，保守 P2


# ---------------------------------------------------------------- 零回归约束


def test_old_fields_untouched(tmp_path: Path) -> None:
    """旧字段（id/title/target/kind/rationale/steps/benefits/related_issues/
    source/confidence）与手工期望 dict 完全一致——W16 只允许填新字段。"""
    ctx = _make_ctx(tmp_path)
    ctx.issues = [
        _rule_issue(
            "PY-LONG-FUNCTION", "app/big.py", 1, _LONG_FUNCTION_SOURCE_LINES,
            id="ISS-0001", title="函数 `process_order` 共 100 行（第 1~100 行），超过 80 行上限",
        ),
        *(
            _rule_issue("PY-BARE-EXCEPT", "app/big.py", ln, id=f"ISS-{n:04d}")
            for n, ln in enumerate((5, 9, 13), 2)
        ),
    ]
    proposals = generate_proposals(ctx)
    decompose = next(p for p in proposals if p.kind == "decompose")
    dedup = next(p for p in proposals if p.kind == "dedup")
    # decompose：字段逐一对照（旧字段语义零变化）
    assert decompose.id == "REF-0001"
    assert decompose.title == "分解长函数 process_order"
    assert decompose.target == "app/big.py::process_order"
    assert decompose.kind == "decompose"
    assert "process_order" in decompose.rationale and "80 行上限" in decompose.rationale
    assert decompose.steps and "编排入口" in decompose.steps[-1]
    assert decompose.benefits
    assert decompose.related_issues == ["ISS-0001"]
    assert decompose.source == "heuristic"
    assert 0.5 <= decompose.confidence <= 0.8
    # dedup：同理抽查关键字段
    assert dedup.target == "app/big.py"
    assert dedup.related_issues == ["ISS-0002", "ISS-0003", "ISS-0004"]
    assert dedup.source == "heuristic"
    # 新字段合法
    assert decompose.priority in {"P0", "P1", "P2"}
    assert dedup.priority in {"P0", "P1", "P2"}
    assert all(p.estimated_effort_hours >= 0.5 for p in proposals)


def test_no_reorder_after_prioritize(tmp_path: Path) -> None:
    """_prioritize 只填字段不重排：输出仍保持 confidence 降序稳定序（旧顺序）。"""
    ctx = _make_ctx(tmp_path)
    ctx.issues = [
        _rule_issue("PY-LONG-FUNCTION", "app/big.py", 1, 260, id="ISS-0001"),
        *(
            _rule_issue("PY-BARE-EXCEPT", "app/big.py", ln, id=f"ISS-{n:04d}")
            for n, ln in enumerate((5, 9, 13), 2)
        ),
    ]
    ctx.workspace.manifests = [
        FileManifest(path="app/monolith.py", language="python", loc=520),
    ]

    class _CycleIndex:
        GRAPH = {"app/a.py": ["app/b.py"], "app/b.py": ["app/a.py"]}

        def dependencies(self, target: str, direction: str = "imports") -> list[str]:
            if direction == "imports":
                return list(self.GRAPH.get(target, []))
            return []

    ctx.index = _CycleIndex()
    proposals = generate_proposals(ctx)
    confidences = [p.confidence for p in proposals]
    assert confidences == sorted(confidences, reverse=True)  # 未被重排
    assert [p.priority for p in proposals]  # 且新字段已填充
