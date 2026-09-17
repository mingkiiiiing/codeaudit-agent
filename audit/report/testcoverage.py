"""测试覆盖盲区识别（审计 P0-5）：critical/high 问题所在非测试文件的测试触达反查。

口径（与报告 untested_hotspots 节的 methodology 一致）：
- eligible：存在 critical/high Issue 且非测试文件（is_test_file 现有口径，见
  ``audit.detect.rules._python_common.is_test_file`` / JS 侧 ``is_js_test_file``）；
- 触达：文件 F 的公开符号（简名不以 ``_`` 开头，覆盖 function/method/class/constant）
  经索引 ``references()`` 按符号名反查 call_edges（含未解析边与全文文本兜底），
  任一调用者来自测试文件 → F 有触达；
- 盲区：eligible 中零触达的文件，输出 TOP 20 + 占比（盲区数 / eligible 数）。

诚实局限（写入 methodology，随节渲染）：
- 名字级反查会把同名符号、测试文件中的文本提及误判为触达 → 结果偏乐观（盲区可能被低估）；
- 纯静态反查覆盖不了动态调用（getattr/反射/框架隐式调用）→ 结果偏悲观；
- 索引跨文件解析率（resolved_ratio）偏低时两种误差都放大，本节仅供测试补充排期参考。

只消费 IndexStore 现有查询 API（symbols_for_file/references/stats），不改契约。
计算全程兜底：任何异常降级为 available=False 的空结构，绝不打断报告阶段。
"""

from __future__ import annotations

from typing import Any

from audit.pipeline import PipelineContext

TOP_LIMIT = 20  # 盲区清单展示上限（超出以 omitted 计数）

_HIGH_SEVERITIES = ("critical", "high")
_SEVERITY_RANK = {"critical": 0, "high": 1}

_METHODOLOGY = (
    "口径：eligible = 存在 critical/high 问题且非测试文件（tests/ 或 test/ 目录、"
    "test_*.py、*_test.py、conftest.py、*.spec.* 等）的文件；触达 = 文件的公开符号"
    "（简名不以 '_' 开头）按符号名反查调用索引（call_edges，含未解析边与全文文本"
    "兜底），存在来自测试文件的调用者即视为有触达。局限：名字级反查会把同名符号/"
    "测试文本提及误判为触达，结果偏乐观（盲区可能被低估）；静态反查覆盖不了动态"
    "调用（getattr/反射），结果偏悲观；索引跨文件解析率（resolved_ratio）偏低时"
    "两种误差都放大。本节为静态近似，仅供测试补充排期参考。"
)


def _is_test_path(rel: str) -> bool:
    """测试文件判定：优先复用 detect 层现有口径（python + js 并集），失败回退内联同口径实现。"""
    try:
        from audit.detect.rules._python_common import is_test_file as py_test
        from audit.detect.rules.js._js_common import is_js_test_file as js_test

        return bool(py_test(rel) or js_test(rel))
    except ImportError:
        return _is_test_path_fallback(rel)


def _is_test_path_fallback(rel: str) -> bool:
    """内联回退口径：与 detect 层 is_test_file / is_js_test_file 保持一致的并集。"""
    parts = rel.replace("\\", "/").split("/")
    parents, name = parts[:-1], parts[-1]
    if any(p in ("tests", "test", "__tests__") for p in parents):
        return True
    return (
        name.startswith("test_")
        or name == "conftest.py"
        or name.endswith("_test.py")
        or name.endswith((".test.js", ".test.ts", ".test.jsx", ".test.tsx"))
        or name.endswith((".spec.js", ".spec.ts", ".spec.jsx", ".spec.tsx"))
    )


def _public_symbols(symbols: list) -> list[str]:
    """文件的公开符号简名（去重保序）：限定名取最后一段，非 '_' 前缀视为公开。"""
    out: list[str] = []
    seen: set[str] = set()
    for sym in symbols:
        short = str(sym.name).split(".")[-1]
        if not short or short.startswith("_") or short in seen:
            continue
        seen.add(short)
        out.append(short)
    return out


def _file_reached_by_tests(index: Any, file: str, public: list[str]) -> bool:
    """文件任一公开符号存在来自测试文件的调用者（名字级反查，含未解析边与文本兜底）。"""
    for name in public:
        try:
            refs = index.references(name)
        except Exception:  # noqa: BLE001 —— 单符号反查失败不影响整体判定
            continue
        for ref in refs:
            if _is_test_path(str(ref.file)):
                return True
    return False


def _empty(available: bool = False, note: str = "") -> dict[str, Any]:
    """空节结构（字段稳定，供渲染层无需判空分支）。"""
    return {
        "available": available,
        "eligible_files": 0,
        "untested_files": 0,
        "ratio": 0.0,
        "resolved_ratio": None,
        "top_limit": TOP_LIMIT,
        "items": [],
        "omitted": 0,
        "methodology": _METHODOLOGY + note,
    }


def compute_untested_hotspots(ctx: PipelineContext, top_limit: int = TOP_LIMIT) -> dict[str, Any]:
    """计算测试覆盖盲区：critical/high 问题所在非测试文件中零测试触达的清单。

    结果同时写入 ctx.extra["untested_hotspots"]（对齐 verify_stats/fix_stats 的
    extra 通道）并由 builder 挂到 AuditReport.untested_hotspots。索引不可用
    （ingest 失败/未建索引）或计算异常时返回 available=False 的空结构。
    """
    try:
        return _compute(ctx, top_limit)
    except Exception as exc:  # noqa: BLE001 —— 报告阶段永不因本节失败
        return _empty(note=f"（本次计算异常降级：{type(exc).__name__}）")


def _compute(ctx: PipelineContext, top_limit: int) -> dict[str, Any]:
    index = ctx.index if ctx.index is not None else getattr(ctx.workspace, "index", None)
    if index is None:
        return _empty(note="（索引不可用，未计算）")

    # eligible：critical/high 问题落在非测试文件的集合（含问题聚合信息）
    file_issues: dict[str, list] = {}
    for issue in ctx.issues:
        sev = issue.severity.value if hasattr(issue.severity, "value") else str(issue.severity)
        if sev not in _HIGH_SEVERITIES or not issue.file:
            continue
        file_issues.setdefault(str(issue.file), []).append((issue.id, sev))

    eligible: dict[str, list] = {}
    for file, marks in file_issues.items():
        if _is_test_path(file):
            continue  # 测试文件自身的问题不参与盲区口径
        eligible[file] = marks
    if not eligible:
        result = _empty(available=True)
        try:
            result["resolved_ratio"] = _resolved_ratio(index)
        except Exception:  # noqa: BLE001 —— 统计字段尽力而为
            pass
        return result

    resolved_ratio = None
    try:
        resolved_ratio = _resolved_ratio(index)
    except Exception:  # noqa: BLE001 —— 统计字段尽力而为
        pass

    items: list[dict[str, Any]] = []
    for file, marks in eligible.items():
        try:
            symbols = index.symbols_for_file(file)
        except Exception:  # noqa: BLE001 —— 单文件符号查询失败按无公开符号处理
            symbols = []
        public = _public_symbols(list(symbols))
        if not _file_reached_by_tests(index, file, public):
            sev_set = {s for _, s in marks}
            items.append(
                {
                    "file": file,
                    "issue_count": len(marks),
                    "issue_ids": [i for i, _ in marks],
                    "severities": [s for s in _HIGH_SEVERITIES if s in sev_set],
                    "public_symbols": public,
                    "public_symbol_count": len(public),
                }
            )

    # 排序：critical 优先于 high，同级别按文件名（展示稳定）
    items.sort(key=lambda it: (_SEVERITY_RANK.get(it["severities"][0], 9), it["file"]))
    omitted = max(0, len(items) - top_limit)
    untested = len(items)
    ratio = round(untested / len(eligible), 4) if eligible else 0.0
    return {
        "available": True,
        "eligible_files": len(eligible),
        "untested_files": untested,
        "ratio": ratio,
        "resolved_ratio": resolved_ratio,
        "top_limit": top_limit,
        "items": items[:top_limit],
        "omitted": omitted,
        "methodology": _METHODOLOGY,
    }


def _resolved_ratio(index: Any) -> float | None:
    """索引跨文件解析率（诚实口径标注用）；不可得时返回 None。"""
    stats = index.stats()
    if not isinstance(stats, dict):
        return None
    value = stats.get("resolved_ratio")
    return float(value) if value is not None else None
