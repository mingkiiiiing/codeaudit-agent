"""依赖与配置安全后处理扫描器总入口（W16 验收短板清偿）。

挂点契约（audit.detect.engine._post_scan_issues）：
- engine 延迟 import 本模块并调用 run(ctx) -> list[Issue]（ctx 为 PipelineContext）；
- run() 永不抛异常：内部全部 try/except，子模块失败把 repr 记入
  ctx.extra["post_scan_errors"]["depcheck.<子模块名>"] 后返回空/部分结果；
- 返回的 Issue 不填 id（engine 统一补号），category=SECURITY/STYLE，
  severity、title、file（相对 src_root 的 posix 路径）、真实行号、
  evidence（含 rule:DEP-... / rule:CFG-SECRET）、suggestion、confidence=0.7、
  source=IssueSource.RULE 齐备。

环境开关：
- CODEAUDIT_DISABLE_POST_SCAN：engine 侧总开关（"1" 全关；含
  "audit.depcheck.scanner" 时本卡全关），本模块不重复检查；
- CODEAUDIT_DEPCHECK_OFF=1：只关依赖清单扫描（DEP-CVE / DEP-DUPLICATE /
  DEP-UNUSED / OSV 在线）；
- CODEAUDIT_CFGSECRET_OFF=1：只关配置明文密钥扫描（CFG-SECRET）；
- CODEAUDIT_OSV_ONLINE=1：opt-in 启用 OSV 在线漏洞查询（osv.py），与
  种子库命中同（文件, CVE）去重；默认未设置时完全不触网，种子库离线
  路径零变化。

冗余依赖（DEP-UNUSED，style/low）：清单声明但全部源码（.py/.js/.ts）
零 import/require 引用的依赖。防误报口径（诚实记录）：
- 项目内不存在对应生态源码（pypi 清单→无任何 .py；npm 清单→无任何
  .js/.ts）时视为"无源码可证"跳过该清单（纯部署/锁定清单场景）；
- npm devDependencies、@types/* 包、node 内置模块同名依赖不报；
- 动态导入/插件式加载/仅文档引用会漏判或误判，Issue 一律 low 并提示
  人工确认。

已知限制（第一版）：源码读取预算 MAX_UNUSED_SOURCE_FILES=1000（超出
部分不参与引用判定）；清单/配置扫描文件数上限 MAX_SCAN_FILES=500；
package-lock.json（W19 追加）只出 DEP-CVE（传递依赖精确版本命中种子库），
不出 UNUSED/重复，文件 >5MB 跳过并记 post_scan_errors，条目超 5000 只扫
前 5000（manifest.MAX_LOCK_ENTRIES）。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from audit.models import Category, Issue, IssueSource, Severity

from . import advisory as advisory_mod
from . import configsecret, manifest, osv as osv_mod

__all__ = ["run", "scan_dependencies", "MAX_SCAN_FILES"]

MAX_SCAN_FILES = 500  # 单次扫描文件数上限（清单 + 配置合计，防爆）

# package-lock.json 单文件大小上限（性能护栏：超过跳过解析并记 post_scan_errors）
LOCK_MAX_BYTES = 5 * 1024 * 1024

# 扫描忽略目录（对齐 audit.workspace.DEFAULT_IGNORE_DIRS 与任务指定五项）
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


def run(ctx: Any) -> list[Issue]:
    """挂点入口：依赖清单漏洞/重复扫描 + 配置明文密钥扫描。永不抛异常。"""
    issues: list[Issue] = []
    errors: dict[str, str] | None = None
    try:
        errors = ctx.extra.setdefault("post_scan_errors", {})
    except Exception:
        errors = None  # ctx.extra 缺失/不可用时只放弃记账，不影响扫描

    src_root = _resolve_src_root(ctx)
    if src_root is None:
        return []

    if os.environ.get("CODEAUDIT_DEPCHECK_OFF", "").strip() != "1":
        try:
            issues.extend(scan_dependencies(src_root))
        except Exception as exc:  # 依赖扫描故障不阻断密钥扫描
            if errors is not None:
                errors["depcheck.manifest"] = repr(exc)

        try:
            # W19 追加：package-lock.json 传递依赖 CVE 扫描（只出 DEP-CVE，
            # 不参与 UNUSED/重复/OSV；>5MB 跳过并记账，见 _lock_cve_issues）
            issues.extend(_lock_cve_issues(src_root, errors=errors))
        except Exception as exc:  # lock 扫描故障不阻断后续扫描
            if errors is not None:
                errors["depcheck.lock"] = repr(exc)

        if osv_mod.osv_enabled():  # opt-in：默认不触网
            try:
                issues.extend(_osv_online_step(src_root, existing=issues, errors=errors))
            except Exception as exc:  # 在线步骤故障不阻断后续扫描
                if errors is not None:
                    errors["depcheck.osv"] = repr(exc)

        try:
            issues.extend(_unused_dep_issues(src_root))
        except Exception as exc:  # 冗余扫描故障不阻断密钥扫描
            if errors is not None:
                errors["depcheck.unused"] = repr(exc)

    if os.environ.get("CODEAUDIT_CFGSECRET_OFF", "").strip() != "1":
        try:
            issues.extend(_scan_cfg_secret(src_root))
        except Exception as exc:  # 密钥扫描故障不阻断整体
            if errors is not None:
                errors["depcheck.cfgsecret"] = repr(exc)
    return issues


def _resolve_src_root(ctx: Any) -> Path | None:
    """防御式取 workspace.src_root：缺失/不存在返回 None（返回空结果）。"""
    try:
        src_root = getattr(getattr(ctx, "workspace", None), "src_root", None)
    except Exception:
        return None
    if not src_root:
        return None
    try:
        src_root = Path(src_root)
        if not src_root.is_dir():
            return None
    except (OSError, TypeError, ValueError):
        return None
    return src_root


def _collect_target_files(src_root: Path) -> tuple[list[tuple[Path, str]], list[Path]]:
    """一次遍历收集 (清单文件, 类型) 与配置文件；应用目录剪枝与文件数上限。"""
    manifests: list[tuple[Path, str]] = []
    configs: list[Path] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(src_root):
        dirnames[:] = sorted(d for d in dirnames if d not in _IGNORE_DIRS)
        for name in sorted(filenames):
            kind = manifest.classify_manifest(name)
            is_cfg = configsecret.is_config_file(name) and name.lower() != "pyproject.toml"
            if kind is None and not is_cfg:
                continue
            if total >= MAX_SCAN_FILES:
                return manifests, configs  # 预算用尽即停（docstring 限制已注明）
            total += 1
            path = Path(dirpath) / name
            if kind is not None:
                manifests.append((path, kind))
            else:
                configs.append(path)
    return manifests, configs


def scan_dependencies(src_root: Path) -> list[Issue]:
    """扫描全部清单文件：DEP-CVE（漏洞钉扎/区间相交）+ DEP-DUPLICATE（同文件重复钉扎）。"""
    manifests, _ = _collect_target_files(src_root)
    advisories = advisory_mod.load_advisories()
    issues: list[Issue] = []
    for path, kind in manifests:
        rel = _rel_posix(src_root, path)
        text = _read_text(path)
        if text is None:
            continue
        try:
            deps = _parse_manifest(text, kind)
        except Exception:
            continue  # 单文件解析失败不阻断整体（坏 JSON/TOML 等）
        issues.extend(_cve_issues(rel, deps, advisories))
        if kind == "requirements":
            issues.extend(_duplicate_issues(rel, deps))
    return issues


def _osv_online_step(src_root: Path, existing: list[Issue], errors: dict[str, str] | None) -> list[Issue]:
    """OSV 在线比对步骤：重放清单解析，把 (相对路径, deps) 交给 osv.osv_issues。"""
    manifests, _ = _collect_target_files(src_root)
    file_deps: list[tuple[str, list[manifest.Dependency]]] = []
    for path, kind in manifests:
        text = _read_text(path)
        if text is None:
            continue
        try:
            deps = _parse_manifest(text, kind)
        except Exception:
            continue  # 与种子库路径同口径：坏清单跳过
        file_deps.append((_rel_posix(src_root, path), deps))
    return osv_mod.osv_issues(file_deps, existing=existing, errors=errors)


def _scan_cfg_secret(src_root: Path) -> list[Issue]:
    """配置明文密钥扫描（复用 configsecret 的目录遍历与打码逻辑）。"""
    _, configs = _collect_target_files(src_root)
    issues: list[Issue] = []
    for path in configs:
        rel = _rel_posix(src_root, path)
        try:
            issues.extend(configsecret.scan_config_file(path, rel_path=rel))
        except Exception:
            continue  # 单文件失败不阻断
    return issues


# ---------------------------------------------------------------- package-lock.json 传递依赖（W19）


def _collect_lock_files(src_root: Path) -> list[Path]:
    """收集 package-lock.json（与 _collect_target_files 同一套目录剪枝与预算）。"""
    locks: list[Path] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(src_root):
        dirnames[:] = sorted(d for d in dirnames if d not in _IGNORE_DIRS)
        for name in sorted(filenames):
            if name.lower() != "package-lock.json":
                continue
            if total >= MAX_SCAN_FILES:
                return locks  # 预算用尽即停（docstring 限制已注明）
            total += 1
            locks.append(Path(dirpath) / name)
    return locks


def _lock_cve_issues(src_root: Path, errors: dict[str, str] | None = None) -> list[Issue]:
    """package-lock.json 传递依赖漏洞扫描（W19 清偿）：只出 DEP-CVE。

    - 不参与 DEP-UNUSED / DEP-DUPLICATE / OSV 在线（lock 不是声明清单，是
      传递依赖锁定产物，冗余语义无意义）；
    - 性能护栏：单文件 > LOCK_MAX_BYTES（5MB）跳过并记 post_scan_errors
      （键 "depcheck.lock:<相对路径>"）；条目数上限见 manifest.MAX_LOCK_ENTRIES；
    - 条目行号回查失败（压缩单行等）时回退第 1 行：lock 文件第 1 行必为
      "{"，条目不可能合法落在第 1 行，故 line_start==1 即回退，在 evidence
      追加 "line_fallback:1" 标注。
    """
    locks = _collect_lock_files(src_root)
    if not locks:
        return []
    advisories = advisory_mod.load_advisories()
    issues: list[Issue] = []
    for path in locks:
        rel = _rel_posix(src_root, path)
        try:
            size = path.stat().st_size
        except OSError as exc:
            if errors is not None:
                errors[f"depcheck.lock:{rel}"] = repr(exc)
            continue
        if size > LOCK_MAX_BYTES:
            if errors is not None:
                errors[f"depcheck.lock:{rel}"] = (
                    f"LockFileTooLarge(size={size} > limit={LOCK_MAX_BYTES})，跳过解析"
                )
            continue
        try:
            deps = manifest.parse_package_lock(path)
        except Exception:
            continue  # 坏 JSON/不可读与既有清单路径同口径：跳过
        issues.extend(_cve_issues(rel, deps, advisories))
    for issue in issues:
        if issue.line_start == 1:
            issue.evidence.append("line_fallback:1")
    return issues


# ---------------------------------------------------------------- Issue 构造


def _cve_issues(rel: str, deps: list[manifest.Dependency], advisories: list) -> list[Issue]:
    """DEP-CVE-<CVE编号>：severity 按 CVSS 阈值映射，行号取约束所在行。"""
    issues: list[Issue] = []
    for dep, adv in advisory_mod.match_advisories(deps, advisories):
        line = next((c.line for c in dep.constraints if c.line > 0), 1)
        rule_id = f"DEP-CVE-{adv.cve}"
        fixed_hint = f"升级到 >= {adv.fixed}" if adv.fixed else "关注上游修复版本"
        range_text = f">= {adv.introduced}" if adv.introduced != "0.0.0" else "所有已发布版本"
        upper_text = f" < {adv.fixed}" if adv.fixed else ""
        issues.append(
            Issue(
                category=Category.SECURITY,
                severity=advisory_mod.severity_from_cvss(adv.cvss),
                title=f"依赖 {dep.raw_name} 命中已知漏洞 {adv.cve}"[:120],
                file=rel,
                line_start=line,
                line_end=line,
                code_snippet=f"{dep.raw_name}: {adv.cve}（CVSS {adv.cvss:g}）",
                description=(
                    f"清单 {rel} 第 {line} 行声明的依赖 {dep.raw_name}（{dep.ecosystem}）"
                    f"版本约束与已知漏洞 {adv.cve} 的影响区间相交："
                    f"{range_text}{upper_text}。{adv.summary}"
                ),
                evidence=[
                    f"rule:{rule_id}",
                    f"cve:{adv.cve}",
                    f"cvss:{adv.cvss:g}",
                    f"introduced:{adv.introduced}",
                    f"fixed:{adv.fixed or '未知'}",
                    f"loc:{rel}:{line}",
                ],
                suggestion=(
                    f"{fixed_hint} 并验证兼容性；若短期无法升级，评估官方缓解措施"
                    f"（见 {adv.reference}）。"
                ),
                confidence=0.7,
                source=IssueSource.RULE,
            )
        )
    return issues


def _duplicate_issues(rel: str, deps: list[manifest.Dependency]) -> list[Issue]:
    """DEP-DUPLICATE：同一清单文件内同一包被两行钉扎为不同精确版本。

    两行 == 不同版本时安装行为取决于 pip 解析次序，极易引入非预期版本
    （可能恰好落在漏洞区间），故归为安全问题（style/low）。
    """
    pinned: dict[str, list[tuple[manifest.Dependency, str, int]]] = {}
    for dep in deps:
        for c in dep.constraints:
            if c.op == "==" and c.version:
                pinned.setdefault(dep.name, []).append((dep, c.version, c.line))
                break  # 同一行多 == 只取首个（罕见形态，避免重复计）

    issues: list[Issue] = []
    for name, entries in pinned.items():
        if len(entries) < 2:
            continue
        versions = [v for _d, v, _l in entries]
        if len(set(versions)) < 2:
            continue  # 同版本重复声明：冗余但非安全项
        first, second = entries[0], entries[-1]
        loc_lines = ", ".join(str(ln) for _d, _v, ln in entries)
        issues.append(
            Issue(
                category=Category.STYLE,
                severity=Severity.LOW,
                title=f"依赖 {first[0].raw_name} 在清单中被钉扎了不同版本（{rel}）"[:120],
                file=rel,
                line_start=second[2],
                line_end=second[2],
                code_snippet=f"{first[0].raw_name}=={versions[0]} 与 =={versions[-1]}",
                description=(
                    f"清单 {rel} 中依赖 {first[0].raw_name} 被多行钉扎为不同精确版本"
                    f"（第 {loc_lines} 行：{', '.join(versions)}），实际安装版本取决于"
                    "解析次序，可能引入未审计的旧版本并踩中已知漏洞。"
                ),
                evidence=[
                    "rule:DEP-DUPLICATE",
                    f"package:{name}",
                    f"versions:{','.join(sorted(versions))}",
                    f"loc:{rel}:{loc_lines}",
                ],
                suggestion=(
                    f"只保留一行 {first[0].raw_name} 的精确钉扎（或多环境用约束文件分层），"
                    "删除其余重复声明。"
                ),
                confidence=0.7,
                source=IssueSource.RULE,
            )
        )
    return issues


# ---------------------------------------------------------------- 冗余依赖（DEP-UNUSED）

# 冗余源码引用判定的文件数上限（.py/.js/.ts 合计，超出截断并诚实降级）
MAX_UNUSED_SOURCE_FILES = 1000

# 知名"清单名 → import 名"映射（canonical 包名 → 额外候选 import 名）。
# 未命中的包用 PEP503 归一名与 -/_ 互换启发式兜底。
PYPI_IMPORT_ALIASES: dict[str, tuple[str, ...]] = {
    "pillow": ("PIL",),
    "beautifulsoup4": ("bs4",),
    "pyyaml": ("yaml",),
    "python-dateutil": ("dateutil",),
    "scikit-learn": ("sklearn",),
    "sklearn": ("sklearn",),  # 旧包名
    "opencv-python": ("cv2",),
    "opencv-contrib-python": ("cv2",),
    "opencv-python-headless": ("cv2",),
    "pycryptodome": ("Crypto",),
    "pycrypto": ("Crypto",),
    "python-dotenv": ("dotenv",),
    "python-jose": ("jose",),
    "python-magic": ("magic",),
    "pyjwt": ("jwt",),
    "pynacl": ("nacl",),
    "python-ldap": ("ldap",),
    "protobuf": ("google",),  # import google.protobuf
    "pywin32": ("win32api", "win32con", "win32com", "pythoncom"),
}

# node 内置模块白名单：同名依赖（误声明）不做冗余报告
_NODE_BUILTINS = frozenset(
    {
        "assert", "buffer", "child_process", "cluster", "crypto", "dgram", "dns",
        "events", "fs", "http", "https", "module", "net", "os", "path", "process",
        "querystring", "readline", "stream", "string_decoder", "tls", "tty", "url",
        "util", "v8", "vm", "worker_threads", "zlib",
    }
)

# JS/TS 引用提取：require('x') / import('x') / ... from 'x' / import 'x'
_JS_SPEC_RE = re.compile(r"""(?:require\s*\(\s*|import\s*\(\s*|from\s+|import\s+)["']([^"'\n]+)["']""")
# python from 子句模块路径
_PY_FROM_RE = re.compile(r"^from\s+([\w.]+)")
# python import 子句（含逗号列表与 as 别名）
_PY_IMPORT_RE = re.compile(r"^import\s+(.+)$")


def _collect_source_corpus(src_root: Path) -> tuple[str, bool, str, bool]:
    """遍历源码拼接 (python 文本, 有 .py, js/ts 文本, 有 .js/.ts)。

    应用 _IGNORE_DIRS 剪枝；文件数达 MAX_UNUSED_SOURCE_FILES 截断（超出
    部分不参与引用判定，属已知限制）。
    """
    py_parts: list[str] = []
    js_parts: list[str] = []
    has_py = False
    has_js = False
    count = 0
    for dirpath, dirnames, filenames in os.walk(src_root):
        dirnames[:] = sorted(d for d in dirnames if d not in _IGNORE_DIRS)
        for name in sorted(filenames):
            lower = name.lower()
            if lower.endswith(".py"):
                has_py = True
            elif lower.endswith((".js", ".ts")):
                has_js = True
            else:
                continue
            if count >= MAX_UNUSED_SOURCE_FILES:
                continue  # 预算用尽仍完成存在性探测，但不再读内容
            path = Path(dirpath) / name
            text = _read_text(path)
            if text is None:
                continue
            count += 1
            if lower.endswith(".py"):
                py_parts.append(text)
            else:
                js_parts.append(text)
    return "\n".join(py_parts), has_py, "\n".join(js_parts), has_js


def _python_import_modules(text: str) -> set[str]:
    """提取源码中被 import/from 引用的模块路径集合（含缩进与 as 别名）。

    - "from X import ..." → X（含相对导入形态，不匹配任何清单包名，无害）；
    - "import A.B as c, d" → {"A.B", "d"}。
    """
    mods: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        m = _PY_FROM_RE.match(line)
        if m is not None:
            mods.add(m.group(1))
            continue
        m = _PY_IMPORT_RE.match(line)
        if m is not None:
            for part in m.group(1).split(","):
                name = part.strip().split(" as ")[0].strip()
                if name and re.fullmatch(r"[\w.]+", name):
                    mods.add(name)
    return mods


def _js_specifiers(text: str) -> set[str]:
    """提取 JS/TS 源码中的模块说明符集合（require/import/from/export-from）。"""
    return set(_JS_SPEC_RE.findall(text))


def _pypi_dep_used(dep: manifest.Dependency, py_mods: set[str]) -> bool:
    """python 依赖是否被引用：候选 import 名与模块路径精确/前缀匹配。"""
    candidates = {dep.name, dep.name.replace("-", "_")}
    candidates.update(PYPI_IMPORT_ALIASES.get(dep.name, ()))
    for mod in py_mods:
        for cand in candidates:
            if mod == cand or mod.startswith(cand + "."):
                return True
    return False


def _npm_dep_used(dep: manifest.Dependency, specs: set[str]) -> bool:
    """npm 依赖是否被引用：说明符精确相等或子路径前缀（含 @scope/pkg/x）。"""
    prefix = dep.name + "/"
    return any(s == dep.name or s.startswith(prefix) for s in specs)


def _npm_prod_names(text: str) -> set[str] | None:
    """package.json 的 dependencies（生产）键集合；解析失败返回 None。"""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    table = data.get("dependencies") if isinstance(data, dict) else None
    if not isinstance(table, dict):
        return set()
    return {manifest.canonical_npm_name(k) for k in table if isinstance(k, str)}


def _unused_dep_issues(src_root: Path) -> list[Issue]:
    """DEP-UNUSED：清单声明但全部源码零引用的依赖（style/low，提示人工确认）。"""
    manifests, _ = _collect_target_files(src_root)
    if not manifests:
        return []
    py_text, has_py, js_text, has_js = _collect_source_corpus(src_root)
    py_mods = _python_import_modules(py_text) if has_py else set()
    js_specs = _js_specifiers(js_text) if has_js else set()

    issues: list[Issue] = []
    for path, kind in manifests:
        eco = "npm" if kind == "package_json" else "pypi"
        # 防误报红线：项目内无该生态源码 → "无源码可证"，跳过该清单
        if eco == "pypi" and not has_py:
            continue
        if eco == "npm" and not has_js:
            continue
        text = _read_text(path)
        if text is None:
            continue
        try:
            deps = _parse_manifest(text, kind)
        except Exception:
            continue
        prod_keys: set[str] | None = None
        if eco == "npm":
            prod_keys = _npm_prod_names(text)
            if prod_keys is None:
                continue  # 理论上与 _parse_manifest 同败，防御性跳过
        rel = _rel_posix(src_root, path)
        for dep in deps:
            if eco == "npm":
                if prod_keys is not None and dep.name not in prod_keys:
                    continue  # devDependencies 不报（构建工具链）
                if dep.name.startswith("@types/"):
                    continue  # 类型包豁免
                if dep.name in _NODE_BUILTINS:
                    continue  # node 内置模块同名依赖豁免
                used = _npm_dep_used(dep, js_specs)
            else:
                used = _pypi_dep_used(dep, py_mods)
            if used:
                continue
            line = next((c.line for c in dep.constraints if c.line > 0), 1)
            issues.append(
                Issue(
                    category=Category.STYLE,
                    severity=Severity.LOW,
                    title=f"依赖 {dep.raw_name} 疑似声明未使用（冗余依赖）"[:120],
                    file=rel,
                    line_start=line,
                    line_end=line,
                    code_snippet=f"{dep.raw_name}（{dep.ecosystem}）零引用",
                    description=(
                        f"清单 {rel} 第 {line} 行声明的依赖 {dep.raw_name}"
                        f"（{dep.ecosystem}）在项目源码（.py/.js/.ts）中未发现任何"
                        " import/require 引用。疑似冗余：动态导入/插件式加载/"
                        "仅文档引用会误报，请人工确认。"
                    ),
                    evidence=[
                        "rule:DEP-UNUSED",
                        f"package:{dep.name}",
                        f"ecosystem:{dep.ecosystem}",
                        f"loc:{rel}:{line}",
                    ],
                    suggestion=(
                        f"确认无动态加载后从 {rel} 移除 {dep.raw_name}；"
                        "若为部署/工具链依赖确需保留，请在项目文档注明用途。"
                    ),
                    confidence=0.7,
                    source=IssueSource.RULE,
                )
            )
    return issues


# ---------------------------------------------------------------- 工具


def _parse_manifest(text: str, kind: str) -> list[manifest.Dependency]:
    """按清单类型分发解析（异常由调用方捕获记账）。"""
    if kind == "requirements":
        return manifest.parse_requirements(text)
    if kind == "package_json":
        return manifest.parse_package_json(text)
    if kind == "pyproject":
        return manifest.parse_pyproject(text)
    return []


def _read_text(path: Path) -> str | None:
    """读文件文本（utf-8 优先，gbk 回退）；失败返回 None。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return path.read_text(encoding=encoding)
        except (OSError, UnicodeDecodeError):
            continue
    return None


def _rel_posix(src_root: Path, path: Path) -> str:
    """绝对路径 → 相对 src_root 的 posix 字符串（越界时回退文件名）。"""
    try:
        return path.relative_to(src_root).as_posix()
    except ValueError:
        return path.name
