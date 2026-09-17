"""确定性重构建议（W7-A2 启发式层，零 LLM）。

输入全部来自流水线既有产物（docs/12 §4-2）：ctx.issues（规则命中转换的最终
问题清单）、ctx.workspace（工作副本/manifests 行数）、ctx.index（符号切片与
依赖图）。产出结构化 RefactorProposal（source="heuristic"，confidence 按证据
强度 0.5~0.8），供报告「重构方案」章节与可选的 LLM 增强层消费。

四类启发式：
1) 长函数分解：LONG-FUNCTION（severity=medium）命中 → 按函数内空行/注释块/
   调用聚类给出 2~4 个候选子职责的拆分步骤（kind="decompose"）；
2) 重复模式归并：同类规则同文件 ≥3 处命中 → 统一工具函数方案（kind="dedup"）；
3) 热点模块拆分：行数达标且被依赖最多的模块 → 拆分模块方案（kind="split-module"）；
4) 依赖循环提示：index 依赖图 DFS 找环，找到才输出（kind="other"）。
"""

from __future__ import annotations

import re
from typing import Any

from audit.models import Issue, RefactorProposal, Severity
from audit.pipeline import PipelineContext
from audit.utils import guess_language, make_id

__all__ = ["generate_proposals"]

# ---------------------------------------------------------------- 阈值（证据强度）

_DEDUP_MIN_HITS = 3  # 同类规则同文件多命中阈值
_DEDUP_MAX_PROPOSALS = 3  # 归并方案上限（按命中数取 top）
_LONG_FUNCTION_RULE_IDS = {"PY-LONG-FUNCTION", "JS-LONG-FUNCTION"}
_LONG_FUNCTION_MAX = 5  # 分解方案上限（按函数长度取 top）
_LONG_FUNCTION_MIN_LINES = 80  # 与规则上限一致，仅用于文案
_HOTSPOT_MIN_LOC = 200  # 热点模块最小行数
_HOTSPOT_MIN_IMPORTERS = 3  # 热点模块最少被依赖数
_MAX_CYCLES_SHOWN = 3  # 循环依赖方案最多列出的环数

# ---------------------------------------------------------------- 重复模式配方

# rule_id -> (方案标题短语, 统一抽象建议)
_DEDUP_RECIPES: dict[str, tuple[str, str]] = {
    "PY-SQL-INJECTION": ("抽取统一查询构造器", "引入统一的 SQL 收口（查询构造器或参数化辅助函数），所有语句经其生成，杜绝字符串拼接"),
    "JS-SQL-CONCAT": ("抽取统一查询构造器", "引入统一的 SQL 收口（查询构造器或参数化辅助函数），所有语句经其生成，杜绝字符串拼接"),
    "PY-BARE-EXCEPT": ("统一异常处理", "引入统一的异常处理装饰器或上下文管理器，集中记录日志并收敛吞异常语义"),
    "PY-EXCEPT-PASS": ("统一异常处理", "引入统一的异常处理装饰器或上下文管理器，集中记录日志并显式声明可忽略的异常类型"),
    "PY-EMPTY-EXCEPT": ("统一异常处理", "引入统一的异常处理装饰器或上下文管理器，集中记录日志并显式声明可忽略的异常类型"),
    "JS-EMPTY-CATCH": ("统一异常处理", "引入统一的错误处理封装（包装函数或中间件），集中记录并收敛空 catch 语义"),
    "PY-NO-TIMEOUT": ("统一网络访问封装", "封装带默认 timeout 的请求函数（或统一 HTTP 客户端），全部远程调用走该入口"),
    "JS-FETCH-NO-TIMEOUT": ("统一网络访问封装", "封装带默认 timeout 的请求函数（或统一 fetch 包装），全部远程调用走该入口"),
    "PY-OPEN-NO-CLOSE": ("统一文件读写工具", "以 contextmanager 封装文件打开/关闭，统一生命周期与异常路径下的资源释放"),
    "PY-OPEN-WITHOUT-ENCODING": ("统一文件读写工具", "以 contextmanager 封装文件读写并集中声明编码约定"),
    "PY-PRINT-DEBUG": ("统一日志门面", "引入统一日志封装（级别/格式/开关），替换散落的打印调试语句"),
    "JS-CONSOLE-LOG": ("统一日志门面", "引入统一日志封装（级别/格式/开关），替换散落的 console 调试语句"),
    "PY-HARDCODED-SECRET": ("集中密钥与配置管理", "建立统一配置模块，密钥改由环境变量或配置文件注入，代码中只保留引用"),
    "JS-HARDCODED-SECRET": ("集中密钥与配置管理", "建立统一配置模块，密钥改由环境变量或配置文件注入，代码中只保留引用"),
    "PY-MAGIC-NUMBER": ("提取具名常量", "将重复出现的魔法数字提取为模块级具名常量并集中管理"),
    "JS-MAGIC-NUMBER": ("提取具名常量", "将重复出现的魔法数字提取为具名常量并集中管理"),
    "PY-MUTABLE-DEFAULT": ("统一默认值规范", "以 None 哨兵 + 函数体内构造替代可变默认参数，可封装为装饰器统一处理"),
    "PY-EQ-NONE": ("统一空值判断规范", "以 is None / is not None 统一空值判断，可借助代码评审清单或 lint 门禁保持一致"),
    "JS-DOUBLE-EQ-NULL": ("统一空值判断规范", "以 === null / == null 约定统一空值判断，可借助 lint 规则保持一致"),
}
_DEDUP_GENERIC = ("统一工具函数收口", "为该模式设计单一工具函数/装饰器，各命中点改为调用统一实现")

# ---------------------------------------------------------------- 工具函数


def _rule_id_of(issue: Issue) -> str:
    """从 Issue.evidence 中的 "rule:XXX" 恢复规则 id（引擎 hits_to_issues 写入）。"""
    for ev in issue.evidence or []:
        if isinstance(ev, str) and ev.startswith("rule:"):
            return ev[len("rule:") :]
    return ""


def _sev_value(issue: Issue) -> str:
    return issue.severity.value if isinstance(issue.severity, Severity) else str(issue.severity)


def _location_stats(locations: list[tuple[str, int]]) -> str:
    """把 [(file, line)] 命中位置压缩为文案（最多列 5 处）。"""
    shown = [f"{f}:{ln}" for f, ln in locations[:5]]
    more = len(locations) - len(shown)
    return "、".join(shown) + (f" 等 {len(locations)} 处" if more > 0 else "")


# ---------------------------------------------------------------- 1) 长函数分解

_TITLE_SYMBOL_RE = re.compile(r"函数\s*`(.+?)`")
_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)")
_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "print", "def", "function",
    "elif", "else", "try", "with", "throw", "new", "typeof", "in", "of", "await",
}


def _function_symbol(issue: Issue) -> str:
    m = _TITLE_SYMBOL_RE.search(issue.title or "")
    return m.group(1) if m else ""


def _load_function_code(ctx: PipelineContext, issue: Issue, symbol: str) -> str:
    """优先取索引切片（带准确符号名），失败回退按行区间读工作副本。"""
    index = ctx.index
    slices_fn = getattr(index, "slices_for_file", None) if index is not None else None
    if callable(slices_fn):
        try:
            for s in slices_fn(issue.file) or []:
                if s.line_start <= issue.line_start and s.line_end >= issue.line_end:
                    if symbol and s.symbol.split(".")[-1] != symbol:
                        continue  # 同文件同名函数多定义时按符号名消歧
                    return s.code or ""
        except Exception:  # noqa: BLE001 —— 切片失败回退直接读文件
            pass
    try:
        return "\n".join(ctx.workspace.read_lines(issue.file, issue.line_start, issue.line_end))
    except OSError:
        return ""


def _body_start(lines: list[str]) -> int:
    """返回函数体（跳过签名行与前导 docstring 块）在原行列表中的起始下标。"""
    idx = 0
    n = len(lines)
    while idx < n and not lines[idx].strip():
        idx += 1
    first = lines[idx].strip() if idx < n else ""
    if first.startswith(("def ", "async def ", "function ")):  # 函数签名行
        idx += 1
    while idx < n and not lines[idx].strip():
        idx += 1
    if idx < n:
        stripped = lines[idx].strip()
        if stripped.startswith(('"""', "'''")):  # docstring 块
            quote = stripped[:3]
            if stripped.count(quote) >= 2:  # 单行 docstring
                return idx + 1
            for j in range(idx + 1, n):
                if quote in lines[j]:
                    return j + 1
            return n
    return idx


def _split_blocks(code: str, base_line: int) -> list[tuple[int, int, list[str]]]:
    """按空行/注释行把函数体切段，返回 [(起始行号, 结束行号, 行列表)]（行号为文件绝对行号）。"""
    lines = code.splitlines()
    start = _body_start(lines)
    blocks: list[tuple[int, int, list[str]]] = []
    cur: list[str] = []
    cur_start = 0
    for idx in range(start, len(lines)):
        stripped = lines[idx].strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            if cur:
                blocks.append((base_line + cur_start, base_line + idx - 1, cur))
                cur = []
            continue
        if not cur:
            cur_start = idx
        cur.append(lines[idx])
    if cur:
        blocks.append((base_line + cur_start, base_line + len(lines) - 1, cur))
    # 过滤不足 2 行的碎块（语义太弱，避免噪声步骤）
    blocks = [b for b in blocks if len(b[2]) >= 2]
    # 超过 4 块时保留最大的 4 块（保持原顺序）
    if len(blocks) > 4:
        keep = sorted(range(len(blocks)), key=lambda i: -len(blocks[i][2]))[:4]
        blocks = [blocks[i] for i in sorted(keep)]
    return blocks


def _block_label(lines: list[str]) -> str:
    """从代码块提取职责线索（调用名/赋值目标，最多 2 个）。"""
    hints: list[str] = []
    for ln in lines:
        for m in _CALL_RE.finditer(ln):
            name = m.group(1)
            if name not in _KEYWORDS and name not in hints:
                hints.append(name)
    for ln in lines:
        if len(hints) >= 2:
            break
        m = _ASSIGN_RE.match(ln)
        if m and m.group(1) not in hints:
            hints.append(m.group(1))
    return "、".join(hints[:2])


def _even_split(code: str, base_line: int, parts: int = 3) -> list[tuple[int, int, list[str]]]:
    """无空行/注释可依据时的兜底：把函数体等分为 parts 段。"""
    lines = code.splitlines()
    start = _body_start(lines)
    body = lines[start:]
    n = len(body)
    if n < 6:
        return []
    size = n // parts
    blocks = []
    for i in range(parts):
        a = i * size
        b_ = (i + 1) * size if i < parts - 1 else n
        if b_ > a:
            blocks.append((base_line + start + a, base_line + start + b_ - 1, body[a:b_]))
    return blocks


def _decompose_proposals(ctx: PipelineContext, issues: list[Issue]) -> list[RefactorProposal]:
    """长函数分解方案：每个超长函数一条（按函数长度取 top N）。"""
    long_issues = [
        i
        for i in issues
        if _rule_id_of(i) in _LONG_FUNCTION_RULE_IDS
        and _sev_value(i) == "medium"
        and i.line_end > i.line_start
    ]
    long_issues.sort(key=lambda i: -(i.line_end - i.line_start))
    proposals: list[RefactorProposal] = []
    for issue in long_issues[:_LONG_FUNCTION_MAX]:
        length = issue.line_end - issue.line_start + 1
        symbol = _function_symbol(issue) or f"{issue.file}:{issue.line_start}"
        code = _load_function_code(ctx, issue, _function_symbol(issue))
        blocks = _split_blocks(code, issue.line_start) or _even_split(code, issue.line_start)
        steps: list[str] = []
        for idx, (a, b, lines) in enumerate(blocks, 1):
            label = _block_label(lines)
            cand = f"（候选子职责：{label}）" if label else "（候选子职责：结合上下文命名）"
            steps.append(f"将第 {a}~{b} 行抽为独立子函数 {cand}")
        if not steps:
            # 源码不可读（工作副本缺失等）：仍给出可执行的通用拆分框架，保证 steps 非空
            steps.append("以函数内空行/注释块/调用聚类为界，将函数体切分为 2~4 个子职责（如输入校验/核心计算/结果输出）")
        steps.append("原函数保留为编排入口，按序调用各子函数；为每个子函数补充单测（可复用 --tests 能力）")
        proposals.append(
            RefactorProposal(
                title=f"分解长函数 {symbol}",
                target=f"{issue.file}::{symbol}",
                kind="decompose",
                rationale=(
                    f"函数 `{symbol}`（{issue.file}:{issue.line_start}~{issue.line_end}，共 {length} 行）"
                    f"超过 {_LONG_FUNCTION_MIN_LINES} 行上限，职责混杂、圈复杂度高；"
                    f"依据函数内空行/调用聚类可切分为约 {max(len(steps) - 1, 2)} 个子职责。"
                ),
                steps=steps,
                benefits="降低单函数复杂度与测试成本，子职责可独立复用，改动影响面更小。",
                related_issues=[issue.id] if issue.id else [],
                source="heuristic",
                confidence=round(0.6 + min(0.15, max(0.0, (length - _LONG_FUNCTION_MIN_LINES) / 400.0)), 2),
            )
        )
    return proposals


# ---------------------------------------------------------------- 2) 重复模式归并


def _dedup_proposals(issues: list[Issue]) -> list[RefactorProposal]:
    """同类规则同文件 ≥3 处命中 → 统一工具函数方案（按命中数取 top）。"""
    groups: dict[tuple[str, str], list[Issue]] = {}
    for issue in issues:
        rule_id = _rule_id_of(issue)
        if rule_id and issue.file:
            groups.setdefault((rule_id, issue.file), []).append(issue)
    candidates = [(k, v) for k, v in groups.items() if len(v) >= _DEDUP_MIN_HITS]
    candidates.sort(key=lambda kv: -len(kv[1]))
    proposals: list[RefactorProposal] = []
    for (rule_id, file), group in candidates[:_DEDUP_MAX_PROPOSALS]:
        title_phrase, abstraction = _DEDUP_RECIPES.get(rule_id, _DEDUP_GENERIC)
        locations = [(i.file, i.line_start) for i in group]
        issue_ids = [i.id for i in group if i.id]
        proposals.append(
            RefactorProposal(
                title=f"{title_phrase}：归并 {file} 中 {len(group)} 处 {rule_id} 命中",
                target=file,
                kind="dedup",
                rationale=(
                    f"{file} 中同一规则 {rule_id} 命中 {len(group)} 处（{_location_stats(locations)}）："
                    f"同一坏味道反复出现说明缺少统一抽象；{abstraction}。"
                ),
                steps=[
                    f"盘点命中位置并确认语义等价：{_location_stats(locations)}",
                    abstraction,
                    "逐处替换为统一实现，保持对外行为等价（逐处替换逐处验证）",
                    "运行现有测试回归（或以 --tests 为核心路径补充回归用例）",
                ],
                benefits=f"消除 {len(group)} 处重复坏味道，后续修改只改一处，降低漂移与漏改风险。",
                related_issues=issue_ids,
                source="heuristic",
                confidence=round(min(0.8, 0.5 + 0.05 * len(group)), 2),
            )
        )
    return proposals


# ---------------------------------------------------------------- 3) 热点模块拆分


def _file_locs(ctx: PipelineContext) -> dict[str, int]:
    """manifests → {相对路径: 行数}（仅已知语言的源文件）。"""
    locs: dict[str, int] = {}
    for m in ctx.workspace.manifests or []:
        rel = str(getattr(m, "path", "") or "")
        if rel and guess_language(rel):
            locs[rel] = int(getattr(m, "loc", 0) or 0)
    return locs


def _hotspot_proposal(ctx: PipelineContext) -> RefactorProposal | None:
    """行数 top 且被依赖最多的模块 → 拆分模块方案（取第一名）。"""
    index = ctx.index
    deps_fn = getattr(index, "dependencies", None) if index is not None else None
    if not callable(deps_fn):
        return None
    scored: list[tuple[str, int, list[str]]] = []
    for rel, loc in _file_locs(ctx).items():
        if loc < _HOTSPOT_MIN_LOC:
            continue
        try:
            importers = [str(x) for x in (deps_fn(rel, "imported_by") or [])]
        except Exception:  # noqa: BLE001 —— 单文件依赖查询失败不阻断
            continue
        if len(importers) >= _HOTSPOT_MIN_IMPORTERS:
            scored.append((rel, loc, importers))
    if not scored:
        return None
    scored.sort(key=lambda t: (-len(t[2]), -t[1]))
    rel, loc, importers = scored[0]
    mod_name = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    shown_importers = "、".join(importers[:5]) + (" 等" if len(importers) > 5 else "")
    return RefactorProposal(
        title=f"拆分热点模块 {rel}",
        target=rel,
        kind="split-module",
        rationale=(
            f"{rel} 共 {loc} 行，且被 {len(importers)} 个模块依赖（{shown_importers}），"
            "是典型的「高耦合 + 大体积」热点：任何改动都会波及大量引用方，应按职责拆分为多个内聚子模块。"
        ),
        steps=[
            f"梳理 {rel} 内的职责分组（可按函数/类对引用方的使用场景聚类）",
            f"将各职责拆为 {mod_name}_<职责>.py 等子模块，公共入口保持 re-export 以兼容现有引用方",
            f"迁移完成后逐个更新引用方导入（共 {len(importers)} 个：{shown_importers}）",
            "运行现有测试回归，确认无导入破坏后再删除兼容 re-export",
        ],
        benefits="降低耦合扇入带来的改动放大效应，使热点模块可独立演进与测试。",
        related_issues=[],
        source="heuristic",
        confidence=round(min(0.8, 0.5 + 0.05 * len(importers)), 2),
    )


# ---------------------------------------------------------------- 4) 依赖循环提示


def _import_graph(deps_fn: Any, files: list[str]) -> dict[str, set[str]]:
    """构造项目内文件级导入图（仅保留指向项目内文件的边）。"""
    fset = set(files)
    graph: dict[str, set[str]] = {}
    for f in files:
        try:
            targets = deps_fn(f, "imports") or []
        except Exception:  # noqa: BLE001 —— 单文件依赖查询失败不阻断
            targets = []
        graph[f] = {str(t) for t in targets if t in fset}
    return graph


def _find_cycles(graph: dict[str, set[str]], limit: int) -> list[list[str]]:
    """迭代三色 DFS 找环（大项目避免递归爆栈），返回至多 limit 条环路径。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(graph, WHITE)
    cycles: list[list[str]] = []
    for start in sorted(graph):
        if color[start] != WHITE:
            continue
        color[start] = GRAY
        stack = [(start, iter(sorted(graph[start])))]
        path = [start]
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if color.get(nxt, BLACK) == GRAY:
                    idx = path.index(nxt)
                    cycles.append(path[idx:] + [nxt])
                    if len(cycles) >= limit:
                        return cycles
                elif color.get(nxt, BLACK) == WHITE:
                    color[nxt] = GRAY
                    stack.append((nxt, iter(sorted(graph.get(nxt, ())))))  # type: ignore[arg-type]
                    path.append(nxt)
                    advanced = True
                    break
            if not advanced:
                color[node] = BLACK
                stack.pop()
                path.pop()
    return cycles


def _cycle_proposal(ctx: PipelineContext) -> RefactorProposal | None:
    """依赖图存在环时输出消除循环依赖方案（找不到不输出）。"""
    index = ctx.index
    deps_fn = getattr(index, "dependencies", None) if index is not None else None
    if not callable(deps_fn):
        return None
    files = list(_file_locs(ctx))
    if not files:
        return None
    cycles = _find_cycles(_import_graph(deps_fn, files), _MAX_CYCLES_SHOWN)
    if not cycles:
        return None
    cycle_text = "；".join(" -> ".join(c) for c in cycles)
    return RefactorProposal(
        title=f"消除模块循环依赖（{len(cycles)} 处）",
        target="project:imports",
        kind="other",
        rationale=(
            f"依赖图检出 {len(cycles)} 条循环依赖（{cycle_text}）：循环依赖使模块无法独立测试与"
            "复用，且在 Python 下易诱发初始化顺序问题；应通过依赖倒置或公共下沉打破。"
        ),
        steps=[
            "对每条环定位「引起回边」的导入，确认双方真正需要的最小接口",
            "把双方共享的类型/常量/工具下沉到独立第三方模块，或将回调改为参数注入（依赖倒置）",
            "重构导入关系后重跑审计，确认依赖图无环",
        ],
        benefits="打破循环后模块可独立测试与按需加载，消除隐藏的初始化顺序风险。",
        related_issues=[],
        source="heuristic",
        confidence=0.75,
    )


# ---------------------------------------------------------------- W16：优先级与工作量估算

# 口径说明（W16 验收短板清偿）：
# priority —— P0=架构级/安全级风险（立即处理）；P1=显著质量风险；P2=一般改进
# （保守兜底）。规则：kind="other" 且标题含「循环依赖」→ P0；关联 issue 中存在
# 安全类（category=security）且 severity ∈ {critical, high}，或标题含「安全」
# → P0；关联 issue 存在 performance 类或 severity=high（安全类已在上一步拦截），
# 或长函数超 200 行（行数拿不到则按约定跳过该条件）→ P1；其余 → P2。
# estimated_effort_hours —— 单人专注工时的启发式估算（非承诺）：循环依赖=8.0
# （跨模块解耦需梳理接口并回归）；热点拆分=热点行数/100（行数越大拆分与回归
# 成本越高）；长函数分解=超限行数/40（超限越多子职责越多），下限 1.0；
# dedup=命中数×1.0（逐处替换逐处验证），下限 0.5；其余一律 1.0。
# 数值统一 round(…, 1)。只填字段不排序：P0 前 P1 后的稳定排序留给展示层，
# 此处重排会改变既有方案顺序（集成测试可能断言顺序）。

_CYCLE_TITLE_KEYWORD = "循环依赖"  # _cycle_proposal 标题特征词
_ARCH_RISK_LONG_FUNCTION_LINES = 200  # 长函数超过该行数视为架构治理级（P1）
_SECURITY_P0_SEVERITIES = {"critical", "high"}  # 安全类命中触发 P0 的严重级

# rationale 文案中的行数短语（decompose「共 N 行」/ split-module「共 N 行」共用）
_RATIONALE_LOC_RE = re.compile(r"共\s*(\d+)\s*行")


def _category_value(issue: Issue) -> str:
    """Issue.category → 小写字符串（兼容枚举与裸字符串两种形态）。"""
    return str(getattr(issue.category, "value", issue.category)).lower()


def _proposal_line_count(proposal: RefactorProposal, by_id: dict[str, Issue]) -> int | None:
    """方案关联的行数（decompose=函数总行数；split-module=热点总行数）。

    优先从关联 issue 的行区间取，回退解析 rationale 中的「共 N 行」文案；
    两处都拿不到返回 None（调用方按约定跳过 P1 条件 / 回落工时兜底值）。
    """
    for rid in proposal.related_issues:
        issue = by_id.get(rid)
        if issue is not None and issue.line_end > issue.line_start:
            return issue.line_end - issue.line_start + 1
    m = _RATIONALE_LOC_RE.search(proposal.rationale or "")
    return int(m.group(1)) if m else None


def _dedup_hit_count(proposal: RefactorProposal) -> int:
    """dedup 方案的命中数：优先数关联 issue，回退解析标题「N 处」。"""
    if proposal.related_issues:
        return len(proposal.related_issues)
    m = re.search(r"(\d+)\s*处", proposal.title or "")
    return int(m.group(1)) if m else 0


def _estimate_effort_hours(proposal: RefactorProposal, by_id: dict[str, Issue]) -> float:
    """按本节顶部注释的口径估算工时；信息不足时回落 1.0 兜底。"""
    title = proposal.title or ""
    if proposal.kind == "other" and _CYCLE_TITLE_KEYWORD in title:
        return 8.0
    if proposal.kind == "split-module":
        loc = _proposal_line_count(proposal, by_id)
        return round(loc / 100.0, 1) if loc else 1.0
    if proposal.kind == "decompose":
        lines = _proposal_line_count(proposal, by_id)
        if lines is None:
            return 1.0
        return round(max((lines - _LONG_FUNCTION_MIN_LINES) / 40.0, 1.0), 1)
    if proposal.kind == "dedup":
        return round(max(_dedup_hit_count(proposal) * 1.0, 0.5), 1)
    return 1.0


def _priority_of(proposal: RefactorProposal, by_id: dict[str, Issue]) -> str:
    """单条方案的 P0/P1/P2 判定（规则见本节顶部注释；只分级不排序）。"""
    title = proposal.title or ""
    if proposal.kind == "other" and _CYCLE_TITLE_KEYWORD in title:
        return "P0"  # 循环依赖属架构级风险
    related = [by_id[rid] for rid in proposal.related_issues if rid in by_id]
    # P0：关联安全类 issue 且 severity 为 critical/high，或标题直接点明「安全」
    for issue in related:
        if _category_value(issue) == "security" and _sev_value(issue) in _SECURITY_P0_SEVERITIES:
            return "P0"
    if "安全" in title:
        return "P0"
    # P1：关联 issue 存在 performance 类或 severity=high（安全类已在上一步拦截），
    # 或长函数超限——行数拿不到时按约定跳过该条件
    for issue in related:
        if _category_value(issue) == "performance" or _sev_value(issue) == "high":
            return "P1"
    if proposal.kind == "decompose":
        lines = _proposal_line_count(proposal, by_id)
        if lines is not None and lines > _ARCH_RISK_LONG_FUNCTION_LINES:
            return "P1"
    return "P2"


def _prioritize(proposals: list[RefactorProposal], issues: list[Issue] | None = None) -> None:
    """W16 后处理：原地填充 priority 与 estimated_effort_hours（契约 v1.7 新字段）。

    只填新字段：不改旧字段语义、不增删方案、不重排顺序。issues 传 ctx.issues，
    用于把 related_issues 对照回具体命中（category/severity）参与分级。
    """
    by_id = {i.id: i for i in (issues or []) if i.id}
    for proposal in proposals:
        proposal.estimated_effort_hours = _estimate_effort_hours(proposal, by_id)
        proposal.priority = _priority_of(proposal, by_id)


# ---------------------------------------------------------------- 入口


def generate_proposals(ctx: PipelineContext) -> list[RefactorProposal]:
    """聚合全部启发式，产出确定性重构方案列表（confidence 降序稳定排序）。"""
    issues = list(ctx.issues or [])
    proposals = [
        *(_decompose_proposals(ctx, issues)),
        *_dedup_proposals(issues),
    ]
    for extra in (_hotspot_proposal(ctx), _cycle_proposal(ctx)):
        if extra is not None:
            proposals.append(extra)
    for n, proposal in enumerate(proposals, 1):
        proposal.id = make_id("REF", n)
    proposals.sort(key=lambda p: -p.confidence)  # 稳定排序：同分保持生成顺序
    _prioritize(proposals, issues)  # W16：只填新字段（priority/effort），不重排
    return proposals
