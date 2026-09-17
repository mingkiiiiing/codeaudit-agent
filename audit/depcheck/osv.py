"""OSV 在线漏洞查询（opt-in 增强，默认完全不触网）。

开关：环境变量 CODEAUDIT_OSV_ONLINE=1 时启用；未设置或为其他值时
osv_enabled() 返回 False，scanner 直接跳过在线步骤，本模块任何函数都
不会发起网络请求——种子库（advisory.py + db/advisory_db.json）离线路径
零变化。

API：POST https://api.osv.dev/v1/query，body 形如
{"package": {"name": <包名>, "ecosystem": "PyPI"|"npm"}, "version": <版本>}，
响应为 {"vulns": [...]}。每条 vuln 取：
- id（OSV-xxx / GHSA-xxx）与 aliases 中的 CVE-xxx（rule id 优先用 CVE）；
- severity 数组中 type=CVSS_V3 的 score（经 advisory.severity_from_cvss
  映射 Severity；缺失时按 0.0 → low）；
- affected[].ranges[].events（introduced/fixed）→ advisory.Interval，
  与清单约束区间做本地交集（复用种子库同款区间逻辑，不单纯信任远端）。

工程约束（第一版，诚实记录）：
- 只用 urllib.request（零新增依赖），timeout 默认 10s；
- 网络异常 / 非 200 / 解析失败一律静默降级为空列表，并把 repr 记入调用方
  传入的 errors 字典（跟随 scanner 的 post_scan_errors 记账模式）；
- 同一 (ecosystem, 包名, 精确钉扎版本) 全扫描只查询一次；单次审计查询
  上限 MAX_OSV_QUERIES=200，超出停止并把超限事实记入 errors；
- 查询版本口径：依赖被精确钉扎（==X.Y.Z）时带 version 查询；约束为区间
  （如 >=A,<B）时不带 version 字段（OSV 返回该包全部漏洞，由本地
  "vuln 区间 ∩ 清单约束区间" 过滤，语义与种子库路径一致）；无任何有效
  约束（npm "*" / git 协议）的依赖直接跳过，不做在线查询。

已知限制：OSV 区间语义（ECOSYSTEM/SEMVER/GIT）在此按数字点分段近似
处理，与 advisory.parse_version 的局限一致；last_affected 视为闭上界。
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Callable

from audit.models import Category, Issue, IssueSource

from . import advisory
from .manifest import Dependency

__all__ = [
    "osv_enabled",
    "query_osv",
    "osv_issues",
    "build_osv_issue",
    "vuln_intervals",
    "vuln_cve",
    "vuln_rule_id",
    "vuln_cvss",
    "MAX_OSV_QUERIES",
    "DEFAULT_TIMEOUT",
    "OSV_QUERY_URL",
]

OSV_QUERY_URL = "https://api.osv.dev/v1/query"
OSV_ONLINE_ENV = "CODEAUDIT_OSV_ONLINE"  # 值为 "1" 时启用在线查询
MAX_OSV_QUERIES = 200  # 单次审计在线查询上限（超出停止并记账）
DEFAULT_TIMEOUT = 10.0  # 单次请求超时（秒）

_ERROR_KEY = "depcheck.osv"  # post_scan_errors 中的记账键
_BUDGET_KEY = "depcheck.osv_budget"  # 查询超限记账键

# 内部生态名 → OSV ecosystem 字段
_ECOSYSTEM_MAP = {"pypi": "PyPI", "npm": "npm"}


def osv_enabled() -> bool:
    """OSV 在线查询是否启用（仅 CODEAUDIT_OSV_ONLINE=1 时为 True，默认关）。"""
    return os.environ.get(OSV_ONLINE_ENV, "").strip() == "1"


# ---------------------------------------------------------------- 查询


def query_osv(
    package: str,
    version: str,
    ecosystem: str,
    timeout: float = DEFAULT_TIMEOUT,
    errors: dict[str, str] | None = None,
) -> list[dict]:
    """调用 OSV /v1/query 返回 vuln 列表；任何失败静默降级为空列表。

    - version 为空串时 body 不带 version 字段（返回该包全部漏洞，由调用方
      本地区间过滤）；
    - 网络异常 / 非 200 / JSON 解析失败：把 repr 记入 errors[_ERROR_KEY]
      （跟随 scanner 错误记账模式）并返回 []。
    """
    eco = _ECOSYSTEM_MAP.get(ecosystem.strip().lower(), ecosystem)
    body: dict[str, Any] = {"package": {"name": package, "ecosystem": eco}}
    if version:
        body["version"] = version
    req = urllib.request.Request(  # noqa: S310  固定官方端点，无用户可控 URL
        OSV_QUERY_URL,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            if status != 200:
                raise OSError(f"OSV API 非 200 响应: {status}")
            payload = json.loads(resp.read())
    except Exception as exc:  # 网络/协议/解析故障统一降级，不阻断扫描
        if errors is not None:
            errors[_ERROR_KEY] = repr(exc)
        return []
    vulns = payload.get("vulns") if isinstance(payload, dict) else None
    if not isinstance(vulns, list):
        return []
    return [v for v in vulns if isinstance(v, dict)]


# ---------------------------------------------------------------- vuln 字段解析


def vuln_cve(vuln: dict) -> str:
    """取 aliases 中首个 CVE-xxx（无则空串）。"""
    for alias in vuln.get("aliases") or []:
        if isinstance(alias, str) and alias.upper().startswith("CVE-"):
            return alias
    return ""


def vuln_rule_id(vuln: dict) -> str:
    """OSV 命中的 rule id：有 CVE 别名用 DEP-CVE-<编号>，否则 DEP-OSV-<短id>。"""
    cve = vuln_cve(vuln)
    if cve:
        return f"DEP-CVE-{cve}"
    vid = str(vuln.get("id") or "").strip()
    return f"DEP-OSV-{vid}" if vid else ""


def vuln_cvss(vuln: dict) -> float:
    """severity 数组中 type=CVSS_V3 的 score；缺失/非法返回 0.0（→ low）。"""
    for sev in vuln.get("severity") or []:
        if isinstance(sev, dict) and str(sev.get("type") or "").upper() == "CVSS_V3":
            try:
                return float(sev.get("score"))
            except (TypeError, ValueError):
                continue
    return 0.0


def vuln_summary(vuln: dict) -> str:
    """漏洞摘要文本：优先 summary，回退 details，截断 200 字符。"""
    text = str(vuln.get("summary") or vuln.get("details") or "").strip()
    return text[:200]


def vuln_intervals(vuln: dict) -> list[advisory.Interval]:
    """affected[].ranges[].events → [introduced, fixed) 区间列表。

    - introduced="0"/空 视为无下界；introduced 后无 fixed → 无上界区间；
    - last_affected 视为闭上界（hi_incl=True）；
    - 一条 range 内多段 introduced/fixed 事件拆为多个区间。
    """
    out: list[advisory.Interval] = []
    for affected in vuln.get("affected") or []:
        if not isinstance(affected, dict):
            continue
        for rng in affected.get("ranges") or []:
            if not isinstance(rng, dict):
                continue
            lo: tuple[int, ...] | None = None
            open_seen = False  # introduced 已出现（区间开启），lo 可为 None（无下界）
            for ev in rng.get("events") or []:
                if not isinstance(ev, dict):
                    continue
                if "introduced" in ev:
                    v = str(ev["introduced"]).strip()
                    lo = None if v in ("", "0") else advisory.parse_version(v)
                    open_seen = True
                elif "fixed" in ev and open_seen:
                    fixed = str(ev["fixed"]).strip()
                    out.append(
                        advisory.Interval(
                            lo=lo,
                            lo_incl=True,
                            hi=advisory.parse_version(fixed) if fixed else None,
                            hi_incl=False,
                        )
                    )
                    lo, open_seen = None, False
                elif "last_affected" in ev and open_seen:
                    v = str(ev["last_affected"]).strip()
                    out.append(
                        advisory.Interval(
                            lo=lo, lo_incl=True, hi=advisory.parse_version(v), hi_incl=True
                        )
                    )
                    lo, open_seen = None, False
            if open_seen:  # introduced 后无结束事件：影响至最新版本
                out.append(advisory.Interval(lo=lo, lo_incl=True, hi=None, hi_incl=False))
    return out


def vuln_first_fixed(vuln: dict) -> str:
    """首个 fixed 事件版本串（无则空串），用于修复建议文案。"""
    for affected in vuln.get("affected") or []:
        if not isinstance(affected, dict):
            continue
        for rng in affected.get("ranges") or []:
            if not isinstance(rng, dict):
                continue
            for ev in rng.get("events") or []:
                if isinstance(ev, dict) and "fixed" in ev:
                    return str(ev["fixed"]).strip()
    return ""


def vuln_hits_interval(vuln: dict, dep_interval: advisory.Interval) -> bool:
    """任一 vuln 区间与清单约束区间相交即视为命中（复用种子库交集逻辑）。"""
    return any(advisory.intervals_overlap(iv, dep_interval) for iv in vuln_intervals(vuln))


# ---------------------------------------------------------------- Issue 组装


def build_osv_issue(rel: str, dep: Dependency, vuln: dict, rule_id: str | None = None) -> Issue:
    """把一条 OSV vuln 组装为 Issue（契约与 scanner._cve_issues 对齐）。

    evidence 首段带 source:osv 标注；description 注明数据来源为 OSV 在线。
    """
    rid = rule_id or vuln_rule_id(vuln)
    cve = vuln_cve(vuln)
    cvss = vuln_cvss(vuln)
    line = next((c.line for c in dep.constraints if c.line > 0), 1)
    fixed = vuln_first_fixed(vuln)
    summary = vuln_summary(vuln)
    evidence = [
        f"rule:{rid}",
        "source:osv",
        f"osv-id:{vuln.get('id') or '未知'}",
    ]
    if cve:
        evidence.append(f"cve:{cve}")
    evidence.extend(
        [
            f"package:{dep.name}",
            f"cvss:{cvss:g}",
            f"fixed:{fixed or '未知'}",
            f"loc:{rel}:{line}",
        ]
    )
    return Issue(
        category=Category.SECURITY,
        severity=advisory.severity_from_cvss(cvss),
        title=f"依赖 {dep.raw_name} 命中已知漏洞 {cve or rid}"[:120],
        file=rel,
        line_start=line,
        line_end=line,
        code_snippet=f"{dep.raw_name}: {cve or rid}（CVSS {cvss:g}，OSV 在线）",
        description=(
            f"（数据来源：OSV 在线 api.osv.dev）清单 {rel} 第 {line} 行声明的依赖"
            f" {dep.raw_name}（{dep.ecosystem}）经在线漏洞库比对命中 {rid}：{summary}"
        ),
        evidence=evidence,
        suggestion=(
            f"升级到 >= {fixed}" if fixed else "关注上游修复版本"
            f"（OSV 条目 {vuln.get('id') or rid}），并验证兼容性。"
        ),
        confidence=0.7,
        source=IssueSource.RULE,
    )


def _exact_pin(dep: Dependency) -> str:
    """首个非通配精确钉扎版本（==X）；无则空串。"""
    for c in dep.constraints:
        if c.op in ("==", "=") and not c.version.endswith(".*"):
            return c.version
    return ""


def osv_issues(
    file_deps: list[tuple[str, list[Dependency]]],
    existing: list[Issue] | None = None,
    errors: dict[str, str] | None = None,
    fetch: Callable[..., list[dict]] = query_osv,
    max_queries: int = MAX_OSV_QUERIES,
) -> list[Issue]:
    """对全部清单依赖做 OSV 在线比对并组装 Issue（去重 + 预算 + 降级）。

    - file_deps: [(清单相对路径, [Dependency])...]（scanner 解析后传入）；
    - existing: 种子库阶段已产出的 Issue——同（文件, CVE）已命中时去重跳过，
      并给既有 Issue 的 evidence 追加 "source:osv" 在线确认标注；
    - fetch: 网络函数，测试可注入 mock（签名对齐 query_osv）；
    - 预算：跨清单去重后查询次数达到 max_queries 即停止，超限事实记入
      errors[_BUDGET_KEY]。
    """
    existing = list(existing or [])
    # 种子库已报 (文件, CVE) 集合，用于同口径去重
    seed_keys: set[tuple[str, str]] = set()
    for iss in existing:
        for e in iss.evidence:
            if e.startswith("rule:DEP-CVE-"):
                seed_keys.add((iss.file, e[len("rule:DEP-CVE-") :]))

    seen_query: set[tuple[str, str, str]] = set()
    seen_rule: set[tuple[str, str]] = set()
    annotated: set[int] = set()  # 已追加在线确认标注的 existing 下标
    query_cache: dict[tuple[str, str, str], list[dict]] = {}  # 查询结果复用
    queries = 0
    issues: list[Issue] = []

    for rel, deps in file_deps:
        for dep in deps:
            if not dep.constraints:
                continue  # 无有效约束（npm "*" / git 协议）：不参与在线查询
            dep_iv = advisory.constraints_to_interval(dep.constraints)
            if dep_iv is None:
                continue
            pin = _exact_pin(dep)
            qkey = (dep.ecosystem, dep.name, pin)
            if qkey in seen_query:
                vulns = query_cache[qkey]  # 同包+版本只查一次，结果复用
            else:
                if queries >= max_queries:
                    if errors is not None:
                        errors[_BUDGET_KEY] = (
                            f"OSV 在线查询达到上限 {max_queries} 次，其余依赖未查询"
                        )
                    return issues
                seen_query.add(qkey)
                queries += 1
                vulns = fetch(dep.name, pin, dep.ecosystem, errors=errors)
                query_cache[qkey] = vulns
            for vuln in vulns:
                rid = vuln_rule_id(vuln)
                if not rid:
                    continue
                if (rel, rid) in seen_rule:
                    continue
                if not vuln_hits_interval(vuln, dep_iv):
                    continue  # 本地区间交集不符：远端条目不适用于本清单约束
                seen_rule.add((rel, rid))
                cve = vuln_cve(vuln)
                if cve and (rel, cve) in seed_keys:
                    # 与种子库命中同文件同 CVE：去重，给既有 Issue 加在线确认标注
                    for idx, iss in enumerate(existing):
                        if idx in annotated or iss.file != rel:
                            continue
                        if f"rule:DEP-CVE-{cve}" in iss.evidence:
                            iss.evidence.append("source:osv")
                            annotated.add(idx)
                            break
                    continue
                issues.append(build_osv_issue(rel, dep, vuln, rule_id=rid))
    return issues
