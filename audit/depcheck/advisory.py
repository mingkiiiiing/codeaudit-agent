"""种子漏洞库加载与"约束区间 ∩ 漏洞区间"交集匹配（DEP-CVE 检出核心）。

数据来源：audit/depcheck/db/advisory_db.json 种子库，条目字段经 GitHub
Security Advisory Database（REST /advisories?cve_id=...）逐条核实
（CVSS 分数、受影响区间、first_patched_version），离线随包分发。

版本语义局限（第一版，诚实记录）：
- 只处理纯数字点分段（release 段）比较，epoch / pre-release / post / local
  修饰符一律剥除（"3.0.0rc1" 按 (3,0,0) 参与比较），与完整 PEP440/semver
  规范不等价；
- ~= / ^ / ~ 按其最常见的语义折算为左闭右开区间；
- 不支持 PEP440 任意子句（===、~==、@ url）与 npm 的连字符范围；
- "0.0.0" 作为 introduced 表示无下界（GHSA 常只给 "<X.Y.Z" 单边上界）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from audit.models import Severity

from .manifest import Constraint, Dependency, canonical_pypi_name

# 种子库路径：随包分发（db/advisory_db.json）
DB_PATH = Path(__file__).resolve().parent / "db" / "advisory_db.json"

# 无下界标记（GHSA 只给 "<X" 上界时用）
_NO_FLOOR = "0.0.0"

_VALID_FIELDS = ("ecosystem", "package", "cve", "introduced", "fixed", "cvss", "summary", "reference")


# ---------------------------------------------------------------- 版本解析与比较

_VERSION_TOKEN_RE = re.compile(r"^\d+")


def parse_version(version: str) -> tuple[int, ...]:
    """把版本字符串解析为纯数字点分段元组。

    只取各段的前导数字（"1.2.3rc1" → (1,2,3)；"2015.4.28" → (2015,4,28)），
    遇到非数字开头的段停止；无任何数字段时返回 (0,)。epoch（"1!2.0"）与
    修饰符不做完整语义（docstring 局限已注明）。
    """
    text = (version or "").strip().lstrip("vV=")
    parts: list[int] = []
    for seg in text.split("."):
        m = _VERSION_TOKEN_RE.match(seg)
        if m is None:
            break
        parts.append(int(m.group(0)))
    return tuple(parts) if parts else (0,)


def vcmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    """数字点分段比较：尾部补零对齐（2.0 == 2.0.0，符合 release 语义）。"""
    n = max(len(a), len(b))
    pa = a + (0,) * (n - len(a))
    pb = b + (0,) * (n - len(b))
    if pa < pb:
        return -1
    if pa > pb:
        return 1
    return 0


# ---------------------------------------------------------------- 区间模型


@dataclass
class Interval:
    """左闭右开为主的版本区间 [lo, hi)；None 表示无界。"""

    lo: tuple[int, ...] | None
    lo_incl: bool
    hi: tuple[int, ...] | None
    hi_incl: bool


def _compatible_upper(v: tuple[int, ...]) -> tuple[int, ...]:
    """PEP440 ~=X.Y.Z 的折算上界：<X.(Y+1).0；两段 ~=X.Y → <(X+1).0。"""
    if len(v) >= 3:
        return (v[0], v[1] + 1, 0)
    if len(v) == 2:
        return (v[0] + 1, 0)
    return (v[0] + 1, 0)


def _caret_upper(v: tuple[int, ...]) -> tuple[int, ...]:
    """npm ^X.Y.Z 的折算上界：最左非零段 +1（^0.2.3 → <0.3.0，^0.0.3 → <0.0.4）。"""
    base = v + (0,) * max(0, 3 - len(v))
    if base[0] > 0:
        return (base[0] + 1, 0, 0)
    if base[1] > 0:
        return (0, base[1] + 1, 0)
    return (0, 0, base[2] + 1)


def _tilde_upper(v: tuple[int, ...]) -> tuple[int, ...]:
    """npm ~X.Y.Z 的折算上界：同级补丁可变 <X.(Y+1).0；单段 ~X → <(X+1).0。"""
    if len(v) >= 2:
        return (v[0], v[1] + 1, 0)
    return (v[0] + 1, 0)


def constraints_to_interval(constraints: list[Constraint]) -> Interval | None:
    """把约束列表折算为单一区间（区间交集）；互相矛盾的 == 返回 None（空区间）。

    ==/同值约束收紧两端；>=/> 收紧下界；<=/</~=/^/~ 收紧上界；!= 不参与
    （manifest.parse_constraint_str 已滤除，docstring 局限已注明）。
    """
    lo: tuple[int, ...] | None = None
    lo_incl = True
    hi: tuple[int, ...] | None = None
    hi_incl = True
    exacts: list[tuple[int, ...]] = []

    def _tighten_lo(v: tuple[int, ...], incl: bool) -> None:
        nonlocal lo, lo_incl
        if lo is None or vcmp(v, lo) > 0:
            lo, lo_incl = v, incl
        elif vcmp(v, lo) == 0:
            lo_incl = lo_incl and incl  # 同值取更严格的开区间端点

    def _tighten_hi(v: tuple[int, ...], incl: bool) -> None:
        nonlocal hi, hi_incl
        if hi is None or vcmp(v, hi) < 0:
            hi, hi_incl = v, incl
        elif vcmp(v, hi) == 0:
            hi_incl = hi_incl and incl

    for c in constraints:
        v = parse_version(c.version)
        op = c.op
        if op in ("==", "="):
            if c.version.endswith(".*"):  # ==1.2.* → [1.2, 1.3)；==1.* → [1, 2)
                base = parse_version(c.version[:-2])
                _tighten_lo(base, True)
                _tighten_hi(
                    base[:-1] + (base[-1] + 1,) if len(base) >= 2 else (base[0] + 1,), False
                )
            else:
                exacts.append(v)
        elif op == ">=":
            _tighten_lo(v, True)
        elif op == ">":
            _tighten_lo(v, False)
        elif op == "<=":
            _tighten_hi(v, True)
        elif op == "<":
            _tighten_hi(v, False)
        elif op == "~=":
            _tighten_lo(v, True)
            _tighten_hi(_compatible_upper(v), False)
        elif op == "^":
            _tighten_lo(v, True)
            _tighten_hi(_caret_upper(v), False)
        elif op == "~":
            _tighten_lo(v, True)
            _tighten_hi(_tilde_upper(v), False)

    if exacts:
        if any(vcmp(e, exacts[0]) != 0 for e in exacts):
            return None  # ==1.0,==2.0 互相矛盾：不可能满足
        _tighten_lo(exacts[0], True)
        _tighten_hi(exacts[0], True)
    return Interval(lo=lo, lo_incl=lo_incl, hi=hi, hi_incl=hi_incl)


def advisory_interval(advisory: "Advisory") -> Interval:
    """漏洞影响区间 [introduced, fixed)；introduced=="0.0.0" 视为无下界。"""
    lo = None
    if advisory.introduced and advisory.introduced != _NO_FLOOR:
        lo = parse_version(advisory.introduced)
    hi = parse_version(advisory.fixed) if advisory.fixed else None
    return Interval(lo=lo, lo_incl=True, hi=hi, hi_incl=False)


def intervals_overlap(a: Interval, b: Interval) -> bool:
    """两区间是否相交（含端点细节：单点相交需两端均含）。"""
    # 取更严格的下界（max）与上界（min）；None = 无界
    if a.lo is None:
        lo, lo_incl = b.lo, b.lo_incl
    elif b.lo is None:
        lo, lo_incl = a.lo, a.lo_incl
    else:
        c = vcmp(a.lo, b.lo)
        if c > 0:
            lo, lo_incl = a.lo, a.lo_incl
        elif c < 0:
            lo, lo_incl = b.lo, b.lo_incl
        else:
            lo, lo_incl = a.lo, a.lo_incl and b.lo_incl

    if a.hi is None:
        hi, hi_incl = b.hi, b.hi_incl
    elif b.hi is None:
        hi, hi_incl = a.hi, a.hi_incl
    else:
        c = vcmp(a.hi, b.hi)
        if c < 0:
            hi, hi_incl = a.hi, a.hi_incl
        elif c > 0:
            hi, hi_incl = b.hi, b.hi_incl
        else:
            hi, hi_incl = a.hi, a.hi_incl and b.hi_incl

    if lo is None or hi is None:
        return True  # 至少一侧无界且另一侧区间非空
    c = vcmp(lo, hi)
    if c < 0:
        return True
    if c > 0:
        return False
    return lo_incl and hi_incl  # 单点相交需两端均含


# ---------------------------------------------------------------- 种子库


@dataclass
class Advisory:
    """种子库条目（字段与 db/advisory_db.json 一一对应）。"""

    ecosystem: str  # "pypi" | "npm"
    package: str  # 规范化包名
    cve: str  # CVE-XXXX-XXXXX
    introduced: str  # 区间下界（含）；"0.0.0" = 无下界
    fixed: str  # 修复版本（区间上界，不含）；空 = 无上界
    cvss: float
    summary: str
    reference: str


def load_advisories(db_path: str | Path | None = None) -> list[Advisory]:
    """加载种子漏洞库；损坏/缺字段的条目跳过（加载本身尽量宽容）。"""
    path = Path(db_path) if db_path else DB_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    advisories: list[Advisory] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            advisories.append(
                Advisory(
                    ecosystem=str(item["ecosystem"]).strip().lower(),
                    package=canonical_pypi_name(str(item["package"])),
                    cve=str(item["cve"]).strip(),
                    introduced=str(item.get("introduced") or _NO_FLOOR).strip(),
                    fixed=str(item.get("fixed") or "").strip(),
                    cvss=float(item.get("cvss") or 0.0),
                    summary=str(item.get("summary") or "").strip(),
                    reference=str(item.get("reference") or "").strip(),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue  # 缺关键字段/类型异常的条目不入库
    return advisories


def severity_from_cvss(score: float) -> Severity:
    """CVSS 基础分 → Severity（契约：≥9.0 critical、≥7.0 high、≥4.0 medium、否则 low）。"""
    if score >= 9.0:
        return Severity.CRITICAL
    if score >= 7.0:
        return Severity.HIGH
    if score >= 4.0:
        return Severity.MEDIUM
    return Severity.LOW


def match_advisories(
    deps: list[Dependency], advisories: list[Advisory] | None = None
) -> list[tuple[Dependency, Advisory]]:
    """依赖约束区间 ∩ 漏洞区间 ≠ ∅ 的全部 (Dependency, Advisory) 对。

    无有效约束（空表/空区间）的依赖不参与匹配（如 npm "*" / git 协议版本）。
    """
    advisories = advisories if advisories is not None else load_advisories()
    by_key: dict[tuple[str, str], list[Advisory]] = {}
    for adv in advisories:
        by_key.setdefault((adv.ecosystem, canonical_pypi_name(adv.package)), []).append(adv)

    matches: list[tuple[Dependency, Advisory]] = []
    for dep in deps:
        if not dep.constraints:
            continue
        dep_iv = constraints_to_interval(dep.constraints)
        if dep_iv is None:
            continue
        for adv in by_key.get((dep.ecosystem, dep.name), []):
            if intervals_overlap(dep_iv, advisory_interval(adv)):
                matches.append((dep, adv))
    return matches
