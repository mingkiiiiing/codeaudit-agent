"""Stage4 检测流水线：上下文构建、规则执行、行号校验、Issue 转换/去重与总入口。

对外 API（集成协议，见 docs/06 §4；Wave 2 契约 v1.2 扩展，见 docs/07 §3.2）：
- build_rule_contexts(ctx) -> list[RuleContext]
- run_rules(ctx) -> list[RuleHit]
- validate_issue_lines(issue, lines) -> bool
- hits_to_issues(hits, id_start=1) -> list[Issue]
- dedup_issues(issues) -> list[Issue]
- filter_suppressed(hits, lines) -> list[RuleHit]
  # 行内抑制（W4-A2，docs/09 §1 第 4 行）：命中行区间内任一行含
  # `# codeaudit: ignore` 丢弃该 hit；`# codeaudit: ignore[ID,...]` 仅当
  # hit.rule_id 在 ID 列表中才丢弃。只作用于静态规则结果（llm_only 模式与
  # LLM 审查通道不经过此过滤）。
- run_detection(ctx, review_fn=None, verify_fn=None) -> list[Issue]
  # review_fn 由集成时注入（simple=review_file 包装 / tools=make_tools_review_fn）；
  # 文件数 >1 时经 audit.agents.review.review_files_parallel 文件级并发审查；
  # 消融开关（docs/08 §3，契约 v1.3）：config.llm_only_mode=True 跳过规则执行、
  # Issue 全部来自 review_fn；config.enable_rule_hints=False 时传给 review_fn 的
  # hints 一律为空列表（规则结果照常进入候选，只是不引导 LLM）；
  # verify_fn 由集成时注入（包装 agents.verify.verify_issue），仅对 critical/high
  # 候选复核：confirmed 更新置信度保留 / false_positive（confidence<=0）从最终
  # ctx.issues 剔除但保留在 ctx.candidates / uncertain 降置信度保留；
  # 统计写入 ctx.extra["verify_stats"]={"checked","confirmed","rejected","uncertain"}。
"""

from __future__ import annotations

import inspect
import re
from typing import Any, Awaitable, Callable, Sequence

from audit.detect.base import RuleContext, RuleRegistry
from audit.detect.registry import get_registry
from audit.detect.rules._python_common import scan_python
from audit.models import Category, Issue, IssueSource, RuleHit, Severity
from audit.pipeline import PipelineContext
from audit.utils import guess_language, make_id
from audit.workspace import WorkspaceContext

__all__ = [
    "IssueReviewFn",
    "IssueVerifyFn",
    "MAX_ISSUES",
    "VERIFY_TRIGGER_SEVERITIES",
    "apply_verify",
    "build_rule_contexts",
    "dedup_issues",
    "filter_suppressed",
    "hits_to_issues",
    "run_detection",
    "run_rules",
    "validate_issue_lines",
]

MAX_ISSUES = 2000  # 单次审计 Issue 数量上限（超预算保护）

# review_fn(workspace, file_path, rule_hints) -> list[Issue]；允许同步或 async 实现
IssueReviewFn = Callable[[WorkspaceContext, str, list[str]], "Sequence[Issue] | Awaitable[Sequence[Issue]]"]

# verify_fn(workspace, issue) -> Issue；允许同步或 async 实现（docs/07 §3.2）
IssueVerifyFn = Callable[[WorkspaceContext, Issue], "Issue | Awaitable[Issue]"]

# 只复核 critical/high（medium/low 不复核，省成本）
VERIFY_TRIGGER_SEVERITIES = (Severity.CRITICAL, Severity.HIGH)

_SUGGESTION_BY_CATEGORY: dict[Category, str] = {
    Category.BUG: "请按上述位置核查代码逻辑并修复，随后针对该场景补充单元测试防止回归。",
    Category.PERFORMANCE: "建议按描述优化数据结构或算法（如 set/join、缓存不变量、批量 IO），并用大数据量验证收益。",
    Category.STYLE: "建议按团队编码规范调整此处实现，保持代码一致性与可维护性。",
    Category.SECURITY: "请优先修复该安全问题：改用参数化查询/安全 API，禁止拼接不可信输入或硬编码敏感信息，并排查同类用法。",
}


# ---------------------------------------------------------------- 上下文构建


def build_rule_contexts(ctx: PipelineContext) -> list[RuleContext]:
    """遍历工作区源文件，构建每个文件的规则检查上下文。

    - 语言用 audit.utils.guess_language 按扩展名判定，非源码文件跳过；
    - symbols 通过 ctx.index.symbols_for_file 获取（IndexStore 接口），索引缺失或
      查询异常时置空——规则不依赖索引也能工作；
    - Python 文件额外预计算掩码扫描结果（ctx.meta["pyscan"]）供各规则复用。
    """
    contexts: list[RuleContext] = []
    for path in ctx.workspace.source_files(ctx.config.languages or None):
        try:
            rel = ctx.workspace.rel(path)
            language = guess_language(rel)
            if not language:
                continue
            text = ctx.workspace.read_file_text(rel)
        except Exception:
            continue  # 单文件读取失败不阻断整体扫描
        lines = text.splitlines()
        symbols: list[Any] = []
        if ctx.index is not None:
            try:
                symbols = list(ctx.index.symbols_for_file(rel))
            except Exception:
                symbols = []
        rc = RuleContext(
            rel_path=rel,
            language=language,
            source=text,
            lines=lines,
            tree=None,
            symbols=symbols,
        )
        if language == "python":
            rc.meta["pyscan"] = scan_python(lines)
        contexts.append(rc)
    return contexts


# ---------------------------------------------------------------- 规则执行

# 行内抑制注释（semgrep 风格，docs/09 §1 第 4 行）：
#   `# codeaudit: ignore`              → 抑制本行全部规则；
#   `# codeaudit: ignore[ID1, ID2]`    → 只抑制列出的规则 ID（逗号分隔可含空格）。
# 关键字大小写不敏感；`ignore` 后必须紧跟单词边界（避免 "ignored" 误判）。
_INLINE_SUPPRESS_RE = re.compile(
    r"codeaudit\s*:\s*ignore\b(?:\[(?P<ids>[^\]]*)\])?", re.IGNORECASE
)


def _parse_suppress_ids(line: str) -> set[str] | None:
    """解析单行抑制注释。无抑制注释返回 None；裸 ignore 返回空集（=全部抑制）；
    ignore[IDs] 返回 ID 集合（ID 与注册表中的完整规则 id 精确匹配）。"""
    match = _INLINE_SUPPRESS_RE.search(line)
    if match is None:
        return None
    raw_ids = match.group("ids")
    if raw_ids is None:
        return set()  # 裸 ignore：抑制全部规则
    return {part.strip() for part in raw_ids.split(",") if part.strip()}


def filter_suppressed(hits: Sequence[RuleHit], lines: list[str]) -> list[RuleHit]:
    """过滤被行内抑制注释命中的静态规则结果（W4-A2，docs/09 §1 第 4 行）。

    判定范围：hit 的行区间 [line_start, line_end] 内任一行含抑制注释即生效——
    - `# codeaudit: ignore`（裸，解析为空集）：丢弃该 hit；
    - `# codeaudit: ignore[ID,...]`：hit.rule_id 在 ID 列表中才丢弃；
    - 无关注释 / ID 不在列表中：保留。
    只作用于静态规则结果；llm_only 模式与 LLM 审查结果不经此函数。
    文件不含任何 "codeaudit" 字样时快速原样返回（默认行为零改动）。
    """
    hits = list(hits)
    if not hits or not any("codeaudit" in line.lower() for line in lines):
        return hits
    suppress_by_line: dict[int, set[str]] = {}
    for lineno, line in enumerate(lines, start=1):
        ids = _parse_suppress_ids(line)
        if ids is not None:
            suppress_by_line[lineno] = ids
    if not suppress_by_line:
        return hits
    kept: list[RuleHit] = []
    for hit in hits:
        start = max(1, hit.line_start)
        end = max(start, hit.line_end)  # 行区间与 hits_to_issues 口径一致
        suppressed = False
        for lineno in range(start, end + 1):
            ids = suppress_by_line.get(lineno)
            if ids is None:
                continue  # 该行无抑制注释
            if not ids or hit.rule_id in ids:
                suppressed = True  # 裸 ignore 命中，或 hit 规则 ID 在列表中
                break
        if not suppressed:
            kept.append(hit)
    return kept


def _run_rules_on_contexts(
    contexts: list[RuleContext],
    registry: RuleRegistry,
    extra: dict[str, Any] | None = None,
) -> list[RuleHit]:
    disabled: set[str] = set()
    if extra and isinstance(extra.get("disabled_rules"), (list, tuple, set)):
        disabled = set(extra["disabled_rules"])
    hits: list[RuleHit] = []
    for rc in contexts:
        for rule in registry.rules_for(rc.language):
            if rule.id in disabled:
                continue
            try:
                found = list(rule.check(rc))
            except Exception as exc:  # 单规则崩溃不阻断整体
                if extra is not None:
                    extra.setdefault("rule_errors", {})[rule.id] = repr(exc)
                continue
            # 行内抑制接线：带 `# codeaudit: ignore[...]` 的行按语义丢弃命中
            hits.extend(filter_suppressed(found, rc.lines))
    hits.sort(key=lambda h: (h.file, h.line_start, h.rule_id))
    return hits


def run_rules(ctx: PipelineContext, registry: RuleRegistry | None = None) -> list[RuleHit]:
    """对工作区全部源文件执行注册表中该语言的全部规则，返回聚合 RuleHit。"""
    registry = registry or get_registry()
    contexts = build_rule_contexts(ctx)
    hits = _run_rules_on_contexts(contexts, registry, ctx.extra)
    ctx.rule_hits = hits
    return hits


# ---------------------------------------------------------------- 校验与转换


def validate_issue_lines(issue: Issue, lines: list[str]) -> bool:
    """行号真实性硬校验：越界或 start>end 返回 False（FR-3.4）。"""
    n = len(lines)
    if issue.line_start < 1 or issue.line_end < issue.line_start:
        return False
    return issue.line_start <= n and issue.line_end <= n


def hits_to_issues(
    hits: Sequence[RuleHit], id_start: int = 1, registry: RuleRegistry | None = None
) -> list[Issue]:
    """RuleHit -> Issue：source=RULE，confidence 取 hit.meta（默认 0.7），按序稳定编号。"""
    registry = registry or get_registry()
    desc_map = {r.id: r.description for r in registry.all_rules}
    issues: list[Issue] = []
    for i, hit in enumerate(hits):
        title = (hit.message or "").split("。")[0].strip() or f"{hit.rule_id} 检出问题"
        description = hit.message or ""
        rule_desc = desc_map.get(hit.rule_id, "")
        if rule_desc:
            description = f"{description}\n【规则 {hit.rule_id}】{rule_desc}"
        issues.append(
            Issue(
                id=make_id("ISS", id_start + i),
                category=hit.category,
                severity=hit.severity,
                title=title[:120],
                file=hit.file,
                line_start=hit.line_start,
                line_end=max(hit.line_end, hit.line_start),
                code_snippet=hit.snippet,
                description=description,
                evidence=[f"rule:{hit.rule_id}", f"loc:{hit.file}:{hit.line_start}-{hit.line_end}"],
                suggestion=_SUGGESTION_BY_CATEGORY.get(
                    hit.category, "请结合上下文评估并修复该问题。"
                ),
                confidence=float(hit.meta.get("confidence", 0.7)),
                source=IssueSource.RULE,
            )
        )
    return issues


# ---------------------------------------------------------------- 去重合并


def _line_close(a: Issue, b: Issue, tolerance: int = 5) -> bool:
    """行区间相近：区间相交或间距 ≤ tolerance 行。"""
    return b.line_start <= a.line_end + tolerance and a.line_start <= b.line_end + tolerance


def _merge_into(kept: Issue, other: Issue) -> None:
    """把 other 合并进 kept 槽位：保留置信度高的主体，拼接去重 evidence，跨源升级 source。"""
    primary, secondary = (kept, other) if kept.confidence >= other.confidence else (other, kept)
    sources = {primary.source, secondary.source}
    cross = sources == {IssueSource.RULE, IssueSource.LLM}
    merged_evidence = list(primary.evidence)
    for e in secondary.evidence:
        if e not in merged_evidence:
            merged_evidence.append(e)
    if cross or IssueSource.RULE_LLM in sources:
        merged_source = IssueSource.RULE_LLM
    else:
        merged_source = primary.source
    merged_confidence = min(1.0, max(primary.confidence, secondary.confidence) + (0.1 if cross else 0.0))
    description = primary.description
    if secondary.title and secondary.title != primary.title:
        description = f"{description}\n[合并] 同位置同类报告：{secondary.title}（来源 {secondary.source.value}）"
    kept.id = primary.id
    kept.category = primary.category
    kept.severity = primary.severity
    kept.title = primary.title
    kept.file = primary.file
    kept.line_start = primary.line_start
    kept.line_end = primary.line_end
    kept.code_snippet = primary.code_snippet
    kept.description = description
    kept.evidence = merged_evidence
    kept.suggestion = primary.suggestion
    kept.confidence = merged_confidence
    kept.source = merged_source


def dedup_issues(issues: Sequence[Issue]) -> list[Issue]:
    """按 (file, category, 行区间 ±5) 去重：保留置信度高者，合并 evidence 并升级 source。

    输出保持首次出现顺序（合并发生在先出现的位置上）。
    """
    kept: list[Issue] = []
    for issue in issues:
        target: Issue | None = None
        for k in kept:
            if k.file == issue.file and k.category == issue.category and _line_close(k, issue):
                target = k
                break
        if target is None:
            kept.append(issue)
        else:
            _merge_into(target, issue)
    return kept


# ---------------------------------------------------------------- Verify 复核接入


def _classify_verify_result(result: Issue) -> str:
    """把 verify_fn 的返回裁定归类为 "rejected" / "uncertain" / "confirmed"。

    - confidence <= 0：false_positive（agents.verify.verify_issue 驳回时置 0）→ rejected；
    - description 带 uncertain/降级标记（verify_issue 的裁定文本）→ uncertain；
    - 其余 → confirmed。
    """
    if float(result.confidence or 0.0) <= 0.0:
        return "rejected"
    description = result.description or ""
    if "[Verify:uncertain]" in description or "[Verify 降级]" in description:
        return "uncertain"
    return "confirmed"


async def apply_verify(
    ctx: PipelineContext, candidates: Sequence[Issue], verify_fn: IssueVerifyFn
) -> tuple[list[Issue], dict[str, int]]:
    """对合并后的候选执行 Verify 复核，返回 (保留清单, 统计)。

    - 仅 severity ∈ {critical, high} 的候选逐条调用 verify_fn（medium/low 不复核，省成本）；
    - 兼容同步/async 的 verify_fn；
    - rejected（confidence<=0）不进保留清单（调用方保证其仍留在 ctx.candidates）；
    - uncertain 保留（confidence 已由 verify_issue 降为 min(原值, 0.5)）；
    - confirmed 保留并应用更新后的 confidence/severity；
    - verify_fn 异常或不返回 Issue 时保底保留原候选（复核故障不删问题）。
    """
    stats = {"checked": 0, "confirmed": 0, "rejected": 0, "uncertain": 0}
    kept: list[Issue] = []
    verify_errors: dict[str, str] = ctx.extra.setdefault("verify_errors", {})
    for issue in candidates:
        if issue.severity not in VERIFY_TRIGGER_SEVERITIES:
            kept.append(issue)
            continue
        try:
            result = verify_fn(ctx.workspace, issue)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # 复核故障不剔除候选，只记录
            verify_errors[issue.id or f"{issue.file}:{issue.line_start}"] = repr(exc)
            kept.append(issue)
            continue
        if not isinstance(result, Issue):
            kept.append(issue)
            continue
        stats["checked"] += 1
        verdict = _classify_verify_result(result)
        if verdict == "rejected":
            stats["rejected"] += 1
            continue  # 从最终 issues 剔除；candidates 保留（供误报率统计）
        stats[verdict] += 1
        kept.append(result)
    return kept, stats


# ---------------------------------------------------------------- 总入口


async def run_detection(
    ctx: PipelineContext,
    review_fn: IssueReviewFn | None = None,
    verify_fn: IssueVerifyFn | None = None,
) -> list[Issue]:
    """检测阶段总入口。

    流程：
    1. run_rules 全库静态规则扫描 -> hits_to_issues 形成规则候选（高召回）；
       config.llm_only_mode=True 时跳过规则执行（ctx.rule_hits 保持空），
       Issue 全部来自 LLM 审查通道（review_fn 缺失则直接返回空清单）；
    2. 若 config.enable_llm_review（llm_only 模式下恒视为开启）且提供了 review_fn：
       逐文件调用 LLM 审查通道（默认注入规则命中作为 hint；config.enable_rule_hints
       =False 时 hints 一律为空列表——规则自己报但不引导 LLM），产出 Issue 做行号
       硬校验（越界即丢弃），再与规则结果按 (file, category, ±5 行) 去重合并
       （跨源升级为 rule+llm）；
       文件数 >1 时改用 review_files_parallel 文件级并发审查（单文件保持顺序路径）；
    3. 若提供了 verify_fn：对合并后的候选按 severity ∈ {critical, high} 逐条复核，
       驳回者从最终 issues 剔除但保留在 candidates，统计写 ctx.extra["verify_stats"]；
    4. 超预算保护：候选 > MAX_ISSUES 时截断并记录到 ctx.extra；
    5. 结果写入 ctx.rule_hits / ctx.candidates / ctx.issues 并返回。

    verify_fn 为 None 时行为与旧版完全一致；消融开关语义见 docs/08 §3（契约 v1.3）。
    """
    registry = get_registry()
    llm_only = bool(getattr(ctx.config, "llm_only_mode", False))
    contexts = build_rule_contexts(ctx)
    if llm_only:  # 纯 LLM 模式：跳过规则执行（文件清单仍复用 build_rule_contexts）
        hits: list[RuleHit] = []
        ctx.rule_hits = []
    else:
        hits = _run_rules_on_contexts(contexts, registry, ctx.extra)
        ctx.rule_hits = hits
    rule_issues = hits_to_issues(hits, id_start=1, registry=registry)

    info: dict[str, Any] = {
        "mode": "llm_only" if llm_only else "rule",
        "rule_hits": len(hits),
        "rule_issues": len(rule_issues),
        "files_reviewed": 0,
        "llm_issues": 0,
        "llm_filtered": 0,
        "truncated_from": None,
    }
    candidates: list[Issue] = list(rule_issues)

    if llm_only and review_fn is None:
        # 纯 LLM 模式但没有审查通道：无可产出源，直接返回空清单
        info["mode"] = "llm_only(no review_fn)"
        candidates = []

    wants_llm = review_fn is not None and (llm_only or ctx.config.enable_llm_review)
    if wants_llm:
        info["mode"] = "llm_only" if llm_only else "rule+llm"
        hits_by_file: dict[str, list[RuleHit]] = {}
        for h in hits:
            hits_by_file.setdefault(h.file, []).append(h)
        hints_enabled = bool(getattr(ctx.config, "enable_rule_hints", True))

        def _hints_of(rel_path: str) -> list[str]:
            # 消融 −rule_hints：规则命中照常进入规则候选，但不注入 LLM prompt
            if not hints_enabled:
                return []
            return [
                f"{h.rule_id}|{h.severity.value}|{h.line_start}-{h.line_end}|{h.message}"
                for h in hits_by_file.get(rel_path, [])
            ]

        llm_issues: list[Issue] = []
        next_id = len(rule_issues) + 1
        per_file_results: dict[str, list[Issue]] = {}
        failed_files: set[str] = set()

        if len(contexts) > 1:
            # 文件级并发审查（同进程内直接调用 W2-A3 的 review_files_parallel）
            from audit.agents.review import review_files_parallel

            jobs = [(rc.rel_path, _hints_of(rc.rel_path)) for rc in contexts]
            per_file_results = await review_files_parallel(ctx, jobs, review_fn=review_fn)
            # R1-20 差集判定：review_files_parallel 只为成功的文件返回条目（失败文件
            # 返回 None 不入结果），用"全部 job − 成功集合"判定失败，避免依赖
            # review_errors 的前后差集（同文件重复失败且消息相同时会漏计）。
            failed_files = {rel for rel, _ in jobs} - set(per_file_results)
        elif contexts:
            # 单文件：保持原有顺序路径（含逐文件异常记录语义）
            rc0 = contexts[0]
            try:
                result = review_fn(ctx.workspace, rc0.rel_path, _hints_of(rc0.rel_path))
                if inspect.isawaitable(result):
                    result = await result
                per_file_results[rc0.rel_path] = list(result or [])
            except Exception as exc:  # LLM 通道故障不影响规则结果
                ctx.extra.setdefault("review_errors", {})[rc0.rel_path] = repr(exc)
                failed_files.add(rc0.rel_path)

        for rc in contexts:
            if rc.rel_path in failed_files:
                continue
            info["files_reviewed"] += 1
            for issue in per_file_results.get(rc.rel_path) or []:
                if not isinstance(issue, Issue):
                    continue
                if not validate_issue_lines(issue, rc.lines):
                    info["llm_filtered"] += 1
                    continue
                if not issue.id:
                    issue.id = make_id("ISS", next_id)
                    next_id += 1
                llm_issues.append(issue)
        info["llm_issues"] = len(llm_issues)
        # 去重合并对纯 LLM 结果同样生效（llm_only 模式下 rule_issues 为空）
        candidates = dedup_issues(rule_issues + llm_issues)

    if len(candidates) > MAX_ISSUES:
        info["truncated_from"] = len(candidates)
        candidates = candidates[:MAX_ISSUES]

    verify_stats: dict[str, int] | None = None
    final_issues: list[Issue] = list(candidates)
    if verify_fn is not None:
        try:
            from audit.agents.prompts import PROMPT_VERSION

            ctx.extra.setdefault("prompt_version", PROMPT_VERSION)
        except Exception:  # pragma: no cover - prompts 模块缺失时静默
            pass
        # 驳回者从最终 issues 剔除，但 candidates 保留全部复核前候选（含驳回，供误报率统计）
        final_issues, verify_stats = await apply_verify(ctx, candidates, verify_fn)
        ctx.extra["verify_stats"] = verify_stats

    info["verify"] = verify_stats is not None
    ctx.extra["detection"] = info
    ctx.candidates = list(candidates)
    ctx.issues = list(final_issues)
    try:
        await ctx.emit(
            "detect",
            f"检测完成：{len(ctx.issues)} 个问题（模式：{info['mode']}）",
            current=len(ctx.issues),
            total=len(ctx.issues),
        )
    except Exception:
        pass  # 进度事件尽力而为
    return ctx.issues
