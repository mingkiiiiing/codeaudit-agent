"""消融实验配置表（docs/04 §4）：固定金标集，逐个拆组件，证明每个设计都值钱。

契约 v1.3 说明（docs/08 §3）：``AuditConfig`` 已暴露全部消融开关——
``enable_verify`` / ``enable_llm_review``（契约 v1.2）与 ``enable_rule_hints`` /
``enable_symbol_context`` / ``enable_llm_cache`` / ``llm_only_mode``（契约 v1.3），
7 组配置**全部可真实执行**（零占位）。``is_placeholder`` 语义保留：
仅当 overrides 含以 "_" 开头的说明键时判为占位——对当前全真实化的配置集合恒 False，
供 run_ablation 兼容历史调用。

预期结论（docs/04 §4，供实测对账）：

=====================  =========================================
配置                    预期
=====================  =========================================
full                   P≥0.85、R≥0.6（基线）
−verify                Precision 显著下降（0.85→~0.68）
−rule_hints            Recall 下降 + 耗时上升
−symbol_context        跨文件类 Bug（空指针链）大量漏报
−cache                 增量审计耗时回升至全量水平
rules_only             Precision 低、跨文件/语义问题 R≈0
llm_only               P、R 双降 + token 成本上升
=====================  =========================================
"""

from __future__ import annotations

# 7 组配置名 → config_overrides。
# 键名约定：以 "_" 开头的键为占位说明，不是 AuditConfig 字段（当前已零占位）。
ABLATION_CONFIGS: dict[str, dict] = {
    # 完整系统基线
    "full": {},
    # 拆掉 Verify Agent，LLM 候选直接入库（契约 v1.2：enable_verify 已落地）
    "−verify": {"enable_verify": False},
    # 规则命中不注入 LLM prompt（规则自己报，但不引导 LLM；契约 v1.3 已落地）
    "−rule_hints": {"enable_rule_hints": False},
    # 审查上下文不附依赖符号源码（架构卡片与文件源码保留；契约 v1.3 已落地）
    "−symbol_context": {"enable_symbol_context": False},
    # 关闭 LLM 响应缓存，每次请求都真实调用（契约 v1.3 已落地）
    "−cache": {"enable_llm_cache": False},
    # 只跑静态规则：config 已有字段可直接表达
    "rules_only": {"enable_llm_review": False},
    # 跳过静态规则，仅 LLM 全量审查（契约 v1.3：llm_only_mode 已落地）
    "llm_only": {"llm_only_mode": True},
}


def is_placeholder(overrides: dict) -> bool:
    """配置是否仍为占位（含以 "_" 开头的说明键，无法映射到 AuditConfig 字段）。

    7 组配置全部真实化后，对合法配置恒返回 False；保留以兼容历史调用方。
    """
    return any(str(key).startswith("_") for key in overrides)


def plan_ablation() -> list[tuple[str, dict]]:
    """返回消融执行计划：按定义顺序的 (配置名, config_overrides) 列表。

    供 run_ablation / 外层脚本按配置逐个调用；占位配置（当前无）仍可
    用 :func:`is_placeholder` 判别后跳过（表注"待接入"）。
    """
    return [(name, dict(overrides)) for name, overrides in ABLATION_CONFIGS.items()]
