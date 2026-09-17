"""P0-3 三规则 AST 佐证测试：SQL 注入 / 并发两规则 / ORM N+1。

硬约束（金标不回退）：AST 佐证只提升置信（meta["ast_confirmed"]=True、
confidence 0.7→0.8），佐证失败或 tree=None（行级兜底路径）时命中原样保留，
命中行号集合与无 AST 时完全一致。
"""

from __future__ import annotations

import pytest

from audit.detect.base import RuleContext
from audit.detect.rules.py_concurrency import SleepInAsyncRule, UnsyncedSharedMutationRule
from audit.detect.rules.py_orm import OrmNPlusOneRule
from audit.detect.rules.python import SqlInjectionConcatRule
from audit.indexer.parsers import parse_source


def ast_ctx(source: str, rel_path: str = "m.py") -> RuleContext:
    """构造带 tree-sitter Tree 的 RuleContext（与引擎接线同口径）。"""
    tree, _ok = parse_source("python", source.encode("utf-8", errors="replace"))
    return RuleContext(
        rel_path=rel_path,
        language="python",
        source=source,
        lines=source.splitlines(),
        tree=tree,
    )


def plain_ctx(source: str, rel_path: str = "m.py") -> RuleContext:
    """构造 tree=None 的 RuleContext（行级兜底路径）。"""
    return RuleContext(
        rel_path=rel_path,
        language=language_of(rel_path),
        source=source,
        lines=source.splitlines(),
        tree=None,
    )


def language_of(rel_path: str) -> str:
    return "typescript" if rel_path.endswith((".ts", ".tsx")) else "python"


SQL_CONCAT = 'uid = input()\ncur.execute("SELECT * FROM users WHERE id=" + uid)\n'
SQL_FSTRING = 'name = input()\nq = f"SELECT * FROM t WHERE n={name}"\ncur.execute(q)\n'
SQL_CONST_2STEP = (
    'PREFIX = "SELECT name FROM users WHERE id="\n'
    "uid = input()\n"
    "q = PREFIX + uid\n"
    "cur.execute(q)\n"
)
SQL_PARAMIZED = 'uid = 1\ncur.execute("SELECT * FROM users WHERE id=?", (uid,))\n'


class TestSqlInjectionAstConfirm:
    rule = SqlInjectionConcatRule()

    def test_concat_confirmed_with_tree(self):
        hits = self.rule.check(ast_ctx(SQL_CONCAT))
        assert [h.line_start for h in hits] == [2]
        assert hits[0].meta["ast_confirmed"] is True
        assert hits[0].meta["confidence"] == 0.8

    def test_fstring_interpolation_confirmed(self):
        hits = self.rule.check(ast_ctx(SQL_FSTRING))
        assert [h.line_start for h in hits] == [2]
        assert hits[0].meta["ast_confirmed"] is True

    def test_two_step_const_use_confirmed(self):
        hits = self.rule.check(ast_ctx(SQL_CONST_2STEP))
        assert [h.line_start for h in hits] == [3]
        assert hits[0].meta["ast_confirmed"] is True

    def test_fallback_without_tree_keeps_hits_and_no_flag(self):
        hits = self.rule.check(plain_ctx(SQL_CONCAT))
        assert [h.line_start for h in hits] == [2]
        assert "ast_confirmed" not in hits[0].meta

    def test_line_sets_identical_with_and_without_tree(self):
        """金标不回退：有/无 AST 命中行集合一致，仅 meta 注记不同。"""
        for src in (SQL_CONCAT, SQL_FSTRING, SQL_CONST_2STEP):
            with_tree = {h.line_start for h in self.rule.check(ast_ctx(src))}
            without = {h.line_start for h in self.rule.check(plain_ctx(src))}
            assert with_tree and with_tree == without

    def test_paramized_query_not_confirmed_nor_hit(self):
        assert self.rule.check(ast_ctx(SQL_PARAMIZED)) == []


UNSYNC_SRC = (
    "import threading\n"
    "\n"
    "counter = 0\n"
    "\n"
    "def worker():\n"
    "    global counter\n"
    "    counter += 1\n"
)
SLEEP_SRC = (
    "import time\n"
    "\n"
    "async def poll():\n"
    "    while True:\n"
    "        time.sleep(1)\n"
    "\n"
    "def sync_helper():\n"
    "    time.sleep(1)\n"
)
ORM_SRC = (
    "def total(user_ids):\n"
    "    amount = 0\n"
    "    for uid in user_ids:\n"
    "        u = session.query(User).get(uid)\n"
    "        amount += u.amount\n"
    "    return amount\n"
)


class TestConcurrencyAstConfirm:
    def test_unsynced_mutation_confirmed(self):
        hits = UnsyncedSharedMutationRule().check(ast_ctx(UNSYNC_SRC))
        assert [h.line_start for h in hits] == [7]
        assert hits[0].meta["ast_confirmed"] is True
        assert hits[0].meta["confidence"] == 0.8

    def test_unsynced_fallback_without_tree(self):
        hits = UnsyncedSharedMutationRule().check(plain_ctx(UNSYNC_SRC))
        assert [h.line_start for h in hits] == [7]
        assert "ast_confirmed" not in hits[0].meta

    def test_sleep_in_async_confirmed(self):
        hits = SleepInAsyncRule().check(ast_ctx(SLEEP_SRC))
        assert [h.line_start for h in hits] == [5]  # sync_helper 内不报
        assert hits[0].meta["ast_confirmed"] is True

    def test_sleep_fallback_without_tree(self):
        hits = SleepInAsyncRule().check(plain_ctx(SLEEP_SRC))
        assert [h.line_start for h in hits] == [5]
        assert "ast_confirmed" not in hits[0].meta


class TestOrmAstConfirm:
    rule = OrmNPlusOneRule()

    def test_query_chain_confirmed(self):
        hits = self.rule.check(ast_ctx(ORM_SRC))
        assert [h.line_start for h in hits] == [4]
        assert hits[0].meta["ast_confirmed"] is True
        assert hits[0].meta["confidence"] == 0.8

    def test_fallback_without_tree(self):
        hits = self.rule.check(plain_ctx(ORM_SRC))
        assert [h.line_start for h in hits] == [4]
        assert "ast_confirmed" not in hits[0].meta

    def test_django_objects_get_confirmed(self):
        src = (
            "def load(ids):\n"
            "    for pk in ids:\n"
            "        rec = Profile.objects.get(user_id=pk)\n"
            "        print(rec)\n"
        )
        hits = self.rule.check(ast_ctx(src))
        assert [h.line_start for h in hits] == [3]
        assert hits[0].meta["ast_confirmed"] is True

    def test_cursor_execute_in_loop_still_not_reported(self):
        src = (
            "def run(ids):\n"
            "    for i in ids:\n"
            "        cur.execute('SELECT 1')\n"
        )
        assert self.rule.check(ast_ctx(src)) == []


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
