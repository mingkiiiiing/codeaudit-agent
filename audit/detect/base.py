"""静态规则契约（契约文件，勿改）：RuleContext、Rule 基类、注册表。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from audit.models import Category, RuleHit, Severity, Symbol

# snippet 打码占位（W14-A1，NFR-11）：固定长度，不泄露密钥真实长度
_SECRET_MASK = "********"


def mask_string_literals(line: str) -> str:
    """把一行源码中字符串字面量的值替换为固定长度的 `*`（保留引号结构）。

    W14-A1（C-1，NFR-11）供 mask_snippet=True 的规则回填 snippet 时调用：
    - 支持单引号 / 双引号 / 反引号字面量，处理反斜杠转义；
    - 空字面量（""）不是凭据，原样保留；未闭合引号不视为字面量；
    - 只处理传入的这一行（命中行），不做全局扫描。
    """
    out: list[str] = []
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch in "'\"`":
            j = i + 1
            closed = False
            while j < n:
                if line[j] == "\\":
                    j += 2
                    continue
                if line[j] == ch:
                    closed = True
                    break
                j += 1
            if closed and j > i + 1:
                out.append(ch + _SECRET_MASK + ch)
                i = j + 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def mask_secret_text(text: str) -> str:
    """对单行文本做密钥形态打码：字符串字面量的值替换为固定 8 个 `*`（W15-A1）。

    消费点：LLM 审查路径（audit/agents/review.py）把 evidence 逐条过本函数——
    LLM 载荷中的证据行可能携带源码密钥明文，进报告前必须打码（NFR-11）。
    语义与 W14-A1 的 mask_string_literals 完全对齐（单/双/反引号字面量、转义、
    空串与未闭合引号原样保留），此处直接复用既有判定，保证规则路径与 LLM 路径
    的打码口径不漂移；`mask_snippet` 既有行为不变。
    """
    return mask_string_literals(text)


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
    # 契约 v1.6（W6-A3）追加：可选的正/反示例代码片段，供规则手册文档生成器
    # （scripts/gen_rule_docs.py）渲染"正反示例"小节；空串表示无示例（存量规则缺省）。
    good_example: str = ""
    bad_example: str = ""
    # W14-A1（C-1，NFR-11）：置 True 的规则（硬编码密钥类），make_hit 回填/接收
    # snippet 时对字符串字面量做值打码——报告只保留结构与变量名，不泄露密钥明文。
    mask_snippet: bool = False

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
        text = snippet or "\n".join(ctx.lines[line_start - 1 : line_end])
        if self.mask_snippet:
            text = "\n".join(mask_string_literals(ln) for ln in text.splitlines())
        return RuleHit(
            rule_id=self.id,
            category=self.category,
            severity=self.severity,
            file=ctx.rel_path,
            line_start=line_start,
            line_end=line_end,
            message=message,
            snippet=text,
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
