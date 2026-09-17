"""配置文件明文密钥扫描（CFG-SECRET）：.env / *.yaml / *.yml / *.properties / *.ini / *.toml。

按"当前行 key[:=]value"抓取：key 名命中敏感词（password/secret/token/api_key/
access_key/private_key 等，大小写不敏感）且值非空、非占位 → 报
security/critical Issue，code_snippet 的值打码为 ********。

已知限制（第一版，诚实记录）：
- ingest 阶段按项目 .gitignore 剪枝工作副本（audit.ingest.core._build_manifests
  会删除 should_ignore 命中的文件）：若 .env 被 .gitignore 忽略，文件不进工作
  副本，本扫描器自然扫不到；
- pyproject.toml 一律跳过：其敏感字段常见于 [tool.*] 节，且 [project] 元数据
  （authors.email 等）极易误报；密钥放在 pyproject 本身就是反模式，收益有限；
- yaml 只做"当前行 key: value"抓取，不做完整 YAML 解析：多行值（|/>）、
  锚点(&/*)、flow 风格嵌套（{password: x}）不支持；
- 只检测"key 名敏感 + 值非占位"，不做熵值校验：语义型误报/漏报可能存在；
- 值内注释按" #（# 前有空白）剥除"，含该形态的合法值会被截断判断。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from audit.models import Category, Issue, IssueSource, Severity

# 规则 ID（Issue.evidence 以 "rule:CFG-SECRET" 引用）
RULE_ID = "CFG-SECRET"

# 敏感 key 名（大小写不敏感）：password/passwd/secret/token/api_key/access_key/private_key
SECRET_KEY_RE = re.compile(r"(?i)(passw(or)?d|secret|token|api_?key|access_?key|private_?key)")

# "key: value" / "key = value" 行（key 允许点/中括号/连字符；yaml 列表项 "- " 前缀兼容）
_CONFIG_LINE_RE = re.compile(
    r"""^\s*(?:-\s+)?(?P<key>["']?[\w.\[\]-]+["']?)\s*(?P<sep>[:=])\s*(?P<val>.+?)\s*$"""
)

# 占位值前缀（小写化后比对）：任务口径 —— <your/changeme/xxx/placeholder/example/sample/test
_PLACEHOLDER_PREFIXES = ("<your", "changeme", "xxx", "placeholder", "example", "sample", "test")
# 占位/空值集合
_PLACEHOLDER_EXACT = {"", "true", "false", "none", "null", "nil", "yes", "no"}

# 值打码占位（与 detect 规则的 mask 口径一致）
MASK = "********"

# 扫描的配置文件形态：.env / .env.*；*.yaml/*.yml/*.properties/*.ini/*.toml
_CFG_SUFFIXES = {".yaml", ".yml", ".properties", ".ini", ".toml"}


def is_config_file(name: str) -> bool:
    """按文件名判定是否为目标配置文件（pyproject.toml 由调用方跳过）。"""
    lower = name.lower()
    if lower == ".env" or lower.startswith(".env."):
        return True
    return Path(lower).suffix in _CFG_SUFFIXES


def is_placeholder(value: str) -> bool:
    """占位值豁免：模板变量、占位前缀、布尔/空语义值、纯打码星号。"""
    v = value.strip().lower()
    if v in _PLACEHOLDER_EXACT or set(v) <= {"*"}:
        return True
    if "${" in v or "{{" in v:  # 环境变量/模板引用，非明文
        return True
    return any(v.startswith(p) for p in _PLACEHOLDER_PREFIXES)


def _strip_quotes(value: str) -> str:
    """剥离值两侧成对的引号。"""
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def scan_config_text(text: str) -> list[tuple[int, str, str]]:
    """扫描配置文本，返回 (行号, key 名, 打码后整行) 列表。

    只抓取"当前行 key[:=] value"形态；命中条件：key 名命中敏感词正则、
    值（去引号/去注释后）非空且非占位。
    """
    findings: list[tuple[int, str, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\r\n")
        if not line.strip() or line.strip().startswith("#"):
            continue
        m = _CONFIG_LINE_RE.match(line)
        if m is None:
            continue
        key = m.group("key").strip("'\"")
        if SECRET_KEY_RE.search(key) is None:
            continue
        val_text = m.group("val")
        if " #" in val_text:  # 行尾注释剥除（yaml/env 常见形态）
            val_text = val_text.split(" #", 1)[0].strip()
        value = _strip_quotes(val_text)
        if not value.strip() or is_placeholder(value):
            continue
        # 打码：替换去引号后的值文本（保留 key/分隔符/引号上下文）
        masked = line.replace(value, MASK, 1) if value else line
        findings.append((lineno, key, masked))
    return findings


def scan_source_root(src_root: Path, max_files: int = 500) -> list[Issue]:
    """扫描 src_root 下全部配置文件，产出 CFG-SECRET Issue 列表。

    忽略目录与上限由调用方（scanner）统一控制时也可直接逐文件调用
    scan_config_file；本函数自带目录剪枝与文件数上限（防大仓库拖垮）。
    """
    issues: list[Issue] = []
    count = 0
    for dirpath, dirnames, filenames in os.walk(src_root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        for name in sorted(filenames):
            if not is_config_file(name):
                continue
            if name.lower() == "pyproject.toml":
                continue  # 见模块 docstring：pyproject 一律跳过
            if count >= max_files:
                return issues
            count += 1
            path = Path(dirpath) / name
            try:
                rel = path.relative_to(src_root).as_posix()
            except ValueError:  # pragma: no cover - 越界回退文件名
                rel = name
            issues.extend(scan_config_file(path, rel_path=rel))
    return issues


def scan_config_file(path: Path, rel_path: str | None = None) -> list[Issue]:
    """扫描单个配置文件为 Issue 列表；读取失败返回空（不抛）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        try:
            text = path.read_text(encoding="gbk")
        except (OSError, UnicodeDecodeError):
            return []
    rel = rel_path or path.name
    findings = scan_config_text(text)
    return [_make_issue(rel, lineno, key, masked) for lineno, key, masked in findings]


def _make_issue(rel_path: str, lineno: int, key: str, masked_line: str) -> Issue:
    """构造 CFG-SECRET Issue（id 留空由 engine 统一补号）。"""
    return Issue(
        category=Category.SECURITY,
        severity=Severity.CRITICAL,
        title=f"配置文件疑似明文密钥：{key}（{rel_path}:{lineno}）"[:120],
        file=rel_path,
        line_start=lineno,
        line_end=lineno,
        code_snippet=masked_line,
        description=(
            f"配置文件 {rel_path} 第 {lineno} 行的键 {key} 疑似硬编码了明文凭据。"
            "配置文件中的密钥会随仓库分发与镜像构建泄露，属于高危暴露面。"
        ),
        evidence=[
            f"rule:{RULE_ID}",
            f"loc:{rel_path}:{lineno}",
            f"key:{key}",
        ],
        suggestion=(
            "将明文凭据移出配置文件：改用环境变量注入或密钥管理服务（如 Vault/KMS），"
            "配置文件中仅保留 ${ENV_VAR} 引用；已泄露的密钥应立即轮换。"
        ),
        confidence=0.7,
        source=IssueSource.RULE,
    )


# 扫描忽略目录（与 scanner 主口径一致，此处独立可测）
_IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".codeaudit",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
    "env",
    ".idea",
    ".vscode",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
    "coverage",
}
