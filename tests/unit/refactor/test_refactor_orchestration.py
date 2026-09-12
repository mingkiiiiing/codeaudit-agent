"""W7-A2 编排接线自测：run_audit 全流程（离线 FakeLLM）产出重构方案并贯通报告。

- detect 之后、fix 之前出现 refactor 阶段事件；
- report.refactor_proposals 非空（长函数分解 + 同类多命中归并）；
- md/html 报告含「重构方案」章节；干净项目显示占位文案不崩。
"""

from __future__ import annotations

import json
from pathlib import Path

from audit.config import AuditConfig
from audit.orchestrator.pipeline import run_audit_simple


def _long_function() -> str:
    """>80 行长函数：签名 + docstring + 3 个空行分隔的职责块（每块 ~30 行）。"""
    lines = ["def process_order(order, conn):", '    """处理订单：校验、落库、收尾。"""', ""]
    for block in range(3):
        for i in range(10):
            n = block * 10 + i
            lines.append(f"    order_id = validate_order(order, {n})")
            lines.append(f"    user = load_user(order, {n})")
            lines.append(f"    save_order(conn, order, {n})")
        lines.append("")
    lines.append("    return order_id")
    return "\n".join(lines) + "\n"


def _handlers() -> str:
    """3 处裸 except（同类规则同文件 ≥3 命中）。"""
    parts = []
    for n in range(1, 4):
        parts.append(f"def handler_{n}(path):\n    try:\n        open(path)\n    except:\n        pass\n")
    return "\n\n".join(parts)


def _make_project(tmp_path: Path, with_defects: bool) -> Path:
    src = tmp_path / "proj"
    (src / "app").mkdir(parents=True)
    (src / "app" / "__init__.py").write_text("", encoding="utf-8")
    if with_defects:
        (src / "app" / "big.py").write_text(_long_function(), encoding="utf-8")
        (src / "app" / "handlers.py").write_text(_handlers(), encoding="utf-8")
    else:
        (src / "app" / "clean.py").write_text("VALUE = 1\n", encoding="utf-8")
    return src


def _config(src: Path, tmp_path: Path) -> AuditConfig:
    return AuditConfig(
        source_path=str(src),
        work_root=str(tmp_path / "work"),
        out_dir=str(tmp_path / "out"),
        enable_llm_review=False,  # 纯规则：FakeLLM 空脚本 + 零 LLM 调用
        enable_verify=False,
    )


async def test_full_pipeline_produces_proposals_and_report_section(tmp_path: Path) -> None:
    """全流程：方案入报告、refactor 阶段事件、md/html 含「重构方案」章节。"""
    src = _make_project(tmp_path, with_defects=True)
    events: list[dict] = []

    report = await run_audit_simple(_config(src, tmp_path), events)

    # 1) 阶段序列：refactor 在 detect 之后、fix 之前
    stages = list(dict.fromkeys(e.get("stage") for e in events if e.get("stage")))
    assert stages.index("detect") < stages.index("refactor") < stages.index("fix")

    # 2) 重构方案贯通到报告：分解 + 归并两类齐备，关联 Issue 为真实检测 id
    kinds = {p.kind for p in report.refactor_proposals}
    assert "decompose" in kinds
    assert "dedup" in kinds
    issue_ids = {i.id for i in report.issues}
    for p in report.refactor_proposals:
        assert p.id.startswith("REF-")
        assert set(p.related_issues) <= issue_ids
        assert 0.5 <= p.confidence <= 0.8
        assert p.steps
    decompose = next(p for p in report.refactor_proposals if p.kind == "decompose")
    long_issue = next(i for i in report.issues if any("PY-LONG-FUNCTION" in str(e) for e in i.evidence))
    assert long_issue.id in decompose.related_issues
    dedup = next(p for p in report.refactor_proposals if p.kind == "dedup")
    assert len(dedup.related_issues) >= 3

    # 3) 事件文案
    refactor_msgs = [str(e.get("message", "")) for e in events if e.get("stage") == "refactor"]
    assert any(m.startswith("重构方案：") for m in refactor_msgs)

    # 4) 三格式报告落盘且 md/html 含章节标题
    out = tmp_path / "out"
    md = (out / "report.md").read_text(encoding="utf-8")
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "重构方案" in md and "重构方案" in html
    assert decompose.title in md
    data = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert data["refactor_proposals"]


async def test_full_pipeline_clean_project_placeholder(tmp_path: Path) -> None:
    """无方案项目：占位文案出现、报告不崩、refactor 阶段照常收尾。"""
    src = _make_project(tmp_path, with_defects=False)
    events: list[dict] = []

    report = await run_audit_simple(_config(src, tmp_path), events)

    assert report.refactor_proposals == []
    refactor_msgs = [str(e.get("message", "")) for e in events if e.get("stage") == "refactor"]
    assert any(m == "重构方案：0 条" for m in refactor_msgs)
    md = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    html = (tmp_path / "out" / "report.html").read_text(encoding="utf-8")
    assert "本次审计未生成重构方案" in md
    assert "本次审计未生成重构方案" in html
