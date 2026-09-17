"""依赖清单解析：requirements*.txt / package.json / pyproject.toml → Dependency 列表。

三类清单统一产出 Dependency（规范化包名 + 约束列表 + 约束所在行号），供
advisory 做漏洞区间交集匹配、scanner 做重复钉扎检测。

已知限制（第一版，诚实记录）：
- 冗余依赖（声明但未使用）检测（DEP-UNUSED）由 scanner 基于本模块解析
  结果实现，本模块只负责解析，不判定源码引用关系；
- pyproject.toml 只解析 [project].dependencies 字符串数组，不支持 poetry/
  setuptools 风格的 [tool.*] 依赖表与 optional-dependencies；
- package.json / pyproject.toml 的行号通过"原始文本回查依赖名"得到，回查
  失败（模板生成/超长单行等）时回退为第 1 行；
- npm 的 git/url/workspace 协议版本（"github:a/b"、"*"、"latest"）无有效
  版本约束，保留依赖记录但不参与漏洞区间匹配；"||" 多区间与连字符范围
  （"1.2.3 - 2.3.4"）不支持，只取首个分支；
- package-lock.json（W19 追加）：解析锁定条目为精确 == 约束，供漏洞区间
  匹配覆盖传递依赖；行号回查失败的条目回退第 1 行（由调用方在 evidence
  标注）。
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 数据结构


@dataclass
class Constraint:
    """单条版本约束：op ∈ {==, =, >=, >, <=, <, ~=, ^, ~}（^/~ 为 npm 语义）。"""

    op: str
    version: str
    line: int = 0


@dataclass
class Dependency:
    """一个清单依赖项：name 为规范化名（pypi 按 PEP503、npm 小写）。"""

    name: str
    raw_name: str
    ecosystem: str  # "pypi" | "npm"
    constraints: list[Constraint] = field(default_factory=list)


# ---------------------------------------------------------------- 归一化工具


def canonical_pypi_name(name: str) -> str:
    """PEP503 包名归一：连续 -_. 折叠为单连字符并小写（Pillow → pillow）。"""
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def canonical_npm_name(name: str) -> str:
    """npm 包名归一：小写（含 scope 的 @a/b 保持原结构）。"""
    return name.strip().lower()


# ---------------------------------------------------------------- requirements*.txt

# 名字（PEP508 简化）：字母数字开头，含 -_. 字符
_REQ_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$")
# 约束算子 + 版本（版本段取到字母/数字/.*+-，覆盖 1.2.3rc1 / 2015.4.28 / 1.2.*）
_REQ_CON_RE = re.compile(r"(==|!=|>=|<=|~=|>|<)\s*([0-9A-Za-z.*!+-]+)")


def parse_constraint_str(text: str, line: int = 0) -> list[Constraint]:
    """解析逗号复合约束串（如 ">=1.0, <2.0"）为 Constraint 列表。

    != 约束对"区间交集"模型无法表达，忽略之（docstring 局限已注明）。
    """
    found: list[Constraint] = []
    for m in _REQ_CON_RE.finditer(text):
        op, version = m.group(1), m.group(2)
        if op == "!=":
            continue
        found.append(Constraint(op=op, version=version, line=line))
    return found


def parse_requirements(text: str) -> list[Dependency]:
    """解析 requirements*.txt 内容为 Dependency 列表。

    - 忽略空行、整行注释、`-r/--requirement/-e` 等选项行；
    - 行内注释（" #" 之后）与环境标记（";" 之后）剥除；
    - extras（name[extra1,extra2]）剥除，只保留包名。
    """
    deps: list[Dependency] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):  # -r other.txt / -e . / --index-url 等选项行
            continue
        if " #" in line:  # 行内注释（简化口径：要求 # 前有空白）
            line = line.split(" #", 1)[0].strip()
        if ";" in line:  # 环境标记（; python_version >= "3"）
            line = line.split(";", 1)[0].strip()
        m = _REQ_NAME_RE.match(line)
        if m is None:
            continue
        raw_name, _extras, constraint_text = m.group(1), m.group(2) or "", m.group(3)
        constraints = parse_constraint_str(constraint_text, line=lineno)
        deps.append(
            Dependency(
                name=canonical_pypi_name(raw_name),
                raw_name=raw_name,
                ecosystem="pypi",
                constraints=constraints,
            )
        )
    return deps


# ---------------------------------------------------------------- package.json

# npm spec 的版本约束片段：^ ~ >= <= > < = 前缀 + 数字版本
_NPM_CON_RE = re.compile(r"(\^|~|>=|<=|>|<|=)?\s*v?(\d+(?:\.\d+){0,3})")
# 无有效版本约束的 spec：git/url/file/workspace 协议
_NPM_NON_VERSION_PREFIXES = ("github:", "git+", "git:", "http://", "https://", "file:", "link:", "workspace:", "npm:")


def parse_npm_spec(spec: str, line: int = 0) -> list[Constraint]:
    """把 npm 版本说明（"^1.2.3" / "~2.0" / ">=1.0" / "1.2.3"）解析为约束列表。

    - "*" / "latest" / 空串 / git-url 协议 → 无有效约束（返回空表）；
    - "||" 多区间只取第一分支；连字符范围（"1.2 - 2.3"）不支持（取第一个数字）。
    """
    text = (spec or "").strip()
    if not text or text in ("*", "latest", "x", "X", ""):
        return []
    if text.lower().startswith(_NPM_NON_VERSION_PREFIXES):
        return []
    if "||" in text:  # 多区间：简化只取第一分支
        text = text.split("||", 1)[0].strip()
    found: list[Constraint] = []
    for m in _NPM_CON_RE.finditer(text):
        op = m.group(1) or "="  # 裸版本视为精确
        found.append(Constraint(op=op, version=m.group(2), line=line))
    return found


def parse_package_json(text: str) -> list[Dependency]:
    """解析 package.json 的 dependencies + devDependencies 为 Dependency 列表。

    解析失败抛 json.JSONDecodeError（由调用方记账容错）；行号回查原文。
    """
    data = json.loads(text)
    if not isinstance(data, dict):
        return []
    deps: list[Dependency] = []
    seen: set[str] = set()
    for section in ("dependencies", "devDependencies"):
        table = data.get(section)
        if not isinstance(table, dict):
            continue
        for raw_name, spec in table.items():
            if not isinstance(raw_name, str) or not isinstance(spec, str):
                continue
            name = canonical_npm_name(raw_name)
            line = _find_kv_line(text, raw_name)
            if name in seen:  # 同名只保留先出现的（dependencies 优先级高）
                continue
            seen.add(name)
            deps.append(
                Dependency(
                    name=name,
                    raw_name=raw_name,
                    ecosystem="npm",
                    constraints=parse_npm_spec(spec, line=line),
                )
            )
    return deps


# ---------------------------------------------------------------- pyproject.toml


def parse_pyproject(text: str) -> list[Dependency]:
    """解析 pyproject.toml 的 [project].dependencies 为 Dependency 列表。

    只支持 PEP621 的 project.dependencies 字符串数组；poetry/setuptools 的
    [tool.*] 依赖表不做（docstring 局限已注明）。解析失败抛 tomllib.TOMLDecodeError。
    """
    data = tomllib.loads(text)
    items = (data.get("project") or {}).get("dependencies") or []
    deps: list[Dependency] = []
    if not isinstance(items, list):
        return deps
    for item in items:
        if not isinstance(item, str):
            continue
        m = _REQ_NAME_RE.match(item.strip())
        if m is None:
            continue
        raw_name, _extras, constraint_text = m.group(1), m.group(2) or "", m.group(3)
        # 行号回查：先找整条依赖串，找不到退回包名
        line = _find_literal_line(text, item.strip()) or _find_literal_line(text, raw_name) or 1
        deps.append(
            Dependency(
                name=canonical_pypi_name(raw_name),
                raw_name=raw_name,
                ecosystem="pypi",
                constraints=parse_constraint_str(constraint_text, line=line),
            )
        )
    return deps


# ---------------------------------------------------------------- package-lock.json

# lock 文件条目解析上限（性能护栏：packages/dependencies 条目超过只解析前
# MAX_LOCK_ENTRIES 条，超出部分不产出依赖，诚实降级）
MAX_LOCK_ENTRIES = 5000

# 锁定条目合法精确版本：数字点分段（可带 v 前缀与 -beta.1/+build 修饰）。
# 排除 "file:../x"、"github:a/b#hash" 等非版本说明被误当作精确版本，
# 否则会被 advisory.parse_version 折算为 (0,) 造成假命中
_LOCK_VERSION_RE = re.compile(r"^v?\d+(\.\d+)*(?:[-+].*)?$")


def parse_package_lock(path: str | Path) -> list[Dependency]:
    """解析 package-lock.json 为 Dependency 列表（npm 传递依赖，W19 清偿）。

    - lockfileVersion 2/3：解析 "packages" 表（键形如 "node_modules/<pkg>"，
      嵌套安装 "node_modules/a/node_modules/b" 取最后一段 node_modules 之后
      的名字）；键 "" 为根条目跳过，node_modules 之外的键（workspace 相对
      路径等）跳过；
    - lockfileVersion 1：解析顶层 "dependencies" 表并向下递归一层（更深嵌套
      不做）；"packages" 与 "dependencies" 并存（lockfileVersion 2）时只解析
      "packages"，避免同一依赖重复产出；
    - "dev": true 的条目跳过（devDependencies 语义，与 DEP-UNUSED 口径一致）；
    - 每个条目产出一条 op="==" 精确约束，行号回查原文（找不到回退 1）；
    - 条目数上限 MAX_LOCK_ENTRIES（前 5000 条，含被跳过的条目）；
    - 文件不可读抛 OSError、JSON 损坏抛 json.JSONDecodeError（由调用方容错）。
    """
    p = Path(path)
    text: str | None = None
    last_exc: Exception | None = None
    for encoding in ("utf-8", "gbk"):
        try:
            text = p.read_text(encoding=encoding)
            break
        except (OSError, UnicodeDecodeError) as exc:
            last_exc = exc
    if text is None:
        raise OSError(f"package-lock.json 不可读：{p}") from last_exc

    data = json.loads(text)  # 损坏 JSON 抛 JSONDecodeError（调用方容错）
    if not isinstance(data, dict):
        return []
    deps: list[Dependency]

    packages = data.get("packages")
    if isinstance(packages, dict) and packages:
        deps = []
        budget = MAX_LOCK_ENTRIES
        for key, entry in packages.items():
            if budget <= 0:
                break
            budget -= 1
            if not isinstance(key, str) or not key.startswith("node_modules/"):
                continue  # 根条目 "" 与 node_modules 外的键跳过
            if not isinstance(entry, dict) or entry.get("dev") is True:
                continue  # devDependencies 语义跳过
            version = entry.get("version")
            if not isinstance(version, str) or not _LOCK_VERSION_RE.match(version.strip()):
                continue  # 非精确数字版本（file:/link: 等）不产出约束
            name = canonical_npm_name(key.rsplit("node_modules/", 1)[-1])
            deps.append(
                Dependency(
                    name=name,
                    raw_name=name,
                    ecosystem="npm",
                    constraints=[Constraint(op="==", version=version.strip(), line=_find_kv_line(text, key))],
                )
            )
        return deps

    # lockfileVersion 1 形态：顶层 dependencies 表，向下递归一层
    deps = []
    top = data.get("dependencies")
    if isinstance(top, dict):
        _collect_lock_v1(text, top, deps, budget=MAX_LOCK_ENTRIES, depth=0, search_from=1)
    return deps


def _collect_lock_v1(
    text: str,
    table: dict,
    out: list["Dependency"],
    budget: int,
    depth: int,
    search_from: int,
) -> int:
    """lockfileVersion 1 的 dependencies 表收集（返回剩余条目预算）。

    行号回查从 search_from 行起（顶层 1 起；嵌套从父条目键行的下一行起，
    避免同名包命中父条目自身的键行）；depth < 1 时递归子 dependencies 一层。
    """
    for raw_name, entry in table.items():
        if budget <= 0:
            break
        budget -= 1
        if not isinstance(raw_name, str) or not isinstance(entry, dict):
            continue
        if entry.get("dev") is True:
            continue  # devDependencies 语义跳过
        entry_line = _find_kv_line_from(text, f'"{raw_name}"', search_from)
        version = entry.get("version")
        if isinstance(version, str) and _LOCK_VERSION_RE.match(version.strip()):
            name = canonical_npm_name(raw_name)
            out.append(
                Dependency(
                    name=name,
                    raw_name=name,
                    ecosystem="npm",
                    constraints=[Constraint(op="==", version=version.strip(), line=entry_line)],
                )
            )
        nested = entry.get("dependencies")
        if depth < 1 and isinstance(nested, dict):  # 只递归一层
            budget = _collect_lock_v1(text, nested, out, budget, depth + 1, entry_line + 1)
    return budget


# ---------------------------------------------------------------- 文件分类与行号回查


def classify_manifest(filename: str) -> str | None:
    """按文件名判定清单类型："requirements" | "package_json" | "pyproject" | None。"""
    name = filename.lower()
    if name == "package.json":
        return "package_json"
    if name == "pyproject.toml":
        return "pyproject"
    if re.match(r"requirements.*\.txt$", name):
        return "requirements"
    return None


def _find_kv_line(text: str, key: str) -> int:
    """在 JSON 文本中回查 "key": 键值对首次出现的行号（1 起，找不到回退 1）。"""
    needle = f'"{key}"'
    for lineno, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return lineno
    return 1


def _find_kv_line_from(text: str, needle: str, start_line: int) -> int:
    """从 start_line（1 起）开始回查首个含 needle 的行号；找不到回退 1。

    用于 lockfileVersion 1 嵌套条目的行号消歧（从父条目键行的下一行起查，
    避免同名包命中父条目自身的键行）。
    """
    lines = text.splitlines()
    for idx in range(max(0, start_line - 1), len(lines)):
        if needle in lines[idx]:
            return idx + 1
    return 1


def _find_literal_line(text: str, needle: str) -> int | None:
    """在文本中回查字面串首次出现的行号（1 起；找不到返回 None）。"""
    if not needle:
        return None
    for lineno, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return lineno
    return None
