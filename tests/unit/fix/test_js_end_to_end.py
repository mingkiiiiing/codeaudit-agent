"""W7-A3 端到端（JS 版）：含 1 个已知问题的小 JS 项目 → run_audit 全流程 →
verified Patch + passed TestCase。

与 tests/integration/test_fix_tests_loop.py 的 Python 版对齐：只有 LLM 的"思考"被
脚本替代（替换 audit.orchestrator.pipeline._make_llm），git apply / tree-sitter
语法重解析 / node --test 沙箱运行全部真实执行。

断言：
- 规则通道检出 store.js 的 SQL 拼接（JS-SQL-CONCAT，critical）并被修复选中；
- 产出 apply_status == "verified" 的 Patch，现有 node:test 测试 2/2 通过；
- 对应 Issue.fix_status == VERIFIED 且 patch_id 贯通；
- testgen 产出 status == "passed" 的 TestCase，生成文件为 .test.mjs 且真实落盘；
- fix / testgen 阶段发真实汇总事件（verified=1 / passed=1）；
- 事件序列完整走到 done；工作副本中 store.js 已参数化。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from audit.config import AuditConfig
from audit.models import FixStatus

import _js_helpers as J

_requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="本机无 node 可执行文件"
)


def _make_js_project(tmp_path: Path) -> Path:
    proj = tmp_path / "js_app"
    J.write_file(proj / "store.js", J.STORE_JS)
    J.write_file(proj / "tests" / "store.test.js", J.TESTS_STORE_TEST_JS)
    return proj


@_requires_node
async def test_js_project_full_audit_fix_and_testgen_loop(tmp_path: Path):
    proj = _make_js_project(tmp_path)

    import audit.orchestrator.pipeline as pipeline_module
    from audit.orchestrator.pipeline import run_audit

    llm = J.ScriptedJsLLM()
    original = pipeline_module._make_llm
    pipeline_module._make_llm = lambda _config: (llm, None)  # noqa: E731
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(dict(event))

    try:
        config = AuditConfig(
            source_path=str(proj),
            work_root=str(tmp_path / "work"),
            out_dir=str(tmp_path / "reports"),
            enable_llm_review=False,  # 审查/复核关闭 → 纯规则检测
            enable_verify=False,
            do_fix=True,
            do_tests=True,
        )
        report = await run_audit(config, emit)
    finally:
        pipeline_module._make_llm = original

    # 脚本化 LLM 真实被调用：fix 与 testgen 各至少 1 次
    assert llm.calls["fix"] >= 1
    assert llm.calls["testgen"] >= 1

    # 检测：SQL 拼接（critical）被规则通道检出
    sql_issue = next(
        (i for i in report.issues if i.file == "store.js" and i.severity.value == "critical"),
        None,
    )
    assert sql_issue is not None, "未检出 store.js 的 critical SQL 拼接问题"

    # verified Patch：现有 node:test 2/2 通过（git apply + tree-sitter + node --test 全真实）
    verified = [p for p in report.patches if p.apply_status == "verified"]
    assert verified, f"未产出 verified Patch：{[p.apply_status for p in report.patches]}"
    patch = verified[0]
    assert (patch.tests_run, patch.tests_passed) == (2, 2)
    assert patch.issue_id == sql_issue.id
    assert "WHERE name = ?" in patch.diff

    # Issue 状态贯通
    assert sql_issue.fix_status == FixStatus.VERIFIED
    assert sql_issue.patch_id == patch.id

    # 工作副本真实落盘：store.js 已参数化
    src_roots = list((tmp_path / "work").glob("*/src"))
    assert len(src_roots) == 1
    fixed = (src_roots[0] / "store.js").read_text(encoding="utf-8")
    assert 'WHERE name = ?' in fixed
    assert "db.all(query, username)" in fixed

    # testgen：passed TestCase + .test.mjs 在工作副本中真实存在（node --test 真跑通过）
    passed = [tc for tc in report.test_cases if tc.status == "passed"]
    assert passed, f"未产出 passed TestCase：{[tc.status for tc in report.test_cases]}"
    tc = passed[0]
    assert tc.target == "store.js:findUser"
    assert tc.file == "tests/generated/test_gen_store.test.mjs"
    assert tc.assert_count >= 1
    gen_file = src_roots[0] / tc.file
    assert gen_file.is_file()
    assert "import { findUser } from '../../store.js';" in gen_file.read_text(encoding="utf-8")

    # fix / testgen 真实汇总事件（而非"跳过"占位）
    fix_msgs = [str(e.get("message", "")) for e in events if e.get("stage") == "fix"]
    testgen_msgs = [str(e.get("message", "")) for e in events if e.get("stage") == "testgen"]
    assert any(m.startswith("修复阶段完成") for m in fix_msgs)
    assert any(m.startswith("单测生成阶段完成") for m in testgen_msgs)
    fix_summary = next(
        e for e in events if e.get("stage") == "fix" and str(e.get("message")).startswith("修复阶段完成")
    )
    assert fix_summary.get("verified") == 1
    testgen_summary = next(
        e for e in events if e.get("stage") == "testgen" and "单测生成阶段完成" in str(e.get("message"))
    )
    assert testgen_summary.get("passed") == 1

    # 事件序列完整走到 done
    stages = [e.get("stage") for e in events if e.get("stage")]
    assert stages[-1] == "done"
