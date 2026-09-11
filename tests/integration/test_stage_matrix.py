"""联调场景⑨（R2 必测场景 9）：七阶段组合矩阵——fix × tests × diff 8 组合参数化。

对 8 种组合逐一断言「修复后的正确行为」：
- 事件序列完整：init → ingest → index → understand → detect → fix → testgen →
  report → done，任何组合不崩、报告恒产出（report.json 落盘 + 返回 AuditReport）；
- 未启用阶段的占位文案精确等于「跳过（未启用 --fix）」/「跳过（未启用 --tests）」，
  且启用时不再出现该占位、有真实阶段事件；
- diff 组合：只审计相对 HEAD 变更的 a.py（「增量模式：仅审计 1 个变更文件」），
  未变更的 b.py 问题不出现；非 diff 组合两文件问题齐全。

无 LLM（FakeLLM 空脚本：fix 生成失败/testgen 跳过是预期分支，阶段仍真实执行）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from audit.config import AuditConfig
from audit.orchestrator.pipeline import run_audit

_A_PY = """import os


def remove_quietly(path):
    try:
        os.remove(path)
    except:
        pass
    return True
"""

_B_PY = """def collect(items, bucket=[]):
    bucket.extend(items)
    return bucket
"""

_FILES = {"a.py": _A_PY, "b.py": _B_PY}

_EXPECTED_STAGES = ["init", "ingest", "index", "understand", "detect", "fix", "testgen", "report", "done"]

_COMBOS = [(do_fix, do_tests, use_diff) for do_fix in (False, True) for do_tests in (False, True) for use_diff in (False, True)]
_COMBO_IDS = [f"fix{int(f)}-tests{int(t)}-diff{int(d)}" for f, t, d in _COMBOS]


def _stage_order(events: list[dict]) -> list[str]:
    seen: list[str] = []
    for event in events:
        stage = event.get("stage")
        if stage and stage not in seen:
            seen.append(stage)
    return seen


def _stage_messages(events: list[dict], stage: str) -> list[str]:
    return [str(e.get("message", "")) for e in events if e.get("stage") == stage]


@pytest.mark.parametrize(("do_fix", "do_tests", "use_diff"), _COMBOS, ids=_COMBO_IDS)
async def test_stage_matrix_combo(
    do_fix: bool, do_tests: bool, use_diff: bool, tmp_path: Path, make_git_project, events_collector
) -> None:
    project = make_git_project(_FILES)
    if use_diff:
        # 提交后只改 a.py：diff 组合应只审计该变更文件
        (project / "a.py").write_text(_A_PY + "\n# touched\n", encoding="utf-8", newline="\n")

    out_dir = tmp_path / "out"
    config = AuditConfig(
        source_path=str(project),
        work_root=str(tmp_path / "work"),
        out_dir=str(out_dir),
        enable_llm_review=False,
        do_fix=do_fix,
        do_tests=do_tests,
        diff_ref="HEAD" if use_diff else "",
    )
    report = await run_audit(config, events_collector)
    events = events_collector.events

    # 任何组合不崩：事件序列完整、终态 done
    assert _stage_order(events) == _EXPECTED_STAGES
    done_events = [e for e in events if e.get("stage") == "done"]
    assert done_events, "缺少 done 事件"

    # 报告恒产出：落盘 + 内存对象
    assert (out_dir / "report.json").is_file()
    assert (out_dir / "report.md").is_file()
    assert (out_dir / "report.html").is_file()
    assert report is not None

    # fix 阶段：启用 → 真实执行；未启用 → 精确占位文案
    fix_msgs = _stage_messages(events, "fix")
    if do_fix:
        assert any(m.startswith("修复阶段开始") for m in fix_msgs), fix_msgs
        assert not any(m == "跳过（未启用 --fix）" for m in fix_msgs)
    else:
        assert fix_msgs.count("跳过（未启用 --fix）") == 1, fix_msgs

    # testgen 阶段：同上
    testgen_msgs = _stage_messages(events, "testgen")
    if do_tests:
        assert any(m.startswith("单测生成阶段开始") for m in testgen_msgs), testgen_msgs
        assert not any(m == "跳过（未启用 --tests）" for m in testgen_msgs)
    else:
        assert testgen_msgs.count("跳过（未启用 --tests）") == 1, testgen_msgs

    # diff 组合语义：只审计变更文件；非 diff：全量
    if use_diff:
        assert any("增量模式：仅审计 1 个变更文件" in m for m in _stage_messages(events, "ingest"))
        assert {i.file for i in report.issues} == {"a.py"}
    else:
        assert {"a.py", "b.py"} <= {i.file for i in report.issues}

    # 检测恒有产出（样本文件带已知缺陷），报告与 summary 一致
    assert report.issues
    assert sum(report.summary.values()) == len(report.issues)
