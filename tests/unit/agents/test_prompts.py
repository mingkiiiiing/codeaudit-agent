"""audit/agents/prompts.py 单元测试（W15-F 卡F：测试盲区补齐）。

断言策略（以模板实际内容为准）：
- PROMPT_VERSION 与 __all__ 锁定，防止版本号被静默升级（bench 消融依赖该版本号对应固定文案）；
- 槽位可通过 str.format 注入且不抛 KeyError，注入值真实出现在产出中（参数注入正确性）；
- 关键段（反幻觉硬约束 / 工具清单 / severity 判级 / verdict JSON schema）逐字存在。

注意：review 模板除 6 个槽位外没有任何字面花括号、verify 模板的字面花括号
均已按 str.format 规则双写（{{...}}）——这是模板可 format 的前提，本测试锁定该前提。
"""

from __future__ import annotations

import pytest

from audit.agents.prompts import (
    PROMPT_VERSION,
    REVIEW_PROMPT_V3,
    VERIFY_PROMPT_V2,
)

REVIEW_SLOTS = {
    "architecture_card": "【架构卡内容】",
    "file_path": "app/services/orders.py",
    "loc": "42",
    "file_symbols": "get_order_summary / load_totals",
    "file_source": "def get_order_summary(order_id, conn):\n    ...",
    "rule_hints": "- [bug] 可变默认参数 (L4)",
}


class TestVersionContract:
    def test_prompt_version_locked(self):
        # bench 消融以版本号选取 prompt；改版本必须是有意识的提交
        assert PROMPT_VERSION == "v3"

    def test_all_exports(self):
        import audit.agents.prompts as prompts_mod

        assert set(prompts_mod.__all__) == {
            "PROMPT_VERSION",
            "REVIEW_PROMPT_V3",
            "VERIFY_PROMPT_V2",
        }


class TestReviewPrompt:
    def test_slots_render_without_key_error(self):
        rendered = REVIEW_PROMPT_V3.format(**REVIEW_SLOTS)
        # 注入值逐个落到产出里
        assert "【架构卡内容】" in rendered
        assert "app/services/orders.py" in rendered
        assert "42 行" in rendered
        assert "get_order_summary / load_totals" in rendered
        assert "def get_order_summary(order_id, conn):" in rendered
        assert "- [bug] 可变默认参数 (L4)" in rendered

    def test_slot_positions_preserved(self):
        rendered = REVIEW_PROMPT_V3.format(**REVIEW_SLOTS)
        assert "<architecture_card>\n【架构卡内容】\n</architecture_card>" in rendered
        assert "[file_symbols]\nget_order_summary / load_totals" in rendered
        assert "[file_source]\ndef get_order_summary" in rendered
        assert "[rule_hints]\n- [bug] 可变默认参数 (L4)" in rendered

    def test_missing_slot_raises_key_error(self):
        # 槽位契约：少传任何一个键都必须炸出来而不是静默产出残缺 prompt
        incomplete = {k: v for k, v in REVIEW_SLOTS.items() if k != "rule_hints"}
        with pytest.raises(KeyError):
            REVIEW_PROMPT_V3.format(**incomplete)

    def test_no_unfilled_braces_after_render(self):
        # 模板除 6 槽位外零字面花括号：渲染后不应残留任何 "{"
        rendered = REVIEW_PROMPT_V3.format(**REVIEW_SLOTS)
        assert "{" not in rendered and "}" not in rendered

    def test_key_sections_present(self):
        # docs/03 §3.1 + 反幻觉硬约束（W2 A3 补充）
        assert "反幻觉硬约束" in REVIEW_PROMPT_V3
        assert "宁缺毋滥" in REVIEW_PROMPT_V3
        assert "严禁臆测" in REVIEW_PROMPT_V3
        assert "严禁凭记忆或猜测填写行号" in REVIEW_PROMPT_V3

    def test_tool_list_present(self):
        assert "read_file / search_code / get_symbol / find_references" in REVIEW_PROMPT_V3
        assert "record_issues" in REVIEW_PROMPT_V3

    def test_severity_rubric_present(self):
        assert "critical=" in REVIEW_PROMPT_V3
        assert "high=" in REVIEW_PROMPT_V3
        assert "medium=" in REVIEW_PROMPT_V3
        assert "low=" in REVIEW_PROMPT_V3


class TestVerifyPrompt:
    def test_issue_json_slot_renders(self):
        issue_json = '{"id": "ISS-0001", "title": "可变默认参数"}'
        rendered = VERIFY_PROMPT_V2.format(issue_json=issue_json)
        assert issue_json in rendered

    def test_missing_slot_raises_key_error(self):
        with pytest.raises(KeyError):
            VERIFY_PROMPT_V2.format()

    def test_verdict_json_schema_present(self):
        # {{...}} 双写经 format 还原为字面 JSON schema，输出契约必须逐字保留
        rendered = VERIFY_PROMPT_V2.format(issue_json="{}")
        assert '{"verdict": "confirmed|false_positive|uncertain"' in rendered
        assert '"confidence"' in rendered
        assert '"severity": "critical|high|medium|low"' in rendered

    def test_key_sections_present(self):
        assert "宁可驳回" in VERIFY_PROMPT_V2
        assert "反幻觉硬约束" in VERIFY_PROMPT_V2
        assert "json mode" in VERIFY_PROMPT_V2
        for verdict in ("confirmed", "false_positive", "uncertain"):
            assert verdict in VERIFY_PROMPT_V2

    def test_false_positive_criteria_lines(self):
        # 复核清单三条否决路径必须逐字存在（verify agent 的行为基准）
        assert "直接判 false_positive" in VERIFY_PROMPT_V2
        assert "判 false_positive" in VERIFY_PROMPT_V2
        assert "如实降为 uncertain" in VERIFY_PROMPT_V2
