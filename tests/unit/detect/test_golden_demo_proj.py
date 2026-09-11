"""demo_proj 金标验证：对样例工程跑 run_rules，断言金标规则按真实行号命中。

对照 tests/samples/demo_proj/GOLDEN_ISSUES.md：
覆盖 G2~G12 共 10 组（G1 为跨文件空指针问题，按任务要求放宽，由 LLM 通道负责）。
注意：金标表中 G3/G4/G5/G8/G9/G11 标注的行号与样例文件真实行号存在 1~3 行偏移
（样例文件内注释亦如此），本测试以"规则必须落在真实缺陷行"为准，见最终交付报告。
"""

from __future__ import annotations

import pytest

from audit.detect.engine import run_rules
from audit.models import Severity

# (rule_id, 文件, 真实行号, 期望严重度)
GOLDEN_HITS = [
    ("PY-SQL-INJECTION", "app/services/orders.py", 11, Severity.CRITICAL),  # G2/G12
    ("PY-LIST-MEMBERSHIP", "app/services/orders.py", 20, Severity.MEDIUM),  # G3
    ("PY-OPEN-NO-CLOSE", "app/services/orders.py", 23, Severity.HIGH),  # G4
    ("PY-BARE-EXCEPT", "app/services/orders.py", 33, Severity.MEDIUM),  # G5
    ("PY-MUTABLE-DEFAULT", "app/utils/mathx.py", 4, Severity.HIGH),  # G6
    ("PY-EQ-NONE", "app/utils/mathx.py", 11, Severity.LOW),  # G7
    ("PY-STR-CONCAT-LOOP", "app/utils/mathx.py", 19, Severity.LOW),  # G8
    ("PY-MAGIC-NUMBER", "app/utils/mathx.py", 24, Severity.LOW),  # G9
    ("PY-HARDCODED-SECRET", "app/config.py", 3, Severity.CRITICAL),  # G10
    ("PY-NO-TIMEOUT", "app/utils/net.py", 7, Severity.MEDIUM),  # G11
]


@pytest.fixture
def rule_index(pipeline_ctx):
    """对样例工作区执行规则，返回 (rule_id, file) -> [RuleHit]。"""
    hits = run_rules(pipeline_ctx)
    index: dict[tuple[str, str], list] = {}
    for h in hits:
        index.setdefault((h.rule_id, h.file), []).append(h)
    return index


class TestGoldenDemoProj:
    def test_at_least_nine_golden_rules_hit(self, rule_index):
        matched = 0
        for rule_id, rel_path, _line, _sev in GOLDEN_HITS:
            if (rule_id, rel_path) in rule_index:
                matched += 1
        assert matched >= 9, f"金标规则只命中 {matched}/10"

    def test_golden_lines_exact(self, rule_index):
        missing: list[str] = []
        for rule_id, rel_path, line, _sev in GOLDEN_HITS:
            got = {h.line_start for h in rule_index.get((rule_id, rel_path), [])}
            if line not in got:
                missing.append(f"{rule_id}@{rel_path}:{line} (命中行: {sorted(got)})")
        assert missing == []

    def test_golden_severities(self, rule_index):
        for rule_id, rel_path, _line, severity in GOLDEN_HITS:
            hits = rule_index.get((rule_id, rel_path), [])
            assert hits, f"{rule_id}@{rel_path} 未命中"
            assert severity in {h.severity for h in hits}

    def test_no_false_positive_on_safe_code(self, rule_index):
        # 参数化 SQL（orders.py:39-42）不得报注入
        assert ("PY-SQL-INJECTION", "app/services/orders.py") in rule_index
        assert all(h.line_start != 40 for h in rule_index[("PY-SQL-INJECTION", "app/services/orders.py")])
        # 带 timeout 的 urlopen（net.py:12）不得报无超时
        assert ("PY-NO-TIMEOUT", "app/utils/net.py") in rule_index
        assert all(h.line_start == 7 for h in rule_index[("PY-NO-TIMEOUT", "app/utils/net.py")])
        # users.py（干净文件）不得产生任何规则命中
        users_hits = [(rid, f) for (rid, f) in rule_index if f == "app/services/users.py"]
        assert users_hits == []

    def test_total_rule_count(self):
        from audit.detect import python_rule_count

        assert python_rule_count() >= 25
