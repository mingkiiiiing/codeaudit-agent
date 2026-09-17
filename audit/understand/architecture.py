"""Stage3 架构理解（understand）：启发式底座 + LLM 增强的 map-reduce 架构卡片。"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from typing import Any

from audit.models import ArchitectureCard
from audit.pipeline import PipelineContext
from audit.utils import extract_json, guess_language

__all__ = ["build_architecture", "find_entry_points"]

# W15 集成修复（审计 A7）：本模块原有多处 except 后静默 continue/return，故障完全
# 不可见（卡F 测试已固化降级行为）。统一补记账：信息收集类降级记 debug（高频、
# 单条无碍全局），LLM 增强失败记 warning（用户可感知的能力降级）。
_LOG = logging.getLogger(__name__)

# 常见入口文件名（根目录或一级子目录优先）
_ENTRY_NAMES = frozenset(
    [
        "main.py", "app.py", "manage.py", "run.py", "server.py", "wsgi.py",
        "asgi.py", "cli.py", "index.js", "index.ts", "main.js", "main.ts", "app.js",
    ]
)

# 依赖文件内容 -> 技术栈关键词
_STACK_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("fastapi", ("fastapi",)),
    ("flask", ("flask",)),
    ("django", ("django",)),
    ("tornado", ("tornado",)),
    ("sqlalchemy", ("sqlalchemy",)),
    ("pydantic", ("pydantic",)),
    ("celery", ("celery",)),
    ("redis", ("redis",)),
    ("react", ("react",)),
    ("vue", ("vue",)),
    ("express", ("express",)),
    ("next.js", ("next",)),
    ("webpack", ("webpack",)),
]

_DEF_CLASS_RE = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)|^\s*class\s+(\w+)")

_LLM_INSTRUCTION = (
    "你是资深软件架构分析师。下面给出对一个代码库的静态启发式分析结果（JSON）。"
    "请校正并补充架构判断，只输出一个 JSON 对象，字段："
    '{"tech_stack": ["技术栈"], "modules": {"目录": "职责摘要"}, '
    '"hotspots": ["风险热点文件或符号"], "summary": "200 字以内的项目架构摘要"}。'
    "不确定的字段给空值即可，不要编造。"
)


# ---------------------------------------------------------------- 信息收集


def _collect_files(ctx: PipelineContext) -> list[tuple[str, str, int]]:
    """收集 (相对路径, 语言, 行数) 列表；单文件失败跳过。"""
    files: list[tuple[str, str, int]] = []
    for path in ctx.workspace.source_files(ctx.config.languages or None):
        try:
            rel = ctx.workspace.rel(path)
            lang = guess_language(rel) or ""
            loc = ctx.workspace.line_count(rel)
        except Exception as exc:
            _LOG.debug("架构理解：文件元信息收集失败，跳过 %s：%s", path, exc)
            continue
        if not lang:
            continue
        files.append((rel, lang, loc))
    return files


def _dependency_blob(ctx: PipelineContext) -> str:
    """汇总常见依赖/清单文件的小写文本（用于技术栈关键词猜测）。"""
    parts: list[str] = []
    for name in ("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "package.json"):
        p = ctx.workspace.abs_path(name)
        if not p.is_file():
            continue
        try:
            parts.append(p.read_text(encoding="utf-8", errors="replace").lower())
        except Exception as exc:
            _LOG.debug("架构理解：依赖清单读取失败，跳过 %s：%s", name, exc)
            continue
    return "\n".join(parts)


def _guess_tech_stack(ctx: PipelineContext, files: list[tuple[str, str, int]]) -> list[str]:
    """技术栈猜测：语言构成 + 依赖文件内容关键词（如 flask/fastapi/react）。"""
    langs = {lang for _, lang, _ in files}
    stack: list[str] = []
    for lang in ("python", "typescript", "javascript"):
        if lang in langs:
            stack.append(lang)
    blob = _dependency_blob(ctx)
    if "package.json" in blob or '"dependencies"' in blob:
        if "node.js" not in stack:
            stack.append("node.js")
    for name, keys in _STACK_KEYWORDS:
        if name in stack:
            continue
        if any(k in blob for k in keys):
            stack.append(name)
    return stack


def _symbol_names(ctx: PipelineContext, rel_paths: list[str]) -> list[str]:
    """模块主要符号名：优先取索引符号表，索引不可用时回退 def/class 正则扫描。"""
    names: list[str] = []
    index = ctx.index
    if index is not None:
        for rel in rel_paths:
            try:
                symbols = index.symbols_for_file(rel)
            except Exception as exc:
                _LOG.debug("架构理解：索引符号查询失败 %s：%s", rel, exc)
                symbols = []
            for s in symbols:
                if getattr(s, "kind", "") in ("function", "class", "method") and s.name not in names:
                    names.append(s.name)
        if names:
            return names
    for rel in rel_paths:
        if not rel.endswith((".py", ".js", ".ts", ".tsx", ".jsx")):
            continue
        try:
            text = ctx.workspace.read_file_text(rel)
        except Exception as exc:
            _LOG.debug("架构理解：符号回退扫描读文件失败，跳过 %s：%s", rel, exc)
            continue
        for ln in text.splitlines()[:300]:
            m = _DEF_CLASS_RE.match(ln)
            if m:
                name = m.group(1) or m.group(2)
                if name and name not in names:
                    names.append(name)
    return names


def _describe_modules(ctx: PipelineContext, files: list[tuple[str, str, int]]) -> dict[str, str]:
    """顶层目录职责表：含源码的一级/二级目录 -> 文件数/行数/主要符号摘要。"""
    groups: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for rel, lang, loc in files:
        parts = rel.split("/")
        if len(parts) == 1:
            groups["(根目录)"].append((rel, lang, loc))
        else:
            for depth in range(1, min(2, len(parts) - 1) + 1):
                key = "/".join(parts[:depth])
                groups[key].append((rel, lang, loc))
    out: dict[str, str] = {}
    for mod in sorted(groups):
        items = groups[mod]
        loc = sum(loc for _, _, loc in items)
        names = _symbol_names(ctx, [rel for rel, _, _ in items])
        summary = "、".join(names[:6]) or "无显式符号"
        out[mod] = f"{len(items)} 个文件 / {loc} 行；主要符号：{summary}"
    return out


def _hotspots(files: list[tuple[str, str, int]], top: int = 5) -> list[str]:
    """规模热点：按行数取 Top N。"""
    ranked = sorted(files, key=lambda t: t[2], reverse=True)
    return [f"{rel} ({loc} 行)" for rel, _, loc in ranked[:top] if loc > 0]


def _render_text(
    files: list[tuple[str, str, int]],
    stack: list[str],
    modules: dict[str, str],
    entries: list[str],
    hotspots: list[str],
) -> str:
    total_loc = sum(loc for _, _, loc in files)
    lang_loc: dict[str, int] = defaultdict(int)
    for _, lang, loc in files:
        lang_loc[lang] += loc
    dist = "、".join(
        f"{lang} {round(loc * 100 / max(1, total_loc))}%"
        for lang, loc in sorted(lang_loc.items(), key=lambda kv: -kv[1])
    )
    lines = [
        f"项目共 {len(files)} 个源文件、{total_loc} 行代码；语言分布：{dist or '未知'}。",
        f"技术栈推测：{'、'.join(stack) or '未知'}。",
        f"入口文件：{'、'.join(entries) or '未识别'}。",
        "模块职责：",
    ]
    lines += [f"  - {k}: {v}" for k, v in modules.items()]
    lines.append("规模热点（按行数 Top）：")
    lines += [f"  - {h}" for h in hotspots]
    return "\n".join(lines)


# ---------------------------------------------------------------- 对外入口


def find_entry_points(ctx: PipelineContext, files: list[tuple[str, str, int]] | None = None) -> list[str]:
    """入口猜测：根目录/一级子目录下的常见入口文件名（main.py/app.py/manage.py/index.ts 等）。"""
    if files is None:
        files = _collect_files(ctx)
    entries: list[str] = []
    for rel, _lang, _loc in files:
        name = rel.rsplit("/", 1)[-1]
        if rel.count("/") <= 1 and name in _ENTRY_NAMES:
            entries.append(rel)
    return sorted(set(entries), key=lambda r: (r.count("/"), r))


def _heuristic_card(ctx: PipelineContext) -> tuple[ArchitectureCard, list[str], list[tuple[str, str, int]]]:
    """纯启发式架构卡片：不依赖 LLM，任何项目都能产出。"""
    files = _collect_files(ctx)
    stack = _guess_tech_stack(ctx, files)
    entries = find_entry_points(ctx, files)
    modules = _describe_modules(ctx, files)
    hotspots = _hotspots(files)
    card = ArchitectureCard(
        text=_render_text(files, stack, modules, entries, hotspots),
        tech_stack=stack,
        modules=modules,
        hotspots=hotspots,
    )
    return card, entries, files


async def _llm_enhance(ctx: PipelineContext, base: ArchitectureCard) -> ArchitectureCard:
    """LLM 增强层：校正技术栈/模块职责/热点并给出摘要。

    任何异常（脚本耗尽返回空、JSON 解析失败、网络故障）都安全降级为启发式结果。
    """
    payload = {
        "tech_stack": base.tech_stack,
        "modules": base.modules,
        "hotspots": base.hotspots,
        "entry_points_hint": [],
    }
    static_json = json.dumps(payload, ensure_ascii=False)
    messages = [
        {"role": "system", "content": _LLM_INSTRUCTION},
        {"role": "user", "content": f"[static_analysis]\n{static_json}"},
    ]
    try:
        resp = await ctx.llm.chat(messages, json_mode=True)
        data = extract_json(resp.content)
    except Exception as exc:
        # W15：LLM 增强失败降级为启发式底座（既有语义），但必须可见——此前完全静默
        _LOG.warning("架构理解：LLM 增强失败，降级为启发式结果：%s", exc)
        return base
    if not isinstance(data, dict):
        return base

    card = ArchitectureCard(
        text=base.text,
        tech_stack=list(base.tech_stack),
        modules=dict(base.modules),
        hotspots=list(base.hotspots),
    )
    tech = data.get("tech_stack")
    if isinstance(tech, list):
        for item in tech:
            if isinstance(item, str) and item.strip() and item.strip() not in card.tech_stack:
                card.tech_stack.append(item.strip())
    mods = data.get("modules")
    if isinstance(mods, dict):
        for k, v in mods.items():
            if isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip():
                card.modules[k] = v.strip()
    hotspots = data.get("hotspots")
    if isinstance(hotspots, list):
        cleaned = [h.strip() for h in hotspots if isinstance(h, str) and h.strip()]
        if cleaned:
            card.hotspots = cleaned
    summary = data.get("summary")
    if isinstance(summary, str) and summary.strip():
        card.text = summary.strip()
    return card


async def build_architecture(ctx: PipelineContext) -> ArchitectureCard:
    """Stage3 入口：先建启发式底座（必做、零成本），enable_llm_review 时叠加 LLM 增强。

    结果写入 ctx.architecture，入口清单另存 ctx.extra["entry_points"]。
    本函数绝不抛异常：LLM 任何失败都降级为启发式结果。
    """
    card, entries, files = _heuristic_card(ctx)
    ctx.extra["entry_points"] = entries
    if ctx.config.enable_llm_review:
        card = await _llm_enhance(ctx, card)
    ctx.architecture = card
    try:
        await ctx.emit("understand", "架构理解完成", current=len(files), total=max(1, len(files)))
    except Exception:
        pass  # 进度事件尽力而为
    return card
