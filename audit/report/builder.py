"""Stage7 报告聚合器：把 PipelineContext 中的全部阶段产物汇总为 AuditReport。"""

from __future__ import annotations

from pathlib import Path

from audit.models import AuditReport, AuditStats, count_by_severity, health_score
from audit.pipeline import PipelineContext
from audit.utils import guess_language, now_iso

# ctx.stats 与 llm.usage_totals() 的重叠键：两者取较大值，避免阶段内已累计时被重复相加
_LLM_STAT_KEYS = ("llm_calls", "prompt_tokens", "completion_tokens", "cache_hits", "cache_misses")

# 生成单测目录（testgen 落盘位置，R1-10）：语言/LOC 统计排除，避免自产代码污染占比
GENERATED_TEST_DIR = "tests/generated"


def _is_generated_test(rel: str) -> bool:
    posix = rel.replace("\\", "/")
    return posix == GENERATED_TEST_DIR or posix.startswith(GENERATED_TEST_DIR + "/")


def _collect_language_stats(ctx: PipelineContext) -> tuple[dict[str, float], int, int]:
    """统计语言占比与总行数（R1-10：排除 tests/generated/ 下的生成测试）。

    返回 (languages, loc, files_total)：
      - languages: 语言 -> 文件数占比（百分比，保留 1 位小数，未知语言不计入）
      - loc: 全部源文件行数合计
      - files_total: 源文件总数

    R4-4：ingest 未成功时 ctx.workspace 仍指向用户原始输入目录，
    统计走空 manifests（files_total=0），绝不回扫用户目录。
    """
    if not ctx.extra.get("ingest_ok", True):
        return {}, 0, 0
    lang_files: dict[str, int] = {}
    files_total = 0
    loc = 0
    for path in ctx.workspace.source_files():
        if _is_generated_test(ctx.workspace.rel(path)):
            continue
        files_total += 1
        lang = guess_language(path)
        if lang:
            lang_files[lang] = lang_files.get(lang, 0) + 1
        try:
            loc += ctx.workspace.line_count(path)
        except OSError:
            continue  # 读取失败的文件不计行数，但不让报告失败
    if files_total > 0:
        languages = {k: round(v * 100.0 / files_total, 1) for k, v in sorted(lang_files.items())}
    else:
        languages = {}
    return languages, loc, files_total


def _merge_stats(ctx: PipelineContext, files_total: int, loc: int) -> AuditStats:
    """合并阶段统计与 LLM 用量：重叠键取较大值（LLM 客户端用量为权威值）。"""
    merged = ctx.stats.to_dict()
    for key, value in ctx.llm.usage_totals().items():
        if key in _LLM_STAT_KEYS:
            merged[key] = max(int(merged.get(key) or 0), int(value))
    merged["files_total"] = files_total
    merged["loc_total"] = loc
    return AuditStats.from_dict(merged)


def build_report(ctx: PipelineContext) -> AuditReport:
    """聚合 PipelineContext 全部产物为 AuditReport。

    - project_name 取工作副本根目录名；
    - languages / loc 从 workspace.source_files() 现场统计；
    - summary / health_score 由最终 Issue 清单推导（复用 audit.models 函数）；
    - stats 合并 ctx.stats 与 ctx.llm.usage_totals()。
    """
    languages, loc, files_total = _collect_language_stats(ctx)
    src_root: Path = ctx.workspace.src_root
    # 项目名：优先取编排层注入的原始路径名（T2 工作副本目录名固定为 src，
    # 不能反映真实项目名）；未注入时回退 src_root 目录名（契约默认行为）。
    project_name = str(ctx.extra.get("project_name") or "").strip() or src_root.name
    report = AuditReport(
        audit_id=ctx.workspace.audit_id,
        project_name=project_name,
        languages=languages,
        loc=loc,
        health_score=health_score(ctx.issues, loc),
        summary=count_by_severity(ctx.issues),
        issues=list(ctx.issues),
        patches=list(ctx.patches),
        test_cases=list(ctx.test_cases),
        refactor_proposals=list(ctx.refactor_proposals),  # 契约 v1.7
        architecture=ctx.architecture,
        stats=_merge_stats(ctx, files_total, loc),
        created_at=now_iso(),
    )
    return report
