"""静态规则契约（契约文件，勿改）：RuleContext、Rule 基类、注册表。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from audit.models import Category, RuleHit, Severity, Symbol


@dataclass
class RuleContext:
    """单文件规则检查上下文，由检测引擎（T4）构建后逐规则传入。"""

    rel_path: str
    language: str
    source: str
    lines: list[str]  # 1-based 对齐：lines[0] 是第 1 行
    tree: Any = None  # tree-sitter Tree（无解析器时为 None）
    symbols: list[Symbol] = field(default_factory=list)  # 该文件符号（索引可用时注入）
    meta: dict[str, Any] = field(default_factory=dict)


class Rule(ABC):
    """静态规则基类。子类以类属性声明元信息，实现 check()。

    约定：
    - check() 必须纯函数式：不修改 ctx，不产生 IO。
    - 只报"可在代码中指认"的问题，行号必须落在文件真实范围内。
    """

    id: str = ""
    category: Category = Category.BUG
    severity: Severity = Severity.LOW
    languages: tuple[str, ...] = ()
    description: str = ""

    @abstractmethod
    def check(self, ctx: RuleContext) -> list[RuleHit]:
        ...

    def make_hit(
        self,
        ctx: RuleContext,
        line_start: int,
        line_end: int,
        message: str,
        snippet: str = "",
        meta: dict[str, Any] | None = None,
    ) -> RuleHit:
        return RuleHit(
            rule_id=self.id,
            category=self.category,
            severity=self.severity,
            file=ctx.rel_path,
            line_start=line_start,
            line_end=line_end,
            message=message,
            snippet=snippet or "\n".join(ctx.lines[line_start - 1 : line_end]),
            meta=meta or {},
        )


class RuleRegistry:
    def __init__(self) -> None:
        self._rules: list[Rule] = []

    def register(self, rule: Rule) -> Rule:
        if not rule.id:
            raise ValueError(f"rule {type(rule).__name__} 缺少 id")
        self._rules.append(rule)
        return rule

    def register_all(self, rules: list[Rule]) -> None:
        for r in rules:
            self.register(r)

    def rules_for(self, language: str) -> list[Rule]:
        return [r for r in self._rules if language in r.languages]

    @property
    def all_rules(self) -> list[Rule]:
        return list(self._rules)

    def __len__(self) -> int:
        return len(self._rules)
