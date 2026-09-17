"""统一异常层级：所有子包抛出的业务异常都继承 AuditError。"""

from __future__ import annotations


class AuditError(Exception):
    """项目内所有业务异常的基类。"""


class ConfigError(AuditError):
    """配置缺失或非法。"""


class IngestError(AuditError):
    """Stage1 接入失败：路径不存在、zip 损坏、规模超限等。"""


class LLMError(AuditError):
    """LLM 调用失败（重试耗尽后的最终错误）。"""


class AgentBudgetError(AuditError):
    """Agent 迭代或 token 预算熔断。"""


class SandboxError(AuditError):
    """沙箱执行失败。"""
