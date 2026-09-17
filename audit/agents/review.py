"""Review Agent（文件审查角色，T3 基础上 Wave 2 A3 增强）。

三条产出路径（按调用方选择）：
- 工具取证路径（review_mode="tools"，本模块 make_tools_review_fn）：
  SimpleAgentRuntime 工具循环 + 只读工具（list_files/read_file/search_code/
  get_symbol/find_references/get_call_chain/get_dependencies）+ 包装版
  record_issues（收集载荷并做行号范围硬校验）；大文件（>400 行）按
  ctx.index.grouped_slices 分片逐片审查后本地合并去重；
- runtime 路径：显式传入 AgentRuntime 时走完整工具循环，依赖 record_issues 收口；
- 简化路径：runtime=None 时单次 json_mode 调用 + extract_json 解析（批量小切片
  合并审查即走此路径，一次调用审 5 个文件）。

产出前置校验：行号越界（line_start < 1 或 > 文件行数）或文件不存在的条目直接丢弃；
合法条目映射为 Issue（source=IssueSource.LLM，id 留空由调用方/集成层分配）。

消融开关（契约 v1.3，docs/08 §3）：config.enable_symbol_context=False 时 prompt 组包
不附符号上下文块（架构卡片与文件源码保留），单文件 / 大文件切片 / 批量小切片三条
路径一致生效；开关判断收在组包入口层（review_file 的 config 可选参数与
make_tools_review_fn 闭包捕获的 ctx.config），业务逻辑不散落判断。

prompt 使用 audit/agents/prompts.py 的版本化常量（REVIEW_PROMPT_V3，prompt_version=v2）。
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable
from typing import Any, Callable, Sequence

from audit.agent.base import AgentLimits, AgentRuntime, ToolSpec
from audit.agent.runtime import SimpleAgentRuntime
from audit.agent.tools import build_default_tools, format_numbered_lines
from audit.config import AuditConfig
from audit.detect.base import mask_secret_text, mask_string_literals
from audit.llm.base import LLMClient, Message
from audit.models import Category, Issue, IssueSource, Severity
from audit.pipeline import PipelineContext
from audit.utils import extract_json
from audit.workspace import WorkspaceContext
from audit.agents.prompts import PROMPT_VERSION, REVIEW_PROMPT_V3

__all__ = [
    "REVIEW_TOOL_NAMES",
    "TOOLS_REVIEW_TOOL_NAMES",
    "TOOLS_REVIEW_MAX_ITERATIONS",
    "TOOLS_REVIEW_TOKEN_BUDGET_CAP",
    "REVIEW_MAX_ITERATIONS",
    "SOURCE_LINES_LIMIT",
    "REVIEW_SLICE_LINES",
    "BATCH_GROUP_SIZE",
    "BATCH_SMALL_FILE_LINES",
    "PROMPT_VERSION",
    "REVIEW_PROMPT_V3",
    "REVIEW_SYSTEM_PROMPT_TEMPLATE",
    "REVIEW_TASK_INSTRUCTION",
    "SYMBOL_CONTEXT_DISABLED_TEXT",
    "build_review_prompt",
    "format_hints",
    "format_symbols",
    "review_file",
    "make_tools_review_fn",
    "review_files_parallel",
    "issues_from_payloads",
]

# Review 角色可用的工具（docs/03 §3.1 prompt 中列出的 7 个）
REVIEW_TOOL_NAMES = (
    "read_file",
    "search_code",
    "get_symbol",
    "find_references",
    "get_call_chain",
    "get_dependencies",
    "record_issues",
)

# 工具取证路径（review_mode="tools"）的只读工具：docs/07 §3.2 约定的 7 个 + 包装版 record_issues
TOOLS_REVIEW_TOOL_NAMES = (
    "list_files",
    "read_file",
    "search_code",
    "get_symbol",
    "find_references",
    "get_call_chain",
    "get_dependencies",
)

# W14-A2（M-1 归一）：Review 迭代上限的唯一事实来源是 AuditConfig.max_tool_iterations
# （默认 12，与历史硬编码行为一致，NFR-10"上限可配置"由此兑现）。以下两个常量
# 降级为"config 缺失/非法时的兜底默认值"继续导出（audit.agents __all__ 不断链），
# 正常路径不再直接消费（经 _max_iterations_from_config 读取）。
REVIEW_MAX_ITERATIONS = 12  # docs/02 §3.2：Review Agent 迭代上限（默认/兜底值）
TOOLS_REVIEW_MAX_ITERATIONS = 12  # 工具取证路径迭代上限（docs/07 §3.2；默认/兜底值）
TOOLS_REVIEW_TOKEN_BUDGET_CAP = 200_000  # 单文件工具路径 token 预算上限
SOURCE_LINES_LIMIT = 400  # 单次组包的源码行数上限（>400 行建议走函数切片审查）
REVIEW_SLICE_LINES = 400  # 大文件切片阈值：>400 行时按 grouped_slices(file, 400) 分片
BATCH_GROUP_SIZE = 5  # 批量小切片：一次审查调用合并的小文件数（docs/02 §5 两级裁剪）
BATCH_SMALL_FILE_LINES = 80  # 批量小切片准入：无 hint 且 <80 行

# 版本化 prompt（v2）：本模块保留旧常量名作为别名，历史引用不断链
REVIEW_SYSTEM_PROMPT_TEMPLATE = REVIEW_PROMPT_V3

REVIEW_TASK_INSTRUCTION = (
    "请审查文件 {file_path}，完成后恰好调用一次 record_issues 提交结论（无问题提交空数组）。"
    "不要用文字总结代替工具调用。"
)

REVIEW_BATCH_TASK_INSTRUCTION = (
    "本批共 {n_files} 个文件：{file_list}。请逐文件审查，完成后恰好调用一次 "
    "record_issues 一次性提交全部问题：每条 issue 的 file 字段必须精确填写其所属文件的"
    "相对路径（只能取自本批文件清单，行号使用该文件的绝对行号）；无问题的文件不要提交条目；"
    "整批无问题就提交空数组。不要用文字总结代替工具调用。"
)

REVIEW_BATCH_JSON_INSTRUCTION = (
    "请审查本批 {n_files} 个文件（{file_list}），以 JSON 输出审查结论："
    '{{"issues": [每条含 category/severity/title/file/line_start/line_end/'
    "description/evidence/suggestion/confidence]}}；"
    "每条 issue 的 file 字段必须精确填写其所属文件的相对路径（只能取自本批文件清单）；"
    '没有问题返回 {{"issues": []}}。'
)

_TRUNCATION_MARKER = "\n...[截断，共 {total} 行，仅展示前 {limit} 行]"

# 消融 −symbol_context（契约 v1.3）：符号上下文关闭时 [file_symbols] 槽位的占位文本
SYMBOL_CONTEXT_DISABLED_TEXT = "（符号上下文已禁用）"

# review_fn(workspace, file_path, hints[, extra_files]) -> list[Issue] | awaitable
IssueReviewFnLike = Callable[..., "Sequence[Issue] | Awaitable[Sequence[Issue]]"]


# ---------------------------------------------------------------------- prompt 组包


def build_review_prompt(
    architecture_summary: str,
    file_path: str,
    loc: int,
    symbols_text: str,
    source_text: str,
    hints_text: str,
) -> str:
    """按 docs/03 §3.1（v2，含反幻觉硬约束）模板组装 Review System Prompt。"""
    return REVIEW_PROMPT_V3.format(
        architecture_card=architecture_summary.strip() or "（无架构卡片）",
        file_path=file_path,
        loc=loc,
        file_symbols=symbols_text,
        file_source=source_text,
        rule_hints=hints_text,
    )


def format_hints(hints: list[str] | None) -> str:
    """规则 hint 列表渲染；空列表显示 '无'。"""
    if not hints:
        return "无"
    return "\n".join(f"- {h}" for h in hints)


def _one_line_hints(hints: list[str] | None) -> str:
    """批量模式下单文件 hint 的单行渲染。"""
    if not hints:
        return "无"
    return "；".join(hints)


def format_symbols(index: Any, file_path: str) -> str:
    """本文件符号表渲染；索引不可用时如实说明。"""
    if index is None:
        return "（索引不可用，无法提供符号表）"
    try:
        symbols = index.symbols_for_file(file_path)
    except Exception as exc:
        return f"（符号表读取失败: {type(exc).__name__}）"
    if not symbols:
        return "（本文件无已解析符号）"
    lines = []
    for s in symbols:
        sig = f" {s.signature}" if s.signature else ""
        lines.append(f"- {s.kind} {s.name} ({s.line_start}-{s.line_end}){sig}")
    return "\n".join(lines)


def _symbols_block(index: Any, file_path: str, include: bool) -> str:
    """组装进 prompt 的符号上下文块；include=False 时以禁用说明占位（消融开关）。"""
    if not include:
        return SYMBOL_CONTEXT_DISABLED_TEXT
    return format_symbols(index, file_path)


# ---------------------------------------------------------------------- 主入口


def _max_iterations_from_config(config: AuditConfig | None) -> int:
    """从 config.max_tool_iterations 读取 Review 迭代上限（W14-A2 M-1 接线）。

    config 缺失（review_file 的 config=None 兼容路径）或字段非法（缺失/非数值）
    时回落 REVIEW_MAX_ITERATIONS（12，历史硬编码值）；非正数钳到 1。
    """
    if config is None:
        return REVIEW_MAX_ITERATIONS
    try:
        value = int(getattr(config, "max_tool_iterations", REVIEW_MAX_ITERATIONS))
    except (TypeError, ValueError):
        return REVIEW_MAX_ITERATIONS
    return max(1, value)


def _message_budget_from_config(config: AuditConfig | None) -> int:
    """从 config.agent_message_budget 读取工具循环消息预算（W22-A 接线）。

    config 缺失或字段非法/负数时按 0（关闭折叠，与 W22-A 之前行为一致）。
    """
    if config is None:
        return 0
    try:
        value = int(getattr(config, "agent_message_budget", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


async def review_file(
    workspace: WorkspaceContext,
    index: Any,
    llm: LLMClient,
    file_path: str,
    hints: list[str] | None = None,
    architecture_summary: str = "",
    runtime: AgentRuntime | None = None,
    extra_files: list[tuple[str, str, list[str]]] | None = None,
    limits: AgentLimits | None = None,
    config: AuditConfig | None = None,
) -> list[Issue]:
    """审查单个文件，返回经行号校验的 Issue 列表。

    新增可选参数（向后兼容）：
    - extra_files：批量小切片合并审查的附加文件 (rel_path, source, hints)；
      主文件与附加文件组合源码（带显式文件分隔标记）合并为一次审查调用，
      要求输出按文件分组（每条 issue 的 file 字段精确归属）；
    - limits：runtime 路径的运行限制；缺省时 max_iterations 取
      config.max_tool_iterations（W14-A2 M-1；config 也为 None 时回落 12）；
    - config：消融开关载体（契约 v1.3）兼迭代上限来源（W14-A2 M-1）；
      config.enable_symbol_context=False 时 prompt 不附符号上下文块。None 时行为不变。
    """
    hint_list = list(hints or [])
    extras = [
        (str(rel).replace("\\", "/"), str(source), list(h or []))
        for rel, source, h in (extra_files or [])
    ]
    # 消融 −symbol_context：开关判断收在组包入口层，业务路径不散落判断
    include_symbols = config is None or bool(getattr(config, "enable_symbol_context", True))

    loc = workspace.line_count(file_path)  # 文件不存在时自然抛 FileNotFoundError
    source_lines = workspace.read_lines(file_path, 1, SOURCE_LINES_LIMIT)
    source_text = format_numbered_lines(source_lines, start=1)
    if loc > len(source_lines):
        source_text += _TRUNCATION_MARKER.format(total=loc, limit=SOURCE_LINES_LIMIT)

    batch_instruction: str | None = None
    if extras:
        # 批量模式：主文件 + 附加小文件合并组包（显式文件分隔标记，输出按文件分组）
        n_files = 1 + len(extras)
        symbol_blocks = [
            f"===== 文件 1/{n_files}: {file_path}（{loc} 行）=====\n"
            f"{_symbols_block(index, file_path, include_symbols)}"
        ]
        source_blocks = [f"===== 文件 1/{n_files}: {file_path}（{loc} 行）=====\n{source_text}"]
        hint_blocks = [f"- {file_path}: {_one_line_hints(hint_list)}"]
        total_loc = loc
        for i, (rel, src, hs) in enumerate(extras, start=2):
            rel_lines = src.splitlines()
            rel_loc = len(rel_lines)
            shown = rel_lines[:SOURCE_LINES_LIMIT]
            text = format_numbered_lines(shown, start=1)
            if rel_loc > len(shown):
                text += _TRUNCATION_MARKER.format(total=rel_loc, limit=SOURCE_LINES_LIMIT)
            header = f"===== 文件 {i}/{n_files}: {rel}（{rel_loc} 行）====="
            symbol_blocks.append(f"{header}\n{_symbols_block(index, rel, include_symbols)}")
            source_blocks.append(f"{header}\n{text}")
            hint_blocks.append(f"- {rel}: {_one_line_hints(hs)}")
            total_loc += rel_loc
        system_prompt = build_review_prompt(
            architecture_summary=architecture_summary,
            file_path=f"以下 {n_files} 个文件（批量小文件合并审查）",
            loc=total_loc,
            symbols_text="\n\n".join(symbol_blocks),
            source_text="\n\n".join(source_blocks),
            hints_text="\n".join(hint_blocks),
        )
        batch_instruction = REVIEW_BATCH_TASK_INSTRUCTION.format(
            n_files=n_files, file_list=", ".join([file_path] + [rel for rel, _, _ in extras])
        )
    else:
        symbols_text = _symbols_block(index, file_path, include_symbols)
        system_prompt = build_review_prompt(
            architecture_summary=architecture_summary,
            file_path=file_path,
            loc=loc,
            symbols_text=symbols_text,
            source_text=source_text,
            hints_text=format_hints(hint_list),
        )

    collected: list[dict[str, Any]] = []
    if runtime is not None:
        state = {"record_issues_called": False}  # R1-26：检测 Agent 是否正常收口
        _ensure_review_tools(runtime, workspace, index, collected, state)
        user_content = batch_instruction or REVIEW_TASK_INSTRUCTION.format(file_path=file_path)
        messages: list[Message] = [{"role": "user", "content": user_content}]
        await runtime.run(
            system_prompt,
            messages,
            # W14-A2（M-1）：迭代上限从 config.max_tool_iterations 读取（缺省兜底 12）；
            # W22-A：兜底 limits 同样携带消息预算（显式 limits 优先，主路径已带）
            limits=limits
            or AgentLimits(
                max_iterations=_max_iterations_from_config(config),
                message_budget_chars=_message_budget_from_config(config),
            ),
        )
        if not state["record_issues_called"]:
            # R1-26：runtime 非正常结束（从未调用 record_issues）必须显式失败，
            # 由调用方记入 ctx.extra["review_errors"]，而不是静默当作"无问题"。
            raise RuntimeError(
                f"Review Agent 非正常结束：{file_path} 审查循环结束但从未调用 record_issues 收口"
            )
    else:
        user_content = batch_instruction or (
            f"请审查文件 {file_path}，以 JSON 输出审查结论："
            '{"issues": [每条含 category/severity/title/file/line_start/line_end/'
            'description/evidence/suggestion/confidence]}；没有问题返回 {"issues": []}。'
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        resp = await llm.chat(messages, json_mode=True)
        collected = _parse_issues_payload(resp.content or "")

    return issues_from_payloads(collected, workspace)


# ---------------------------------------------------------------------- 工具装配与解析


def _ensure_review_tools(
    runtime: AgentRuntime,
    workspace: WorkspaceContext,
    index: Any,
    collected: list[dict[str, Any]],
    state: dict[str, bool] | None = None,
) -> None:
    """把 Review 所需工具注册进 runtime；record_issues 包装为带收集能力的处理器。

    - 只读工具：runtime 已注册则尊重调用方的版本，缺失才补；
    - record_issues：包装现有（或默认）处理器，把校验通过的 issues 载荷收集到
      collected 供本模块转换为 Issue；state 非空时在收到调用（无论批次是否
      校验通过）后置 state["record_issues_called"] = True，供非正常结束检测（R1-26）。
    """
    defaults = {spec.name: spec for spec in build_default_tools(workspace, index, sandbox=None)}
    for name in REVIEW_TOOL_NAMES:
        default_spec = defaults.get(name)
        if default_spec is None:
            continue
        if name == "record_issues":
            existing = runtime.get_tool(name) or default_spec
            inner = existing.handler

            async def capturing(**kwargs: Any) -> Any:
                if state is not None:
                    state["record_issues_called"] = True
                result = await inner(**kwargs)
                if (
                    isinstance(result, dict)
                    and result.get("ok")
                    and isinstance(kwargs.get("issues"), list)
                ):
                    collected.extend(kwargs["issues"])
                return result

            runtime.register_tool(
                ToolSpec(
                    name=existing.name,
                    description=existing.description,
                    parameters=existing.parameters,
                    handler=capturing,
                )
            )
        elif not runtime.has_tool(name):
            runtime.register_tool(default_spec)


def _parse_issues_payload(text: str) -> list[dict[str, Any]]:
    """从 json_mode 响应中解析 issues 数组；解析失败返回空列表（宁缺毋滥）。"""
    try:
        data = extract_json(text)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = data.get("issues", [])
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def issues_from_payloads(payloads: list[dict[str, Any]], workspace: WorkspaceContext) -> list[Issue]:
    """把 record_issues 载荷映射为 Issue，行号越界/文件不存在的条目丢弃。"""
    issues: list[Issue] = []
    for payload in payloads:
        issue = _issue_from_payload(payload, workspace)
        if issue is not None:
            issues.append(issue)
    return issues


def _issue_from_payload(payload: dict[str, Any], workspace: WorkspaceContext) -> Issue | None:
    file_name = str(payload.get("file", "")).replace("\\", "/").strip()
    try:
        total = workspace.line_count(file_name)
    except OSError:
        return None  # 文件不存在：视为幻觉，丢弃
    line_start = payload.get("line_start")
    line_end = payload.get("line_end", line_start)
    if not isinstance(line_start, int) or isinstance(line_start, bool) or line_start < 1 or line_start > total:
        return None  # 行号越界：丢弃
    if not isinstance(line_end, int) or isinstance(line_end, bool) or line_end < line_start:
        line_end = line_start
    line_end = min(line_end, total)

    try:
        category = Category(str(payload.get("category", "")))
    except ValueError:
        category = Category.BUG
    try:
        severity = Severity(str(payload.get("severity", "")))
    except ValueError:
        severity = Severity.MEDIUM
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))

    # W15-A1（docs/20 §4.2）：LLM 载荷的 evidence 可能携带源码密钥明文，逐条过
    # mask_secret_text（与规则路径同一打码口径）后再进 Issue。
    evidence = [
        mask_secret_text(str(e))
        for e in (payload.get("evidence") or [])
        if str(e).strip()
    ]
    try:
        snippet = "\n".join(workspace.read_lines(file_name, line_start, line_end))
    except OSError:
        snippet = ""
    # W15-A1（docs/20 §4.2）：code_snippet 套用规则路径 mask_snippet 的同一打码语义
    # （mask_string_literals 逐行处理）——报告不泄露密钥明文（NFR-11）。
    snippet = "\n".join(mask_string_literals(ln) for ln in snippet.splitlines())

    return Issue(
        id="",  # 由调用方/集成层统一分配
        category=category,
        severity=severity,
        title=str(payload.get("title", "")),
        file=file_name,
        line_start=line_start,
        line_end=line_end,
        code_snippet=snippet,
        description=str(payload.get("description", "")),
        evidence=evidence,
        suggestion=str(payload.get("suggestion", "")),
        confidence=confidence,
        source=IssueSource.LLM,
    )


# ---------------------------------------------------------------------- 工具取证路径（W2-A3）


def _tools_runtime(
    workspace: WorkspaceContext, index: Any, llm: LLMClient
) -> SimpleAgentRuntime:
    """构建只装配只读工具的 runtime；record_issues 由调用方按需包装注入。

    只读工具集见 TOOLS_REVIEW_TOOL_NAMES（docs/07 §3.2 约定的 7 个）；
    record_issues 不在此注册，避免收集列表跨次审查串扰（每次审查新建 runtime）。
    """
    runtime = SimpleAgentRuntime(llm)
    defaults = {spec.name: spec for spec in build_default_tools(workspace, index, sandbox=None)}
    for name in TOOLS_REVIEW_TOOL_NAMES:
        spec = defaults.get(name)
        if spec is not None:
            runtime.register_tool(spec)
    return runtime


async def _review_slice(
    runtime: Any,
    workspace: WorkspaceContext,
    index: Any,
    file_path: str,
    start: int,
    end: int,
    slice_index: int,
    slice_total: int,
    hints: list[str],
    architecture_summary: str,
    limits: AgentLimits,
    include_symbols: bool = True,
    state: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """审查单个函数分组切片，返回收集到的 issue 载荷（行号为全文件绝对行号）。

    include_symbols=False（消融 −symbol_context）时组包不附符号上下文块。
    state 非空（R4-6）时跟踪 record_issues 收口：runtime 结束却从未调用
    record_issues 时抛 RuntimeError，由调用方记入 review_errors，
    而不是把该切片静默当作"无问题"。
    """
    collected: list[dict[str, Any]] = []
    _ensure_review_tools(runtime, workspace, index, collected, state)
    lines = workspace.read_lines(file_path, start, end)
    source_text = (
        f"[切片 {slice_index}/{slice_total}] 以下为该文件第 {start}-{end} 行"
        "（函数分组切片，行号为全文件绝对行号）：\n" + format_numbered_lines(lines, start=start)
    )
    try:
        loc = workspace.line_count(file_path)
    except OSError:
        loc = end
    system_prompt = build_review_prompt(
        architecture_summary=architecture_summary,
        file_path=file_path,
        loc=loc,
        symbols_text=_symbols_block(index, file_path, include_symbols),
        source_text=source_text,
        hints_text=format_hints(hints),
    )
    task = (
        REVIEW_TASK_INSTRUCTION.format(file_path=file_path)
        + f" 本次仅审查第 {start}-{end} 行切片（{slice_index}/{slice_total}），"
        "报告行号必须使用上面展示的全文件绝对行号。"
    )
    await runtime.run(system_prompt, [{"role": "user", "content": task}], limits=limits)
    if state is not None and not state["record_issues_called"]:
        # R4-6：与单文件路径的 R1-26 同口径——切片 runtime 非正常结束必须显式失败
        raise RuntimeError(
            f"Review Agent 非正常结束：{file_path} 第 {start}-{end} 行切片"
            f"（{slice_index}/{slice_total}）未调用 record_issues 收口"
        )
    return collected


def _merge_slice_issues(issues: list[Issue]) -> list[Issue]:
    """多切片审查结果的合并去重：按 (file, category, ±5 行) 合并。

    优先复用 audit.detect.engine.dedup_issues（同一合并语义）；导入失败时
    本地兜底实现（保留置信度高者并拼接 evidence）。
    """
    if len(issues) <= 1:
        return list(issues)
    try:
        from audit.detect.engine import dedup_issues

        return dedup_issues(issues)
    except Exception:  # pragma: no cover - 防御性兜底
        kept: list[Issue] = []
        for issue in issues:
            target: Issue | None = None
            for k in kept:
                if (
                    k.file == issue.file
                    and k.category == issue.category
                    and k.line_start <= issue.line_end + 5
                    and issue.line_start <= k.line_end + 5
                ):
                    target = k
                    break
            if target is None:
                kept.append(issue)
            elif issue.confidence > target.confidence:
                evidence = list(target.evidence)
                evidence.extend(e for e in issue.evidence if e not in evidence)
                target.evidence = evidence
                target.line_start = min(target.line_start, issue.line_start)
                target.line_end = max(target.line_end, issue.line_end)
                target.confidence = issue.confidence
        return kept


def make_tools_review_fn(ctx: PipelineContext) -> IssueReviewFnLike:
    """构造工具取证式 review_fn（review_mode="tools" 时由编排层注入）。

    返回 async review_fn(workspace, file_path, hints=None, extra_files=None) -> list[Issue]：
    - 工具路径：每次审查新建 SimpleAgentRuntime，注册只读工具（list_files/read_file/
      search_code/get_symbol/find_references/get_call_chain/get_dependencies）+
      包装版 record_issues（收集载荷并经工具层行号范围校验）；
      AgentLimits(max_iterations=config.max_tool_iterations（W14-A2 M-1，默认 12）,
      token_budget=min(200_000, ctx.config.token_budget//10))；
      产出 Issue 的 source=IssueSource.LLM；
    - 大文件切片：文件 >400 行且 ctx.index 可用时，按 ctx.index.grouped_slices(file, 400)
      逐片审查后本地合并去重；index 为 None 则整文件截断审查（review_file 默认行为）；
    - 批量小切片：extra_files 非空时合并为一次 json 审查调用（低风险小文件不需要工具取证）；
    - 消融开关（契约 v1.3）：从闭包捕获 ctx.config——enable_symbol_context=False 时
      三条路径（单文件 / 大文件切片 / 批量小切片）组包均不附符号上下文块；
    - 并在 ctx.extra["prompt_version"] 记录 "v2"。
    """
    architecture_summary = str(getattr(ctx.architecture, "text", "") or "")
    config = ctx.config  # 闭包捕获：供消融开关判断、迭代上限（M-1）与透传 review_file
    limits = AgentLimits(
        # W14-A2（M-1）：迭代上限从 config.max_tool_iterations 读取（缺省兜底 12）
        max_iterations=_max_iterations_from_config(config),
        token_budget=min(TOOLS_REVIEW_TOKEN_BUDGET_CAP, int(ctx.config.token_budget) // 10),
        # W22-A：消息总字符预算（0=关闭折叠；config 缺字段时按 0 保持旧行为）
        message_budget_chars=_message_budget_from_config(config),
    )
    include_symbols = bool(getattr(config, "enable_symbol_context", True))

    async def review_fn(
        workspace: WorkspaceContext,
        file_path: str,
        hints: list[str] | None = None,
        extra_files: list[tuple[str, str, list[str]]] | None = None,
    ) -> list[Issue]:
        ctx.extra["prompt_version"] = PROMPT_VERSION
        hint_list = list(hints or [])
        if extra_files:
            # 批量小切片：合并一次 json 调用（低风险切片不值得工具取证开销）
            return await review_file(
                workspace,
                ctx.index,
                ctx.llm,
                file_path,
                hints=hint_list,
                architecture_summary=architecture_summary,
                extra_files=list(extra_files),
                config=config,
            )

        loc = workspace.line_count(file_path)
        slices: list[Any] = []
        if loc > REVIEW_SLICE_LINES and ctx.index is not None:
            try:
                slices = list(ctx.index.grouped_slices(file_path, REVIEW_SLICE_LINES))
            except Exception:
                slices = []  # 切片失败回退整文件截断审查

        if slices:
            merged: list[Issue] = []
            for i, sl in enumerate(slices, 1):
                runtime = _tools_runtime(workspace, ctx.index, ctx.llm)
                state: dict[str, bool] = {"record_issues_called": False}
                try:
                    collected = await _review_slice(
                        runtime,
                        workspace,
                        ctx.index,
                        file_path,
                        int(getattr(sl, "line_start", 1) or 1),
                        int(getattr(sl, "line_end", loc) or loc),
                        i,
                        len(slices),
                        hint_list,
                        architecture_summary,
                        limits,
                        include_symbols=include_symbols,
                        state=state,
                    )
                except Exception as exc:  # noqa: BLE001 —— R4-6：单切片非正常结束不静默
                    # 记入 review_errors（键带切片序号），继续审查其余切片
                    ctx.extra.setdefault("review_errors", {})[f"{file_path}#slice-{i}"] = repr(exc)
                    continue
                merged.extend(issues_from_payloads(collected, workspace))
            return _merge_slice_issues(merged)

        runtime = _tools_runtime(workspace, ctx.index, ctx.llm)
        return await review_file(
            workspace,
            ctx.index,
            ctx.llm,
            file_path,
            hints=hint_list,
            architecture_summary=architecture_summary,
            runtime=runtime,
            limits=limits,
            config=config,
        )

    review_fn._supports_extra_files = True  # type: ignore[attr-defined]  # 供 review_files_parallel 识别可批量
    return review_fn


# ---------------------------------------------------------------------- 文件级并发（W2-A3）


async def review_files_parallel(
    ctx: PipelineContext,
    jobs: list[tuple[str, list[str]]],
    review_fn: IssueReviewFnLike | None = None,
) -> dict[str, list[Issue]]:
    """文件级并发审查：asyncio.Semaphore(concurrency) + asyncio.gather。

    - review_fn 缺省时用 make_tools_review_fn(ctx) 构造工具取证路径；
    - 批量小切片：config.batch_small_slices 且 review_fn 支持批量（由
      make_tools_review_fn 产出）时，把"无规则 hint 且 <80 行"的小文件按
      BATCH_GROUP_SIZE(5) 个一组拼进一次审查调用（组合源码 + 文件分隔标记，
      要求输出按文件分组），显著省 token；其余文件逐个并发审查；
    - 单文件（或单组）异常降级为空结果，并把 repr(exc) 记入
      ctx.extra["review_errors"][file]（批量组内每个文件都记）；
    - 返回 {rel_path: [Issue, ...]}（仅含成功条目；失败文件不在结果中）。
    """
    if review_fn is None:
        review_fn = make_tools_review_fn(ctx)
    workspace = ctx.workspace
    limit = max(1, int(getattr(ctx.config, "concurrency", 8) or 8))
    semaphore = asyncio.Semaphore(limit)
    errors = ctx.extra.setdefault("review_errors", {})

    entries = [(str(rel).replace("\\", "/"), list(hints or [])) for rel, hints in jobs]

    batch_enabled = bool(getattr(ctx.config, "batch_small_slices", False)) and bool(
        getattr(review_fn, "_supports_extra_files", False)
    )

    groups: list[list[tuple[str, list[str]]]] = []
    singles: list[tuple[str, list[str]]] = []
    if batch_enabled:
        small: list[tuple[str, list[str]]] = []
        small_keys: set[str] = set()
        for rel, hints in entries:
            if hints:
                continue  # 有规则 hint 的文件走独立审查（docs/02 §5 两级裁剪）
            try:
                n_lines = workspace.line_count(rel)
            except OSError:
                continue  # 不可读文件交由单文件路径自然报错
            if n_lines < BATCH_SMALL_FILE_LINES:
                small.append((rel, hints))
                small_keys.add(rel)
        singles = [e for e in entries if e[0] not in small_keys]
        for i in range(0, len(small), BATCH_GROUP_SIZE):
            groups.append(small[i : i + BATCH_GROUP_SIZE])
    else:
        singles = list(entries)

    async def _run_single(rel: str, hints: list[str]) -> dict[str, list[Issue]]:
        async with semaphore:
            result = review_fn(workspace, rel, hints)
            if inspect.isawaitable(result):
                result = await result
            return {rel: list(result or [])}

    async def _run_group(group: list[tuple[str, list[str]]]) -> dict[str, list[Issue]]:
        async with semaphore:
            first_rel, first_hints = group[0]
            extras = [(rel, workspace.read_file_text(rel), hints) for rel, hints in group[1:]]
            result = review_fn(workspace, first_rel, first_hints, extra_files=extras)
            if inspect.isawaitable(result):
                result = await result
            by_file: dict[str, list[Issue]] = {rel: [] for rel, _ in group}
            for issue in result or []:
                key = issue.file if isinstance(issue, Issue) and issue.file in by_file else first_rel
                by_file[key].append(issue)
            return by_file

    async def _guarded(factory: Any, keys: list[str]) -> dict[str, list[Issue]] | None:
        try:
            return await factory()
        except Exception as exc:  # 单文件/单组故障不影响其余文件
            for k in keys:
                errors[k] = repr(exc)
            return None

    tasks = [
        _guarded(lambda r=rel, h=hints: _run_single(r, h), [rel]) for rel, hints in singles
    ]
    tasks += [_guarded(lambda g=group: _run_group(g), [rel for rel, _ in group]) for group in groups]
    results: dict[str, list[Issue]] = {}
    for part in await asyncio.gather(*tasks):
        if part:
            results.update(part)
    return results
