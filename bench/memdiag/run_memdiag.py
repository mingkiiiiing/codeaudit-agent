"""W11-A4 库层内存诊断：逐任务 run_audit + RSS 采样 + tracemalloc 快照 diff，归因每任务内存缓爬。

背景（docs/15、bench/results/soak_w10.md）：HTTP soak 实测每任务 ~0.15-0.27 MB 的 RSS 缓爬，
与累计任务数 Pearson r=0.68，判 DEFER（疑似每任务常数缓存而非时间泄漏），未归因。
本脚本绕过 HTTP 层（服务进程 RSS 已由 soak 覆盖），以库层直调 asyncio.run(run_audit(...))
模拟服务端逐任务行为：每轮独立 work_root、独立 SQLite 索引、独立 LLM 客户端（离线为 FakeLLM），
用 tracemalloc 行级快照 diff 定位 Python 对象层面的滞留来源，RSS 只作进程级佐证。

方法要点：
  - 第 1 轮为预热轮（懒加载导入 audit/* 及依赖，RSS 抬升属一次性）；
  - 第 2 轮起：轮前 gc.collect() + tracemalloc.start(25) + take_snapshot()，跑完审计后再
    gc.collect() + take_snapshot()，与轮前 diff compare_to('lineno')，记录 top10 增长行；
    轮前快照即基线，轮间 diff 只含「本轮新滞留 + 本轮未释放暂态」，可跨轮累计归因到文件；
  - 全程离线：入口即清除 GLM_API_KEY / GLM_BASE_URL / GLM_MODEL；
  - 只诊断不修：不改动 audit/* 任何文件；修复建议写入报告供集成人裁决。

用法：
    python -m bench.memdiag.run_memdiag            # 完整 30 轮
    python -m bench.memdiag.run_memdiag --quick    # 10 轮冒烟
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import gc
import logging
import math
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import tracemalloc
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MINI_APP = PROJECT_ROOT / "demo" / "mini_app"
DEFAULT_OUT = PROJECT_ROOT / "bench" / "results" / "memdiag_w11.md"
QUICK_ROUNDS = 10
DEFAULT_ROUNDS = 30
TRACE_DEPTH = 25
TOP_LINES_PER_ROUND = 10
TOP_FILES_IN_REPORT = 15
STEADY_FROM_ROUND = 6  # 1-based：第 6 轮起视为稳态（第 1 轮导入预热、第 2-5 轮缓存填充）
LINEAR_STEADY_KB = 0.2  # 稳态轮均净增超过该值（KB/轮）判「线性增长」


# ---------------------------------------------------------------- 进程指标（Windows 当前进程 RSS）
def _win_rss_mb() -> tuple[float, float] | None:
    """取当前进程工作集（current, peak），单位 MB；非 Windows 或失败返回 None。

    与 bench.adversarial._win_rss_mb 同口径，但那里需要子进程句柄（proc._handle），
    这里用 psapi.GetCurrentProcess() 伪句柄直接测本进程，不经子进程。
    """
    if sys.platform != "win32":
        return None
    from ctypes import wintypes

    class _PMC(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    try:
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        # GetCurrentProcess 在 kernel32（返回伪句柄 -1），GetProcessMemoryInfo 在 psapi；
        # 必须显式声明 argtypes：64 位下 HANDLE 若按默认 c_int 传参，-1 伪句柄会被截断为无效句柄
        ctypes.windll.psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
        handle = wintypes.HANDLE(ctypes.windll.kernel32.GetCurrentProcess())
        if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
            return pmc.WorkingSetSize / 1048576, pmc.PeakWorkingSetSize / 1048576
    except Exception:  # noqa: BLE001 —— 指标可选，取不到不影响诊断主体
        return None
    return None


# ---------------------------------------------------------------- 数值工具
def _slope(xs: list[float], ys: list[float]) -> float | None:
    """最小二乘斜率；样本不足或 x 无方差返回 None。"""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / denom


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson 相关系数；样本不足或零方差返回 None。"""
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return sxy / (sx * sy)


def _rel(path: str) -> str:
    """绝对路径转项目内相对显示路径；项目外路径原样返回。"""
    try:
        return str(Path(path).resolve().relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return path.replace("\\", "/")


# ---------------------------------------------------------------- 每轮执行与快照
async def _noop(event: dict[str, Any]) -> None:
    """空事件发射器：吞掉全部进度事件（与 soak / adversarial 库层直调同口径）。"""


def _run_round(round_no: int, tmp_root: Path) -> tuple[dict[str, Any], list[tuple[str, float]], dict[str, float]]:
    """跑一轮完整审计并采样。

    返回 (轮摘要, top10 行级增长[(文件:行, 增量KB)], 文件级净增聚合{文件: KB})。
    第 1 轮为预热轮（懒加载导入），不做 tracemalloc（快照里全是导入噪声）。
    """
    from audit.config import AuditConfig
    from audit.orchestrator.pipeline import run_audit

    work_root = tmp_root / f"round_{round_no:03d}"
    cfg = AuditConfig(
        source_path=str(MINI_APP),
        work_root=str(work_root),
        out_dir=str(work_root),
        enable_llm_review=False,  # 纯规则模式：FakeLLM，无 httpx 连接池（与 soak 离线口径一致）
    )

    tracing = round_no >= 2
    before = None
    if tracing:
        gc.collect()
        tracemalloc.start(TRACE_DEPTH)
        before = tracemalloc.take_snapshot()

    t0 = time.perf_counter()
    report = asyncio.run(run_audit(cfg, _noop))
    duration = time.perf_counter() - t0

    traced_cur = traced_peak = None
    top_lines: list[tuple[str, float]] = []
    file_net: dict[str, float] = {}
    if tracing:
        gc.collect()  # 先清 cyclic garbage，再取 after 快照：diff 只含可达滞留
        after = tracemalloc.take_snapshot()
        traced_cur, traced_peak = (b / 1048576 for b in tracemalloc.get_traced_memory())
        diff = after.compare_to(before, "lineno")  # type: ignore[arg-type]
        tracemalloc.stop()
        for stat in diff:
            size_kb = stat.size_diff / 1024
            frame = stat.traceback[0]
            file_net[frame.filename] = file_net.get(frame.filename, 0.0) + size_kb
            if size_kb > 0:
                top_lines.append((f"{_rel(frame.filename)}:{frame.lineno}", size_kb))
        top_lines.sort(key=lambda kv: kv[1], reverse=True)
        top_lines = top_lines[:TOP_LINES_PER_ROUND]

    rss = _win_rss_mb()
    summary: dict[str, Any] = {
        "round": round_no,
        "duration": duration,
        "rss": rss[0] if rss else None,
        "peak": rss[1] if rss else None,
        "traced_cur": traced_cur,
        "traced_peak": traced_peak,
        "issues": len(report.issues),
        "top1": f"{top_lines[0][0]} +{top_lines[0][1]:.1f}KB" if top_lines else "-",
    }
    return summary, top_lines, file_net


# ---------------------------------------------------------------- 运行末残留盘点
def _inventory() -> list[str]:
    """全部轮次结束后盘点全局残留：可疑对象存活数、事件循环、线程、logging handler、re 缓存。"""
    gc.collect()
    lines: list[str] = []

    suspects: dict[type, str] = {sqlite3.Connection: "sqlite3.Connection（索引库连接）"}
    for module_name, class_name, label in (
        ("audit.indexer.store", "SqliteIndexStore", "SqliteIndexStore（索引存储实例）"),
        ("audit.workspace", "WorkspaceContext", "WorkspaceContext（工作区上下文）"),
        ("audit.pipeline", "PipelineContext", "PipelineContext（流水线上下文）"),
        ("audit.llm.glm_client", "GlmClient", "GlmClient（真实 LLM 客户端）"),
        ("audit.llm.base", "FakeLLMClient", "FakeLLMClient（离线假客户端）"),
        ("logging.Handler", "Handler", "logging.Handler（日志处理器）"),
    ):
        try:
            module = __import__(module_name, fromlist=[class_name])
            suspects[getattr(module, class_name)] = label
        except (ImportError, AttributeError):  # pragma: no cover —— 模块缺成员时跳过该项
            continue

    counts = {label: 0 for label in suspects.values()}
    loops = 0
    type_to_label = {t: label for t, label in suspects.items()}
    for obj in gc.get_objects():
        label = type_to_label.get(type(obj))
        if label is not None:
            counts[label] += 1
        elif type(obj).__name__.endswith("EventLoop"):
            loops += 1
    for label, n in counts.items():
        verdict = "无残留" if n == 0 else f"存活 {n} 个"
        lines.append(f"- {label}：{verdict}")
    lines.append(f"- 事件循环对象（asyncio Loop）残留：{loops} 个")
    lines.append(f"- 存活线程数（含主线程）：{threading.active_count()} 个")

    total_handlers = len(logging.getLogger().handlers)
    for lg in logging.Logger.manager.loggerDict.values():
        if isinstance(lg, logging.Logger):
            total_handlers += len(lg.handlers)
    lines.append(f"- logging 全局 handler 总数（root + 具名 logger）：{total_handlers} 个")

    re_cache = getattr(re, "_cache", None)
    lines.append(f"- re 模块编译缓存条目（上限 512）：{len(re_cache) if re_cache is not None else '不可见'} 条")

    try:
        from audit.indexer.parsers import get_language, get_parser

        li, pi = get_language.cache_info(), get_parser.cache_info()
        lines.append(
            f"- audit/indexer/parsers.py lru_cache：language currsize={li.currsize}（hits={li.hits}），"
            f"parser currsize={pi.currsize}（hits={pi.hits}）——按语言数有界（≤3）"
        )
    except ImportError:  # pragma: no cover
        lines.append("- audit/indexer/parsers.py lru_cache：模块不可用")
    return lines


# ---------------------------------------------------------------- tracemalloc 跨轮聚合与分类
def _classify(series: list[float]) -> str:
    """对单文件跨轮净增序列分类：线性增长 / 预热集中 / 平台期波动。

    - 稳态（后 5 轮）轮均净增 > LINEAR_STEADY_KB → 线性增长（真滞留信号）；
    - 前 3 轮占累计 ≥80% 且稳态不再增 → 预热集中（缓存/预热，一次性行为）；
    - 其余 → 平台期波动。
    """
    total = sum(series)
    steady = series[-5:]
    steady_mean = sum(steady) / len(steady)
    if steady_mean > LINEAR_STEADY_KB:
        return "线性增长"
    first3 = sum(series[:3])
    if total > 0 and first3 >= 0.8 * total:
        return "预热集中"
    return "平台期波动"


def _aggregate_file_growth(
    per_round_files: list[dict[str, float]],
) -> list[tuple[str, float, float, float, float, str]]:
    """按文件跨轮累计净增，返回 top N：(文件, 累计KB, 轮均KB, 前3轮占比, 后5轮轮均KB, 判定)。"""
    n_rounds = len(per_round_files)
    files: set[str] = set()
    for mapping in per_round_files:
        files.update(mapping)
    rows: list[tuple[str, float, float, float, float, str]] = []
    for name in files:
        series = [mapping.get(name, 0.0) for mapping in per_round_files]
        total = sum(series)
        if total <= 1.0:  # 累计净增 ≤1KB 的文件不进报告（噪声）
            continue
        first3 = sum(series[:3])
        steady = series[-5:]
        steady_mean = sum(steady) / len(steady)
        rows.append(
            (
                _rel(name),
                total,
                total / n_rounds,
                (first3 / total) if total > 0 else 0.0,
                steady_mean,
                _classify(series),
            )
        )
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows[:TOP_FILES_IN_REPORT]


# ---------------------------------------------------------------- 报告渲染
_SUSPECT_ROWS = (
    (
        "audit/indexer/parsers.py:19/31 lru_cache（Language/Parser）",
        "maxsize=None 按语言名缓存；键空间=3 种语言，实测 currsize 见盘点节",
        "有界（≤3 条目）",
        "无需修复",
    ),
    (
        "audit/indexer/store.py:142 _alias_cache / _lang_map（实例级）",
        "实例随 SqliteIndexStore 创建，build 前重置（store.py:433-436）；连接与实例生命周期见盘点节",
        "有界（任务级实例，finally close）",
        "若盘点节末态存活 >0 则需排查外部引用链",
    ),
    (
        "audit/indexer/store.py:130 SQLite 连接与语句缓存",
        "每轮新建连接；orchestrator/pipeline.py finally 统一 close（R1-4 收口）",
        "有界（连接随任务关闭）",
        "盘点节 sqlite3.Connection 末态存活数为直接证据",
    ),
    (
        "tree-sitter Parser 实例（parsers.py）",
        "经 lru_cache 复用 3 个 Parser 实例；tree 对象随任务 GC",
        "有界",
        "无需修复",
    ),
    (
        "audit/detect/rules 正则编译缓存",
        "绝大多数为模块级/类级一次性编译；python_ext.py:557 循环内按变量名动态 re.compile，"
        "进入 re 模块全局缓存（512 条上限，实测见盘点节）",
        "有界（512 条上限）但动态键会占满缓存",
        "建议（集成人裁决）：python_ext.py 局部变量赋值检测改为预编译模板 + 子模式复用，"
        "避免动态 pattern 污染 re 全局缓存（约数十 KB 量级一次性占用）",
    ),
    (
        "audit/llm/glm_client.py:57 _cache（响应缓存）",
        "本诊断 enable_llm_review=False → FakeLLM，GlmClient 未构造；HTTP 服务场景为 FIFO 容量 4096 上限",
        "有界（FIFO 4096；且为任务级实例）",
        "无需修复；若服务端未来跨任务共享 GlmClient，需复核 4096×单响应体上限",
    ),
    (
        "audit/llm/base.py FakeLLMClient.calls（调用记录）",
        "实例级列表，随 PipelineContext/ctx 一起可回收",
        "有界（任务级）",
        "无需修复",
    ),
    (
        "audit/report/builder.py 模块级状态",
        "仅有 _LLM_STAT_KEYS 等常量元组，无可变模块级容器",
        "有界",
        "无需修复",
    ),
    (
        "asyncio 循环每轮 asyncio.run 新建销毁",
        "asyncio.run 退出时 shutdown_asyncgens/default_executor 后 close；残留见盘点节 Loop 计数",
        "有界（无跨轮 Loop 累积）",
        "RSS 层面的残差属 CPython/MSVC 分配器高水位，tracemalloc 不可见，非 Python 对象泄漏",
    ),
    (
        "logging handler 累积",
        "grep 全 audit/ 无 getLogger/basicConfig/addHandler 调用（仅规则夹具字符串字面量）；实测见盘点节",
        "有界",
        "无需修复",
    ),
    (
        "audit/detect/engine.py ProcessPoolExecutor（W7 并行扫描）",
        "mini_app 5 文件 < PARALLEL_SCAN_MIN_FILES(100) 且默认 rule_scan_workers=1 → 本诊断走串行路径",
        "本路径未触发",
        "大库（≥100 文件）+ 并行开启时才有进程池；executor.shutdown(wait=True) 已在 finally 收口",
    ),
)


def _render_report(
    rounds: int,
    summaries: list[dict[str, Any]],
    per_round_files: list[dict[str, float]],
    inventory_lines: list[str],
    elapsed_sec: float,
    quick_note: str,
) -> str:
    """渲染 markdown 诊断报告。"""
    lines: list[str] = []
    valid_rss = [(s["round"], s["rss"]) for s in summaries if s["rss"] is not None]
    xs = [float(r) for r, _ in valid_rss]
    ys = [float(v) for _, v in valid_rss]
    slope_all = _slope(xs, ys)
    corr = _pearson(xs, ys)
    steady_x = [x for x in xs if x >= STEADY_FROM_ROUND]
    steady_y = [y for x, y in zip(xs, ys, strict=True) if x >= STEADY_FROM_ROUND]
    slope_steady = _slope(steady_x, steady_y)
    mean_dur = sum(s["duration"] for s in summaries) / max(1, len(summaries))

    lines.append("# W11-A4 库层内存诊断报告（memdiag）")
    lines.append("")
    lines.append("## 环境头")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"- Python：{sys.version.split()[0]}；平台：{sys.platform}")
    lines.append(f"- 轮数：{rounds}{quick_note}")
    lines.append(f"- 审计素材：{_rel(MINI_APP)}（{sum(1 for p in MINI_APP.rglob('*') if p.is_file())} 个文件，固定审计素材）")
    lines.append("- 调用方式：库层直调 `asyncio.run(run_audit(config, noop_emitter))`，不经 HTTP（HTTP 层 RSS 已由 soak 覆盖）")
    lines.append("- 每轮独立 work_root（临时目录，模拟服务端逐任务隔离，结束后已清理）；enable_llm_review=False（纯规则模式）")
    lines.append("- 离线消毒：启动前已 pop GLM_API_KEY / GLM_BASE_URL / GLM_MODEL")
    lines.append("- RSS 口径：Windows psapi.GetProcessMemoryInfo 当前进程工作集（与 soak 同口径）")
    lines.append("- tracemalloc 口径：第 2 轮起轮前/轮后各 take_snapshot（depth=25），轮前 gc.collect()；diff=compare_to('lineno')")
    lines.append("")

    lines.append("## 每轮 RSS / tracemalloc 采样")
    lines.append("")
    lines.append("| 轮 | 耗时(s) | RSS(MB) | ΔRSS(MB) | 峰值WS(MB) | traced当前(MB) | traced峰值(MB) | issues | 轮内 top1 增长行 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    prev_rss: float | None = None
    for s in summaries:
        rss_txt = f"{s['rss']:.1f}" if s["rss"] is not None else "-"
        peak_txt = f"{s['peak']:.1f}" if s["peak"] is not None else "-"
        d_rss = "-" if s["rss"] is None or prev_rss is None else f"{s['rss'] - prev_rss:+.2f}"
        cur = "-" if s["traced_cur"] is None else f"{s['traced_cur']:.2f}"
        peak = "-" if s["traced_peak"] is None else f"{s['traced_peak']:.2f}"
        lines.append(
            f"| {s['round']} | {s['duration']:.2f} | {rss_txt} | {d_rss} | {peak_txt} "
            f"| {cur} | {peak} | {s['issues']} | {s['top1']} |"
        )
        if s["rss"] is not None:
            prev_rss = s["rss"]
    lines.append("")

    lines.append("## RSS 斜率与相关性")
    lines.append("")
    if slope_all is not None:
        lines.append(f"- 全轮 RSS 斜率：{slope_all:+.3f} MB/轮（折算 ≈ {slope_all / mean_dur * 60:+.3f} MB/min，平均轮时长 {mean_dur:.2f}s）")
    else:
        lines.append("- 全轮 RSS 斜率：不可得（无有效 RSS 样本）")
    if slope_steady is not None:
        lines.append(
            f"- 稳态（第 {STEADY_FROM_ROUND} 轮起，{len(steady_x)} 轮）RSS 斜率：{slope_steady:+.3f} MB/轮"
            f"（折算 ≈ {slope_steady / mean_dur * 60:+.3f} MB/min）"
        )
    if corr is not None:
        lines.append(f"- RSS 与轮次 Pearson r：{corr:.2f}（soak DEFER 阈值 0.6）")
    if valid_rss:
        lines.append(
            f"- RSS 总变化：{valid_rss[0][1]:.1f} → {valid_rss[-1][1]:.1f} MB"
            f"（Δ {valid_rss[-1][1] - valid_rss[0][1]:+.1f} MB，全程 {elapsed_sec:.0f}s）；"
            "第 1 轮含懒加载导入预热，解读以稳态段为准"
        )
    lines.append("")

    lines.append("## tracemalloc 文件级跨轮累计聚合（top 15）")
    lines.append("")
    lines.append("| 文件 | 累计净增(KB) | 轮均(KB) | 前3轮占比 | 后5轮轮均(KB) | 判定 |")
    lines.append("|---|---|---|---|---|---|")
    agg = _aggregate_file_growth(per_round_files)
    if not agg:
        lines.append("| （无累计净增 >1KB 的文件） | - | - | - | - | - |")
    for name, total, mean, share, steady_mean, verdict in agg:
        lines.append(f"| {name} | {total:+.1f} | {mean:+.2f} | {share:.0%} | {steady_mean:+.3f} | {verdict} |")
    lines.append("")
    lines.append("判定规则：稳态（后 5 轮）轮均净增 >0.2 KB/轮 →「线性增长」（真滞留信号）；"
                 "前 3 轮占累计 ≥80% 且稳态归零 →「预热集中」（缓存/预热，有界）；其余为「平台期波动」。")
    lines.append("")

    lines.append("## 嫌疑清单核查")
    lines.append("")
    lines.append("| 嫌疑点 | 证据 | 有界性判定 | 建议 |")
    lines.append("|---|---|---|---|")
    for point, evidence, verdict, suggestion in _SUSPECT_ROWS:
        lines.append(f"| {point} | {evidence} | {verdict} | {suggestion} |")
    lines.append("")

    lines.append("## 运行末残留盘点（全部轮次结束后）")
    lines.append("")
    lines.extend(inventory_lines)
    lines.append("")

    lines.append("## 结论与修复建议")
    lines.append("")
    linear_files = [r for r in agg if r[5] == "线性增长"]
    if linear_files:
        lines.append(f"- 检出 Python 对象级线性滞留文件 {len(linear_files)} 个（见聚合表「线性增长」行），"
                     "为每任务缓爬的直接嫌疑，建议按文件逐点复核引用链后修复。")
    else:
        lines.append("- tracemalloc 维度：稳态段无「线性增长」文件——未检出 Python 对象层的每任务滞留（无单点泄漏）。")
    if slope_steady is not None and abs(slope_steady) < 0.05:
        lines.append(f"- RSS 维度：稳态斜率 {slope_steady:+.3f} MB/轮 ≈ 0——库层（纯规则模式）RSS 无每任务常数缓爬。")
    elif slope_steady is not None:
        lines.append(f"- RSS 维度：稳态斜率 {slope_steady:+.3f} MB/轮，与 tracemalloc 结论对照解读"
                     "（若 tracemalloc 无线性滞留，则残差来自分配器高水位 / C 扩展内存，非 Python 对象泄漏）。")
    lines.append("- soak 每任务 ~0.15-0.27 MB 缓爬若在本诊断（纯规则库层）不复现，则增量大概率来自 HTTP 层/"
                 "服务端任务簿记（taskstore 历史表、SSE 通道、报告文件句柄缓存等），归集成人在 server/ 侧复核。")
    lines.append("- 修复建议汇总见「嫌疑清单核查」表「建议」列；本任务只诊断不修（audit/* 所有权约束）。")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程
def main(argv: list[str] | None = None) -> int:
    """主流程：消毒环境 → 逐轮审计+采样 → 聚合归因 → 写报告 → 清理。"""
    parser = argparse.ArgumentParser(description="W11-A4 库层内存诊断（只诊断不修）")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS, help="审计轮数（默认 30）")
    parser.add_argument("--out", type=str, default=str(DEFAULT_OUT), help="报告输出路径（markdown）")
    parser.add_argument("--quick", action="store_true", help="冒烟口径：轮数固定 10")
    args = parser.parse_args(argv)

    # 全程离线：清除 GLM_*（与 soak / adversarial 同约定；本诊断走 FakeLLM 路径本就不读，双保险）
    for key in ("GLM_API_KEY", "GLM_BASE_URL", "GLM_MODEL"):
        os.environ.pop(key, None)

    rounds = QUICK_ROUNDS if args.quick else args.rounds
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = PROJECT_ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    quick_note = "（--quick 冒烟口径）" if args.quick else ""

    print(f"[memdiag] 素材：{MINI_APP}；轮数：{rounds}{quick_note}；报告：{out_path}")
    tmp_root = Path(tempfile.mkdtemp(prefix="memdiag_w11_"))
    summaries: list[dict[str, Any]] = []
    per_round_files: list[dict[str, float]] = []
    t_start = time.perf_counter()
    try:
        for round_no in range(1, rounds + 1):
            summary, _top, file_net = _run_round(round_no, tmp_root)
            summaries.append(summary)
            if round_no >= 2:
                per_round_files.append(file_net)
            rss_txt = f"{summary['rss']:.1f}" if summary["rss"] is not None else "-"
            print(
                f"[memdiag] 轮 {round_no}/{rounds} done={summary['duration']:.2f}s "
                f"rss={rss_txt}MB issues={summary['issues']} top1={summary['top1']}"
            )
        elapsed = time.perf_counter() - t_start
        inventory = _inventory()
        report_md = _render_report(rounds, summaries, per_round_files, inventory, elapsed, quick_note)
        out_path.write_text(report_md, encoding="utf-8")
        print(f"[memdiag] 报告已写入：{out_path}")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        print(f"[memdiag] 临时工作区已清理：{tmp_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
