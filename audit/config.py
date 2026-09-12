"""审计配置（契约文件，勿改）。环境变量优先级高于默认值。

契约 v1.4（docs/09 §3）：新增配置文件合成 from_sources()——
合成优先级：默认值 < 配置文件（.codeaudit.toml / pyproject [tool.codeaudit]）< 环境变量(GLM_*) < CLI 显式参数。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

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

    # Wave 4（契约 v1.4，见 docs/09 §3）：CI 门禁 / 基线 / 增量 / 配置文件
    fail_on_severity: str = "off"  # off|critical|high|medium|low；非 off 且存在 >= 级别问题 → 退出码 3
    baseline_path: str = ""  # 已知问题基线文件；命中指纹的问题被抑制（PR 增量模式）
    report_baseline_out: str = ""  # 审计结束后把当前问题写入该基线文件
    diff_ref: str = ""  # git ref（HEAD~1 / origin/main...）；仅审计相对该 ref 变更的文件
    config_file: str = ""  # 显式配置文件路径；空则自动发现 .codeaudit.toml / pyproject [tool.codeaudit]
    config_warnings: list[str] = field(default_factory=list)  # 配置合成的警告（未知键/读取失败），只读输出

    # Wave 7（契约 v1.8 微增，docs/12 §4）：规则扫描并行度（W7-A1）
    # W7 实测（bench/results/stress_w7_clean.md）：本机默认并行较串行慢 43%
    # （进程池序列化开销 > 多核收益），默认强制串行；多核大库场景可显式开启
    rule_scan_workers: int = 1  # 0=自动（文件数≥100 时 min(4,cpu)）；1=串行（默认）；>1=指定并发度

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

    @classmethod
    def from_sources(
        cls,
        cli_overrides: dict[str, Any] | None = None,
        config_file: str = "",
        search_root: Path | None = None,
    ) -> "AuditConfig":
        """配置合成（契约 v1.4）：默认值 < 配置文件 < 环境变量 < CLI 显式参数。

        配置文件发现：显式 config_file 优先；否则在 search_root（缺省 CWD）下依次
        找 .codeaudit.toml（顶层键）与 pyproject.toml 的 [tool.codeaudit] 节。
        未知键与读取失败记入 config_warnings，不抛异常。
        """
        warnings: list[str] = []
        allowed = {f.name for f in fields(cls)} - {"config_warnings", "config_file"}

        file_values: dict[str, Any] = {}
        if config_file:
            path = Path(config_file)
            if not path.is_file():
                warnings.append(f"指定的配置文件不存在：{config_file}")
            else:
                values, w = _read_toml_section(path, section=False)
                warnings.extend(w)
                file_values = values
        else:
            root = Path(search_root) if search_root else Path.cwd()
            auto = root / ".codeaudit.toml"
            pyproject = root / "pyproject.toml"
            if auto.is_file():
                file_values, w = _read_toml_section(auto, section=False)
                warnings.extend(w)
            elif pyproject.is_file():
                file_values, w = _read_toml_section(pyproject, section=True)
                warnings.extend(w)

        values: dict[str, Any] = {}
        for f in fields(cls):
            if f.name in {"config_warnings"}:
                continue
            if f.name in file_values:
                values[f.name] = file_values[f.name]
        # 环境变量层（仅 GLM 三键，与 from_env 口径一致）
        values["api_key"] = os.environ.get("GLM_API_KEY", values.get("api_key", ""))
        values["base_url"] = os.environ.get("GLM_BASE_URL", values.get("base_url", DEFAULT_BASE_URL))
        values["model"] = os.environ.get("GLM_MODEL", values.get("model", DEFAULT_MODEL))
        # CLI 显式参数层（None 表示未显式传入，跳过）
        for key, val in (cli_overrides or {}).items():
            if val is not None and key in allowed:
                values[key] = val

        cfg = cls(**values)
        cfg.config_warnings = warnings
        return cfg

    @property
    def llm_available(self) -> bool:
        return bool(self.api_key)

    def resolve_out_dir(self) -> Path:
        return Path(self.out_dir) if self.out_dir else Path(self.work_root) / "reports"


def _read_toml_section(path: Path, section: bool) -> tuple[dict[str, Any], list[str]]:
    """读取配置文件：section=False 读顶层键（.codeaudit.toml）；True 读 [tool.codeaudit] 节。"""
    allowed = {f.name for f in fields(AuditConfig)} - {"config_warnings", "config_file"}
    warnings: list[str] = []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {}, [f"配置文件 {path.name} 读取失败：{exc}"]
    if section:
        data = (data.get("tool") or {}).get("codeaudit") or {}
        if not isinstance(data, dict):
            return {}, [f"配置文件 {path.name} 的 [tool.codeaudit] 节格式非法"]
    values: dict[str, Any] = {}
    for key, val in data.items():
        if key in allowed and val is not None:
            values[key] = val
        else:
            warnings.append(f"配置文件 {path.name} 含未知/非法键：{key}")
    return values, warnings
