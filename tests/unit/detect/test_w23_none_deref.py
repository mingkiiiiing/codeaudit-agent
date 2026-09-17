"""P0-3 配套新规则 PY-NONE-DEREF 行为测试：同函数内 None 赋值→解引用（空值流）。

语料：tests/samples/none_deref/（5 正例 / 3 反例）。
验收口径（W23 卡 B）：正例 ≥4 检出（实测 5/5，行号逐一断言）、反例 0 误报。
AST-only 契约：tree=None（无 tree-sitter / 行级兜底路径）时不产任何命中。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from audit.detect.base import RuleContext
from audit.detect.rules.py_none_deref import NoneDerefRule
from audit.indexer.parsers import parse_source

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples" / "none_deref"

# 正例：文件名 -> 期望命中行号（与语料文件内注释一致，W18 教训：按行号精确归因）
POSITIVE_EXPECTED = {
    "positive_attribute.py": 10,
    "positive_call.py": 8,
    "positive_loop.py": 8,
    "positive_subscript.py": 7,
    "positive_try_block.py": 8,
}
CLEAN_FILES = [
    "clean_branch_guard.py",
    "clean_param_self.py",
    "clean_reassigned.py",
]


def _ctx(source: str, rel_path: str, *, with_tree: bool) -> RuleContext:
    tree = None
    if with_tree:
        tree, _ok = parse_source("python", source.encode("utf-8", errors="replace"))
    return RuleContext(
        rel_path=rel_path,
        language="python",
        source=source,
        lines=source.splitlines(),
        tree=tree,
    )


rule = NoneDerefRule()


@pytest.mark.parametrize("fname,expected_line", sorted(POSITIVE_EXPECTED.items()))
def test_positive_sample_hits_expected_line(fname, expected_line):
    src = (SAMPLES_DIR / fname).read_text(encoding="utf-8")
    hits = rule.check(_ctx(src, fname, with_tree=True))
    assert [h.line_start for h in hits] == [expected_line]


def test_positive_detection_meets_acceptance_bar():
    """验收口径：5 正例至少检出 4（当前实现 5/5）。"""
    detected = 0
    for fname in POSITIVE_EXPECTED:
        src = (SAMPLES_DIR / fname).read_text(encoding="utf-8")
        if rule.check(_ctx(src, fname, with_tree=True)):
            detected += 1
    assert detected >= 4


@pytest.mark.parametrize("fname", CLEAN_FILES)
def test_clean_sample_zero_false_positive(fname):
    src = (SAMPLES_DIR / fname).read_text(encoding="utf-8")
    assert rule.check(_ctx(src, fname, with_tree=True)) == []


@pytest.mark.parametrize("fname", sorted(POSITIVE_EXPECTED) + CLEAN_FILES)
def test_ast_only_contract_no_hit_without_tree(fname):
    """tree=None（行级兜底路径）时 AST-only 规则不产命中。"""
    src = (SAMPLES_DIR / fname).read_text(encoding="utf-8")
    assert rule.check(_ctx(src, fname, with_tree=False)) == []
