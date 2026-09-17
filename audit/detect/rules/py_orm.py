"""Python ORM N+1 查询规则库（W19-B）：循环体内逐条 ORM 查询。

与 python_ext.py 同一实现思路：基于 `_python_common` 的逐行掩码扫描 + 缩进循环
作用域启发式（不依赖 tree-sitter），行号精确落在真实代码行；`check()` 纯函数式，
不修改 ctx、无 IO。

P0-3 AST 佐证（保守口径）：ctx.tree 可用时用 AST 证实命中行确为 ORM 链式调用
（attribute 调用链方法名序列：`query` 收尾于 get/first/all，或 `objects` 收尾于
get），证实→置信度 0.7→0.8（meta["ast_confirmed"]=True）；佐证失败或 tree=None
（行级逻辑即兜底路径）时命中原样保留，绝不删减既有命中。

口径（W19-B 误报红线）：本规则**只认 ORM 形态**——SQLAlchemy 链式
`session.query(...).get(/.first(/.all(`（与 `.query(` 同行链式）或 Django
`Model.objects.get(`；`.execute()` 属原生 SQL 游标调用，是既有 IO-IN-LOOP
规则的领地，本规则一律不报。registry 接线由集成人统一完成；本文件只提供
build_py_orm_rules()。
"""

from __future__ import annotations

import re
from typing import Any, Pattern

from audit.detect.ast_util import attr_chain_names, confirm_hit, line_node_map
from audit.detect.base import Rule, RuleContext
from audit.detect.rules._python_common import (
    get_scan,
    line_in_loops,
    loop_ranges,
)
from audit.models import Category, RuleHit, Severity

__all__ = ["OrmNPlusOneRule", "build_py_orm_rules"]


class OrmNPlusOneRule(Rule):
    """for/while 循环体内逐条执行 ORM 查询（N+1 查询问题）。

    只认 ORM 形态：循环体内（header 行不算）`.query(` 与 `.get(/.first(/.all(`
    同行链式，或 `Model.objects.get(`。排除 `.execute(`（原生 SQL 归 IO-IN-LOOP）
    与循环外同型调用（循环作用域天然排除）。静态无法证明循环次数真的很大，
    小集合上的逐条查询属可解释的误报方向，故定 performance/medium。
    """

    id = "PY-ORM-N-PLUS-ONE"
    category = Category.PERFORMANCE
    severity = Severity.MEDIUM
    languages = ("python",)
    description = (
        "for/while 循环体内逐条执行 ORM 查询（SQLAlchemy `session.query(X).get(/"
        ".first(/.all(` 同行链式，或 Django `Model.objects.get(`）：循环 N 次即发送"
        " N 条 SQL（N+1 问题），数据量增大时延迟线性放大；应改为一次性批量查询或"
        "关系预加载。"
    )

    bad_example = (
        "for uid in user_ids:\n"
        "    user = session.query(User).get(uid)  # 每轮循环发一条 SQL\n"
        "    total += user.amount\n"
    )
    good_example = (
        "users = session.query(User).filter(User.id.in_(user_ids)).all()  # 一条 IN 查询\n"
        "by_id = {u.id: u for u in users}\n"
        "for uid in user_ids:\n"
        "    total += by_id[uid].amount\n"
    )

    # SQLAlchemy 链式：同行先 `.query(` 后 `.get(/.first(/.all(`（掩码行保证
    # 字符串/注释里的同形文本不干扰匹配）
    _QUERY_CHAIN_RE: Pattern[str] = re.compile(r"\.query\s*\(.*\.\s*(?:get|first|all)\s*\(")
    # Django 形态：`Model.objects.get(`（filter/all 返回惰性 queryset，不在此列）
    _OBJECTS_GET_RE: Pattern[str] = re.compile(r"\b\w+\s*\.\s*objects\s*\.\s*get\s*\(")
    # 原生 SQL 游标循环执行（cur.execute）归既有 IO-IN-LOOP 规则，本规则让路
    _EXECUTE_RE: Pattern[str] = re.compile(r"\.\s*execute\s*\(")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        loops = loop_ranges(scan.masked)
        if not loops:
            return []
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            lineno = idx + 1
            # 只看循环体（header 行不算 body：`for u in session.query(U).all():`
            # 只执行一次查询，报了就是误报）
            if not line_in_loops(loops, lineno):
                continue
            if self._EXECUTE_RE.search(ln):
                continue  # 原生 SQL 领地，见模块 docstring 口径
            if self._QUERY_CHAIN_RE.search(ln):
                kind = "session.query(...).get(/.first(/.all("
            elif self._OBJECTS_GET_RE.search(ln):
                kind = "Model.objects.get("
            else:
                continue
            hits.append(
                self.make_hit(
                    ctx,
                    lineno,
                    lineno,
                    f"第 {lineno} 行在循环体内逐条执行 ORM 查询（{kind}）：构成 N+1 查询——"
                    "循环 N 次即发送 N 条 SQL，数据量增大时延迟线性放大；请改为一次批量取回"
                    "（SQLAlchemy：selectinload/joinedload 预加载，或 `IN` 批量查询后建映射；"
                    "Django：select_related/prefetch_related 或 `__in` 过滤）。",
                    meta={"pattern": kind},
                )
            )
        # P0-3 AST 佐证：AST 证实 ORM 链式调用形态后提升置信（不删命中）
        self._ast_confirm(ctx, hits)
        return hits

    def _ast_confirm(self, ctx: RuleContext, hits: list[RuleHit]) -> None:
        """P0-3 AST 佐证（保守口径）：AST 证实命中行为 ORM 链式调用才提升置信。

        判定：命中行上的 call 节点的属性链方法名序列满足——SQLAlchemy 形态
        ``…query(…)`` 收尾于 get/first/all，或 Django 形态 ``…objects.get(…``。
        佐证失败或 ctx.tree 为 None（行级兜底）时命中原样保留。
        """
        if ctx.tree is None or ctx.tree.root_node is None or not hits:
            return
        source = ctx.source.encode("utf-8", errors="replace")
        nodes_by_line = line_node_map(ctx.tree)
        for hit in hits:
            if any(_is_orm_chain(n, source) for n in nodes_by_line.get(hit.line_start, [])):
                confirm_hit(hit)


def _is_orm_chain(node: Any, source: bytes) -> bool:
    """AST 判定（P0-3 佐证）：call 节点是否为 ORM 链式查询形态。

    - SQLAlchemy：属性链含 ``query`` 且终端方法为 get/first/all；
    - Django：``objects`` 链终端方法为 get。
    """
    if node.type != "call":
        return False
    names = attr_chain_names(node, source)
    if not names:
        return False
    terminal = names[-1]
    if terminal in {"get", "first", "all"} and "query" in names[:-1]:
        return True
    return terminal == "get" and "objects" in names[:-1]


# ---------------------------------------------------------------- 注册


def build_py_orm_rules() -> list[Rule]:
    """构建 W19-B ORM 规则实例（registry 接线由集成人完成）。"""
    return [
        OrmNPlusOneRule(),
    ]
