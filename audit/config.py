"""审计配置。环境变量优先级高于默认值；from_env 自动加载 .env（W12-A1/F7）。

契约 v1.4（docs/09 §3）：新增配置文件合成 from_sources()——
合成优先级：默认值 < 配置文件（.codeaudit.toml / pyproject [tool.codeaudit]）< 环境变量(GLM_*) < CLI 显式参数。

F7 .env 自动加载（docs/17 §2 W12-A1；早期版本"不自动加载 .env"的语义自 W12 起废止）：
- 查找顺序：显式 env_file 参数 → CWD/.env；文件不存在静默跳过；
- 解析：零依赖手写——KEY=VALUE、# 注释行、export KEY=VALUE 前缀、值的首尾引号
  剥离（单双引号）、空行跳过；解析失败的行静默跳过；
- 优先级（关键）：真实进程环境变量永远优先于 .env——.env 只补"进程环境尚不存在
  的键"，绝不覆盖用户运行中修改的值；模块级 _ENV_LOADED 标志保证同进程至多实际
  加载一次；
- 写入策略分两层：GLM_* 三键只合并进本次 from_env 取值（不写 os.environ——避免
  污染全局进程环境、误伤 from_sources 等其他环境消费者）；CODEAUDIT_* 三键以
  setdefault 写入 os.environ（server 侧运行期直接读环境，必须经进程环境传递）；
- 作用范围：只认白名单键 GLM_API_KEY / GLM_BASE_URL / GLM_MODEL /
  CODEAUDIT_MAX_RUNNING / CODEAUDIT_MAX_PENDING / CODEAUDIT_DB_PATH，
  其余键一律忽略（防 .env 意外注入无关环境变量）。server 侧并发准入上限
  （W14-A3 起）与 DB 路径同为惰性解析：lifespan 中 from_env 加载 .env 后
  取值即生效，import 时不再固化。
- W15-A1（docs/20 §4.1）：server 安全治理三键 CODEAUDIT_API_TOKEN /
  CODEAUDIT_SOURCE_ROOTS / CODEAUDIT_RATE_LIMIT 为真实进程环境直读（不进 .env
  白名单，与 CODEAUDIT_SANDBOX_BACKEND 同口径），全部 opt-in，缺省 = 治理关闭。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_MODEL = "glm-5.3-flash"

# F7：.env 白名单——GLM_* 三键合并进本次 from_env 取值（不写 os.environ）；
# CODEAUDIT_* 三键 setdefault 进 os.environ（server 侧直接读环境，须经进程环境传递）
_DOTENV_GLM_KEYS = frozenset({"GLM_API_KEY", "GLM_BASE_URL", "GLM_MODEL"})
_DOTENV_SERVER_KEYS = frozenset(
    {"CODEAUDIT_MAX_RUNNING", "CODEAUDIT_MAX_PENDING", "CODEAUDIT_DB_PATH"}
)
_DOTENV_KEYS = _DOTENV_GLM_KEYS | _DOTENV_SERVER_KEYS

# W14-A2（M-2a）：沙箱后端环境变量。真实进程环境直读（from_env / from_sources
# 两个入口同一键名），不经 .env 白名单——与 CODEAUDIT_DOCKER_IMAGE 的直读口径一致。
_SANDBOX_BACKEND_ENV = "CODEAUDIT_SANDBOX_BACKEND"

# W15-A1（docs/20 §4.1）：server 安全治理三键。真实进程环境直读（from_env /
# from_sources 两个入口同一键名），不经 .env 白名单——与 CODEAUDIT_SANDBOX_BACKEND
# 的直读口径一致；全部 opt-in（env 缺省 = 治理关闭，既有行为零变化）。
_API_TOKEN_ENV = "CODEAUDIT_API_TOKEN"  # codeaudit: ignore[PY-HARDCODED-SECRET] 这是环境变量名而非密钥值（W21 卡2 定性）
_SOURCE_ROOTS_ENV = "CODEAUDIT_SOURCE_ROOTS"
_RATE_LIMIT_ENV = "CODEAUDIT_RATE_LIMIT"

# F7：同进程只加载一次标志（实际发生加载后才置位；见 _load_dotenv）
_ENV_LOADED = False


def _parse_source_roots(raw: str | None) -> list[str]:
    """W15-A1：解析 CODEAUDIT_SOURCE_ROOTS——os.pathsep 分隔，剥首尾空白，空段丢弃。

    空串 / None（env 未设）返回空列表 = 白名单关闭（不限制 source_path）。
    """
    if not raw:
        return []
    return [seg.strip() for seg in raw.split(os.pathsep) if seg.strip()]


def _parse_rate_limit(raw: str | None) -> int:
    """W15-A1：解析 CODEAUDIT_RATE_LIMIT——非法 / 0 / 负数一律回落 0（限流关闭）。"""
    if raw is None:
        return 0
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if value > 0 else 0


def _parse_dotenv_line(line: str) -> tuple[str, str] | None:
    """解析 .env 单行为 (key, value)；空行/注释行/无 '=' 的坏行返回 None（静默跳过）。

    支持：KEY=VALUE、export KEY=VALUE 前缀、值的首尾成对单/双引号剥离、
    键值两侧空白容忍；值内含 '=' 时按第一个 '=' 切分。
    """
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if text.startswith("export ") or text.startswith("export\t"):
        text = text[len("export"):].lstrip()
    key, sep, value = text.partition("=")
    if not sep:
        return None
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    if not key:
        return None
    return key, value


def _load_dotenv(env_file: str | Path | None) -> dict[str, str]:
    """加载 .env（F7，零依赖手写解析），返回 .env 提供的 GLM_* 键值（供 from_env 合并）。

    - 路径：显式 env_file 优先；缺省自动发现 CWD/.env；文件不存在/不可读返回空
      dict 并静默跳过（不置标志，后续调用仍可重试）；
    - 写入策略：CODEAUDIT_* 键以 setdefault 写入 os.environ（server 侧运行期直接读
      环境，必须经进程环境传递，且绝不覆盖已有值）；GLM_* 键不写 os.environ——仅经
      返回值合并进本次 from_env 取值，避免污染全局进程环境、误伤 from_sources 等
      其他环境消费者（"真实环境变量优先"在 from_env 内逐键保证）；
    - 模块级 _ENV_LOADED 标志保证同进程至多实际加载一次。线程安全性：GIL 下标志
      读写近似原子；极端竞态下最坏重复解析一次，setdefault/合并均幂等，无实际危害。
    """
    global _ENV_LOADED
    if _ENV_LOADED:
        return {}
    path = Path(env_file) if env_file is not None else Path.cwd() / ".env"
    try:
        # utf-8-sig：容忍带 BOM 的 .env（首键不被 \ufeff 破坏），无 BOM 时行为一致
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return {}
    merged: dict[str, str] = {}
    for line in text.splitlines():
        parsed = _parse_dotenv_line(line)
        if parsed is None:
            continue
        key, value = parsed
        if key in _DOTENV_SERVER_KEYS:
            os.environ.setdefault(key, value)
        elif key in _DOTENV_GLM_KEYS:
            merged[key] = value
    _ENV_LOADED = True
    return merged


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
    # max_tool_iterations（W14-A2 M-1 接线）：Review/工具取证路径 LLM 工具循环的
    # 迭代上限（NFR-10"上限可配置"的唯一事实来源，消费点 audit/agents/review.py）。
    # 默认 12 与历史硬编码行为一致（原默认 25 全库无消费点，属死配置）。
    max_tool_iterations: int = 12
    # W22-A：tools 审查路径工具循环的消息总字符预算（消费点 audit/agents/review.py →
    # AgentLimits.message_budget_chars；runtime 从最旧 tool 消息折叠 content）。
    # 0=关闭折叠；默认 60_000 字符（在线实测 completion:prompt=3:1 异常的主因是
    # 工具结果逐轮累积不裁剪，见审计 3.3-2）。
    agent_message_budget: int = 60_000
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

    # W22-B（审计 3.1-1/3.3-1）：风险聚焦两级审计——LLM 通道只深审风险分 top-N
    # 文件，其余文件仅静态规则通道（规则候选仍全量入报告）。0=全量（默认，行为
    # 零变化）；>0=深审文件数上限。风险分 = 规则命中按 SEVERITY_WEIGHT 加权，
    # 无命中文件按行数降序垫底；接线在 audit.detect.engine.run_detection。
    # 默认值翻转需待 bench.real_run 的 full vs llm_focus 对数（P50 ≤ 300 s/KLOC
    # 且 Precision 降幅 ≤3pp），见 docs/19。
    llm_review_top_files: int = 0

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

    # Wave 14（W14-A2，M-2a 开关接线）：沙箱后端。合法值 subprocess/docker；
    # 默认 subprocess（W13 收口裁决：CI 实证自动启用 Docker 会把宿主解释器包进
    # 容器导致全 127，必须显式 opt-in）。非法值由 SandboxExecutor 诚实降级
    # subprocess 并在进度事件中标注。分层：默认值 < 配置文件 < 环境变量
    # CODEAUDIT_SANDBOX_BACKEND < CLI --sandbox-backend。
    sandbox_backend: str = "subprocess"

    # Wave 15（W15-A1，docs/20 §4.1）：server 安全治理，全部 opt-in（缺省 = 既有行为，
    # 单点回滚 = 不设对应 env）。分层：默认值 < 配置文件 < 环境变量（与沙箱后端键同层）。
    # 非空时 /api/*（除 GET /api/health）要求 Authorization: Bearer 或 X-API-Token，
    # 失败 401；空 = 鉴权关闭（本机开发态默认）。
    api_token: str = ""
    # 非空时 POST /api/audits 的 source_path 必须 resolve 后位于任一根之内，越界 400；
    # 空 = 不限制。env CODEAUDIT_SOURCE_ROOTS 以 os.pathsep 分隔多个根。
    allowed_source_roots: list[str] = field(default_factory=list)
    # >0 时对写方法（POST/DELETE）按客户端 IP 做内存滑动窗口限流，超限 429；0 = 关闭。
    rate_limit_per_min: int = 0

    # Wave 22（W22-C，审计 2.3-1/P0-4）：规则级配置暴露——按项目裁剪审计口径。
    # 三者全部默认空 = 既有行为零变化；接线在 audit.detect.engine（run_detection /
    # build_rule_contexts）。disabled_rules 与 ignore_paths 另有 CLI 显式参数
    # （--disable-rule / --ignore-path，可多次）；severity_overrides 只走配置文件
    # （键 = 规则 id，值 = critical|high|medium|low）。
    # 注意：ignore_paths 只影响检测阶段（规则扫描与 LLM 审查文件清单）；索引构建与
    # 后处理扫描器（克隆/死代码/依赖）不受此过滤。
    disabled_rules: list[str] = field(default_factory=list)
    severity_overrides: dict[str, str] = field(default_factory=dict)
    ignore_paths: list[str] = field(default_factory=list)

    @classmethod
    def from_env(
        cls,
        source_path: str | None = None,
        *,
        env_file: str | Path | None = None,
        **overrides: object,
    ) -> "AuditConfig":
        """从进程环境构造配置；首次调用自动加载 .env（F7，语义见模块 docstring）。

        取值优先级：显式 overrides（None 跳过）> 真实进程环境变量 > .env（仅补环境
        尚不存在的键，且不落 os.environ）> 默认值。env_file：显式 .env 路径；None 时
        自动发现 CWD/.env；文件不存在静默跳过；同进程至多实际加载一次
        （_ENV_LOADED 标志）。
        """
        dotenv_values = _load_dotenv(env_file)
        env_map = {
            "api_key": ("GLM_API_KEY", ""),
            "base_url": ("GLM_BASE_URL", DEFAULT_BASE_URL),
            "model": ("GLM_MODEL", DEFAULT_MODEL),
        }
        values = {}
        for key, (env, default) in env_map.items():
            # 优先级：真实进程环境变量 > .env（仅补环境缺失键）> 默认值
            if env in os.environ:
                values[key] = os.environ[env]
            elif env in dotenv_values:
                values[key] = dotenv_values[env]
            else:
                values[key] = default
        if source_path is not None:
            values["source_path"] = source_path
        # W14-A2（M-2a）：沙箱后端环境变量层（显式 overrides 仍高于环境变量——
        # 下面的 values.update 覆盖在此之后执行）。值不在此处校验：非法值由
        # SandboxExecutor 诚实降级 subprocess 并在事件中标注。
        if _SANDBOX_BACKEND_ENV in os.environ:
            values["sandbox_backend"] = os.environ[_SANDBOX_BACKEND_ENV]
        # W15-A1：server 安全治理三键的 env 层（真实进程环境 > 默认；显式 overrides
        # 仍高于环境变量——下方 values.update 覆盖在此之后执行）。
        if _API_TOKEN_ENV in os.environ:
            values["api_token"] = os.environ[_API_TOKEN_ENV]
        if _SOURCE_ROOTS_ENV in os.environ:
            values["allowed_source_roots"] = _parse_source_roots(os.environ[_SOURCE_ROOTS_ENV])
        if _RATE_LIMIT_ENV in os.environ:
            values["rate_limit_per_min"] = _parse_rate_limit(os.environ[_RATE_LIMIT_ENV])
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
        # 环境变量层（GLM 三键与 from_env 口径一致；W14-A2 增沙箱后端键——
        # 配置文件值可被环境变量覆盖，CLI 显式参数层再覆盖环境变量）。
        # W16 修复（README 承诺对齐）：from_sources 同样自动加载 CWD/.env——
        # 此前仅 from_env（server/bench.real_run 路径）加载，CLI 填了 .env 仍被
        # 判定"LLM 未配置"。口径与 from_env 一致：真实进程环境 > .env > 默认值；
        # GLM_* 只合并不写 os.environ，_ENV_LOADED 标志保证同进程至多加载一次，
        # 无 .env 时 dotenv_values 为空 dict，行为零变化。
        dotenv_values = _load_dotenv(None)
        values["api_key"] = os.environ.get(
            "GLM_API_KEY", dotenv_values.get("GLM_API_KEY", values.get("api_key", ""))
        )
        values["base_url"] = os.environ.get(
            "GLM_BASE_URL", dotenv_values.get("GLM_BASE_URL", values.get("base_url", DEFAULT_BASE_URL))
        )
        values["model"] = os.environ.get(
            "GLM_MODEL", dotenv_values.get("GLM_MODEL", values.get("model", DEFAULT_MODEL))
        )
        values["sandbox_backend"] = os.environ.get(
            _SANDBOX_BACKEND_ENV, values.get("sandbox_backend", "subprocess")
        )
        # W15-A1：server 安全治理三键的 env 层（配置文件值可被环境变量覆盖，
        # CLI 显式参数层再覆盖环境变量；解析规则见 _parse_source_roots/_parse_rate_limit）
        values["api_token"] = os.environ.get(_API_TOKEN_ENV, values.get("api_token", ""))
        if _SOURCE_ROOTS_ENV in os.environ:
            values["allowed_source_roots"] = _parse_source_roots(os.environ[_SOURCE_ROOTS_ENV])
        if _RATE_LIMIT_ENV in os.environ:
            values["rate_limit_per_min"] = _parse_rate_limit(os.environ[_RATE_LIMIT_ENV])
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
