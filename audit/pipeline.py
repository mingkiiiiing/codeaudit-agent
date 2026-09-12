"""流水线契约（契约文件，勿改）：PipelineContext 与事件发射器。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from audit.config import AuditConfig
from audit.llm.base import LLMClient
from audit.models import (
    ArchitectureCard,
    AuditStats,
    Issue,
    Patch,
    RefactorProposal,
    RuleHit,
    TestCase,
)
from audit.workspace import WorkspaceContext

# async def emitter(event: dict) -> None
EventEmitter = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class PipelineContext:
    """七阶段流水线的共享上下文：各阶段只读写自己负责的字段。

    字段所有权（谁写谁负责）：
      - ingest/index 阶段：workspace（含 index 注入）
      - understand：architecture
      - detect：rule_hits / candidates / issues
      - refactor（契约 v1.7）：refactor_proposals
      - fix / testgen：patches / test_cases
      - report：读取以上全部
    """

    config: AuditConfig
    workspace: WorkspaceContext
    llm: LLMClient
    emitter: EventEmitter
    index: Any = None  # IndexStore | None
    architecture: ArchitectureCard | None = None
    rule_hits: list[RuleHit] = field(default_factory=list)
    candidates: list[Issue] = field(default_factory=list)  # 验证前的候选
    issues: list[Issue] = field(default_factory=list)  # 验证后的最终清单
    refactor_proposals: list[RefactorProposal] = field(default_factory=list)  # 契约 v1.7
    patches: list[Patch] = field(default_factory=list)
    test_cases: list[TestCase] = field(default_factory=list)
    stats: AuditStats = field(default_factory=AuditStats)
    extra: dict[str, Any] = field(default_factory=dict)  # 阶段间自由传递

    async def emit(
        self,
        stage: str,
        message: str,
        current: int | None = None,
        total: int | None = None,
        **extra_data: Any,
    ) -> None:
        event: dict[str, Any] = {"type": "progress", "stage": stage, "message": message}
        if current is not None:
            event["current"] = current
        if total is not None:
            event["total"] = total
        event.update(extra_data)
        await self.emitter(event)
