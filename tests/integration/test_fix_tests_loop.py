"""联调场景②（R2 必测场景 2）：fix + tests 脚本化 LLM 全闭环。

复刻 demo/run_demo.py 的 DemoScriptedLLM 注入思路（替换 audit.orchestrator.pipeline
._make_llm 装配缝隙，见 conftest.run_with_scripted_llm）：只有 LLM 的"思考"被脚本
替代，git apply / tree-sitter 语法重解析 / 沙箱运行测试全部真实执行。

额外覆盖 R1-5 关联形态：靶项目放在**外层 git 仓库的子目录**（模拟用户在自己仓库
内运行审计），工作副本由审计器复制到 work_root——补丁必须仍能真实生效。

断言（修复后的正确行为）：
- 脚本化 LLM 确实被调用（fix / testgen 计数 >= 1）；
- 产出 apply_status == "verified" 的 Patch，现有测试 2/2 通过；
- 对应 Issue.fix_status == VERIFIED 且 patch_id 贯通（R1-17）；
- testgen 产出 status == "passed" 的 TestCase，生成文件真实落盘于工作副本；
- fix / testgen 阶段发真实汇总事件（含 verified=1 / passed=1 统计）；
- 事件序列完整走到 done；工作副本中 store.py 已被参数化修复。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from audit.config import AuditConfig
from audit.models import FixStatus


async def test_scripted_llm_fix_and_tests_full_loop(
    tmp_path: Path,
    make_git_project,
    mini_app_files,
    run_with_scripted_llm,
    events_collector,
    scripted_llm,
    stage_order,
    stage_messages,
) -> None:
    # 靶项目位于外层 git 仓库子目录（R1-5 关联形态：外层仓库不影响修复生效）
    repo_root = make_git_project(mini_app_files, subdir="services/mini_app")
    target = repo_root / "services" / "mini_app"

    config = AuditConfig(
        source_path=str(target),
        work_root=str(tmp_path / "work"),
        out_dir=str(tmp_path / "reports"),
        enable_llm_review=False,  # 审查/复核关闭 → 纯规则检测（与 demo 一致）
        enable_verify=False,
        do_fix=True,
        do_tests=True,
    )
    report = await run_with_scripted_llm(config, events_collector)
    events = events_collector.events

    # 脚本化 LLM 真实被调用：fix 与 testgen 各至少 1 次，且无其他通道调用
    assert scripted_llm.calls["fix"] >= 1
    assert scripted_llm.calls["testgen"] >= 1

    # verified Patch：现有测试 2/2 通过（git apply + 语法重解析 + 沙箱真实执行）
    verified = [p for p in report.patches if p.apply_status == "verified"]
    assert verified, f"未产出 verified Patch：{[p.apply_status for p in report.patches]}"
    patch = verified[0]
    assert (patch.tests_run, patch.tests_passed) == (2, 2)
    assert "conn.execute(query, (username,))" in patch.diff  # 参数化修复
    assert "WHERE name = ?" in patch.diff

    # Issue 状态贯通：fix_status == verified 且 patch_id 指向该 Patch
    issue = next(i for i in report.issues if i.id == patch.issue_id)
    assert issue.fix_status == FixStatus.VERIFIED
    assert issue.patch_id == patch.id

    # 工作副本真实落盘：store.py 已参数化
    src_roots = list((tmp_path / "work").glob("*/src"))
    assert len(src_roots) == 1
    fixed = (src_roots[0] / "store.py").read_text(encoding="utf-8")
    assert 'execute(query, (username,))' in fixed
    assert '"SELECT * FROM users WHERE name = ?"' in fixed

    # testgen：passed TestCase + 生成文件在工作副本中真实存在
    passed = [tc for tc in report.test_cases if tc.status == "passed"]
    assert passed, f"未产出 passed TestCase：{[tc.status for tc in report.test_cases]}"
    assert passed[0].target == "store.py:find_user"
    assert passed[0].assert_count >= 1
    gen_file = src_roots[0] / passed[0].file
    assert gen_file.is_file()
    assert "test_find_user_treats_injection_payload_as_literal" in gen_file.read_text(encoding="utf-8")

    # fix / testgen 真实汇总事件（而非"跳过"占位）
    fix_msgs = stage_messages(events, "fix")
    testgen_msgs = stage_messages(events, "testgen")
    assert any(m.startswith("修复阶段完成") for m in fix_msgs)
    assert not any("跳过" in m for m in fix_msgs)
    assert any(m.startswith("单测生成阶段完成") for m in testgen_msgs)
    assert not any("跳过" in m for m in testgen_msgs)
    fix_summary_event = next(e for e in events if e["stage"] == "fix" and str(e["message"]).startswith("修复阶段完成"))
    assert fix_summary_event.get("verified") == 1
    testgen_summary_event = next(
        e for e in events if e["stage"] == "testgen" and "单测生成阶段完成" in str(e["message"])
    )
    assert testgen_summary_event.get("passed") == 1

    # 事件序列完整走到 done（脚本化注入无 LLM 警告 → 无 init 事件，ingest 为首）
    assert stage_order(events)[0] == "ingest"
    assert stage_order(events)[-1] == "done"
    done_events: list[dict[str, Any]] = [e for e in events if e["stage"] == "done"]
    assert done_events and done_events[-1].get("type") == "progress"
