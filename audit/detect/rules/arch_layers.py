"""分层架构违规检测（W16，验收短板清偿）：高层目录直接依赖低层目录。

单文件规则，保守白名单（可演进：项目出现其他分层命名时在下方两个 frozenset
扩充即可）：
- 高层（表现层）目录段：api / controller / handler / routes / presentation / views；
- 低层（数据访问/基础设施）目录段：dao / repository / dal / infra /
  infrastructure / persistence / db。
注意：model/models 故意不进低层表——api 层 import pydantic/sqlalchemy 的模型
定义是常态，纳入会大量误报。

判定：文件相对路径的目录段命中高层 && 文件内 import 目标命中低层 → 违规。
- python：`from X.Y import ...` / `import X.Y[, ...]`，取模块路径前两段与低层
  段做精确段名匹配（`app.dao.user` 的次段 dao 命中；相对导入 `from ..dao`
  剥离前导点后同判）；
- js/ts：`import ... from '...'` 的模块说明符按路径分段精确匹配，覆盖
  `../dao/` 相对路径与 `@/dao/` 别名两种形态（副作用导入 `import '../db/init'`
  与动态导入 `import('../db/init')` 同判）。

豁免（防误报）：文件自身在低层目录不报（数据层内部互引属正常）；根目录文件
不报（无分层结构）；api→service 等中间层不在低层表，天然不报（层之间依赖
不属于本规则关注点）。

去注释：匹配前对每行剥离行内 `#`（python）与 `//`（js/ts）——只为避免
「注释掉的 import」误报，不解析字符串字面量，容忍少量误差（注释中恰好出现
引号包裹的 import 形态属可接受误差）。
"""

from __future__ import annotations

import re
from typing import Callable, Pattern

from audit.detect.base import Rule, RuleContext, RuleHit
from audit.models import Category, Severity

__all__ = [
    "JsArchLayerViolationRule",
    "PyArchLayerViolationRule",
    "build_arch_layer_rules",
]

# 保守白名单（演进点：项目出现其他分层命名时在此扩充）
_HIGH_LAYER_SEGMENTS = frozenset(
    {"api", "controller", "handler", "routes", "presentation", "views"}
)
_LOW_LAYER_SEGMENTS = frozenset(
    {"dao", "repository", "dal", "infra", "infrastructure", "persistence", "db"}
)

_PY_FROM_RE: Pattern[str] = re.compile(r"^\s*from\s+([\w.]+)\s+import\b")
_PY_IMPORT_RE: Pattern[str] = re.compile(r"^\s*import\s+([\w.]+(?:\s*,\s*[\w.]+)*)")
# import { x } from '...' / import x from '...' / import * as ns from '...'
_JS_FROM_RE: Pattern[str] = re.compile(r"""\bimport\b[^;'"]*?\bfrom\s*['"]([^'"]+)['"]""")
# 副作用导入：import '...'
_JS_BARE_RE: Pattern[str] = re.compile(r"""\bimport\s*['"]([^'"]+)['"]""")
# 动态导入：import('...')
_JS_DYNAMIC_RE: Pattern[str] = re.compile(r"""\bimport\s*\(\s*['"]([^'"]+)['"]""")


def _dir_segments(rel_path: str) -> list[str]:
    """文件相对路径 → 目录段列表（不含文件名；兼容 \\ 与 / 分隔）。"""
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p]
    return parts[:-1]


def _py_module_hits_low(module: str) -> bool:
    """python 模块路径前两段是否命中低层段（精确段名匹配，见模块 docstring）。"""
    parts = [p for p in module.split(".") if p]
    return any(p in _LOW_LAYER_SEGMENTS for p in parts[:2])


def _js_module_hits_low(specifier: str) -> bool:
    """js/ts 模块说明符按路径分段是否命中低层段（覆盖 ../dao/ 与 @/dao/）。"""
    parts = [p for p in specifier.replace("\\", "/").split("/") if p]
    return any(p in _LOW_LAYER_SEGMENTS for p in parts)


def _py_low_layer_imports(line: str) -> list[str]:
    """一行 python 源码中命中低层段的 import 模块路径（已去行内 # 注释）。"""
    stripped = line.split("#", 1)[0]
    m = _PY_FROM_RE.match(stripped)
    if m:
        return [m.group(1)] if _py_module_hits_low(m.group(1)) else []
    m = _PY_IMPORT_RE.match(stripped)
    if m:
        return [c.strip() for c in m.group(1).split(",") if _py_module_hits_low(c.strip())]
    return []


def _js_low_layer_imports(line: str) -> list[str]:
    """一行 js/ts 源码中命中低层段的模块说明符（已去行内 // 注释）。"""
    stripped = line.split("//", 1)[0]
    specs = [m.group(1) for m in _JS_FROM_RE.finditer(stripped)]
    specs += [m.group(1) for m in _JS_BARE_RE.finditer(stripped)]
    specs += [m.group(1) for m in _JS_DYNAMIC_RE.finditer(stripped)]
    return [s for s in specs if _js_module_hits_low(s)]


def _violation_message(high_seg: str, modules: list[str]) -> str:
    shown = "、".join(modules)
    return (
        f"高层目录 `{high_seg}/` 直接 import 低层模块 `{shown}`：跳过了 service 等"
        "中间层，表现层与存储实现强耦合，替换存储或复用逻辑困难；若项目本无中间层"
        "约定可忽略本提示或调整目录命名，否则建议依赖经服务层转发，或以依赖倒置"
        "（接口/协议）解耦。"
    )


def _arch_layer_hits(
    rule: Rule,
    ctx: RuleContext,
    low_layer_imports_of: Callable[[str], list[str]],
) -> list[RuleHit]:
    """公共判定流程：目录段豁免/定性 → 逐行提取 import → 命中低层即报（行级一条）。"""
    dirs = _dir_segments(ctx.rel_path)
    if not dirs:
        return []  # 根目录文件：无分层结构，不参与
    if any(seg in _LOW_LAYER_SEGMENTS for seg in dirs):
        return []  # 文件自身在低层目录：数据层内部互引属正常，不报
    high_seg = next((seg for seg in dirs if seg in _HIGH_LAYER_SEGMENTS), None)
    if high_seg is None:
        return []  # 非高层目录（service 等中间层或无分层目录）：不在本规则范围
    hits: list[RuleHit] = []
    for idx, line in enumerate(ctx.lines, 1):
        modules = low_layer_imports_of(line)
        if modules:
            hits.append(rule.make_hit(ctx, idx, idx, _violation_message(high_seg, modules)))
    return hits


class PyArchLayerViolationRule(Rule):
    """python：高层目录（api/controller 等）直接 import 低层目录（dao/db 等）。"""

    id = "PY-LAYER-VIOLATION"
    category = Category.STYLE
    severity = Severity.MEDIUM
    languages = ("python",)
    description = (
        "高层目录（api/controller 等）直接依赖低层目录（dao/repository/db 等），"
        "跳过 service 中间层：表现层与存储实现强耦合，替换存储或复用接口困难。"
        "若项目无中间层约定可忽略或调整目录命名；建议依赖经服务层转发，或以"
        "依赖倒置（接口/协议）解耦。"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        return _arch_layer_hits(self, ctx, _py_low_layer_imports)


class JsArchLayerViolationRule(Rule):
    """js/ts：高层目录直接 import 低层目录（相对路径 ../dao/ 或别名 @/dao/ 等）。"""

    id = "JS-LAYER-VIOLATION"
    category = Category.STYLE
    severity = Severity.MEDIUM
    languages = ("javascript", "typescript")
    description = PyArchLayerViolationRule.description

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        return _arch_layer_hits(self, ctx, _js_low_layer_imports)


def build_arch_layer_rules() -> list[Rule]:
    """W16：分层架构违规规则集（python 与 js/ts 各一条），供集成层统一注册。"""
    return [PyArchLayerViolationRule(), JsArchLayerViolationRule()]
