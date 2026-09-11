"""审计配置（契约文件，勿改）。环境变量优先级高于默认值。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_MODEL = "glm-5.3-flash"


@dataclass
class AuditConfig:
    source_path: str = ""  # 待审计项目路径或 zip
    work_root: str = ".codeaudit"  # 工作区根（工作副本/索引/报告默认放这里）
    out_dir: str = ""  # 报告输出目录，空则用 work_root/reports
    languages: list[str] = field(default_factory=list)  # 空 = 自动检测全部

    # LLM
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    concurrency: int = 8
    request_timeout: float = 120.0
    token_budget: int = 2_000_000  # 单次审计全局 token 预算
    enable_llm_review: bool = True  # False 时纯规则模式

    # Agent 行为
    max_tool_iterations: int = 25
    do_fix: bool = False
    do_tests: bool = False

    # 规模保护
    max_files: int = 2000
    max_loc: int = 500_000

    # Wave 2（契约 v1.2，见 docs/07 §3）
    enable_verify: bool = True  # 检测后启用 Verify Agent 复核（LLM 可用时才生效）
    review_mode: str = "simple"  # simple=单次 json 调用 | tools=工具取证循环
    fix_max_patches: int = 50  # 单次审计最多生成的修复 Patch 数
    testgen_max_functions: int = 30  # 单次审计最多生成单测的目标函数数
    batch_small_slices: bool = True  # 低风险小切片合并批量审查（省 token）

    # Wave 3（契约 v1.3，见 docs/08 §3）：消融实验运行期开关
    enable_rule_hints: bool = True  # false：规则命中不注入 LLM 审查 prompt（消融 −rule_hints）
    enable_symbol_context: bool = True  # false：审查上下文不附依赖符号源码（消融 −symbol_context）
    enable_llm_cache: bool = True  # false：GlmClient 不做响应缓存（消融 −cache）
    llm_only_mode: bool = False  # true：跳过静态规则，纯 LLM 审查（消融 llm_only）

    @classmethod
    def from_env(cls, source_path: str | None = None, **overrides: object) -> "AuditConfig":
        env_map = {
            "api_key": ("GLM_API_KEY", ""),
            "base_url": ("GLM_BASE_URL", DEFAULT_BASE_URL),
            "model": ("GLM_MODEL", DEFAULT_MODEL),
        }
        values = {k: os.environ.get(env, default) for k, (env, default) in env_map.items()}
        if source_path is not None:
            values["source_path"] = source_path
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)

    @property
    def llm_available(self) -> bool:
        return bool(self.api_key)

    def resolve_out_dir(self) -> Path:
        return Path(self.out_dir) if self.out_dir else Path(self.work_root) / "reports"
