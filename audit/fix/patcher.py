"""Patch 生成与 unified diff 校验/应用（W2-A1 Stage5 修复闭环）。

职责边界（只依赖 Wave 1 契约，不重写任何 Wave 1 成果）：
- build_fix_messages：按 docs/03 §3.3 Fix Agent 规范构造消息（最小改动/风格对齐/
  输出 JSON {"diff","rationale"} 指令首尾双写）。
- generate_patch：LLM json_mode 单次调用 + extract_json 解析；解析失败把错误回喂
  重试 1 次；仍失败返回 None。
- validate_diff：纯校验（---/+++/@@ 头结构 + 路径安全），不产生任何写入副作用。
- apply_diff：委托 git CLI 在 workspace.src_root 应用 diff（git 支持 repo 外 apply）。

安全边界：diff 涉及的文件路径必须解析后仍位于工作副本内；/dev/null 仅用于表示
新增/删除文件的一侧。git apply 带超时保护，进程级失败一律以 (False, 原因) 返回。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from audit.utils import extract_json, truncate
from audit.workspace import WorkspaceContext

__all__ = [
    "build_fix_messages",
    "generate_patch",
    "validate_diff",
    "apply_diff",
]

APPLY_TIMEOUT_SEC = 30.0
_MAX_FULL_FILE_LINES = 400
_MAX_SYMBOL_BLOCKS = 3
_MAX_SYMBOL_LINES = 60

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
# evidence 文本中的非符号词（rule_hit 类型名/描述词等），避免误查符号表
_EVIDENCE_STOPWORDS = {
    "return", "none", "true", "false", "null", "rule_hit", "call_chain", "rule",
    "line", "file", "path", "type", "error", "warning", "issue", "hint", "evidence",
    "self", "this", "that", "from", "with", "import", "def", "class", "and", "not",
}

_FIX_SYSTEM_PROMPT = """你是修复工程师。针对给定的已确认问题生成最小修复补丁。

要求：
1. 最小改动：只改与该问题直接相关的行，不顺手重构、不改无关格式。
2. 保持项目现有风格（命名/异常处理/日志用法向周边代码看齐）。
3. diff 必须是 git apply 可解析的 unified diff：含 diff --git / --- / +++ / @@ 头，
   文件路径相对项目根（如 a/app.py 与 b/app.py；新增/删除文件对应侧用 /dev/null）。
4. rationale 用中文说明修复思路与为何不影响其他调用方（用证据说话）。

输出格式（必须严格遵守）：只输出一个 JSON 对象
{"diff": "<unified diff 全文>", "rationale": "<中文说明>"}
不要输出 JSON 以外的任何文字。"""


# ---------------------------------------------------------------- 消息构造


def _numbered(lines: list[str], start_line: int = 1) -> str:
    """给源码行附加真实行号（1-based），供 LLM 对齐 hunk 行号。"""
    return "\n".join(f"{no:4d}: {text}" for no, text in enumerate(lines, start_line))


def build_fix_messages(
    issue: Any,
    file_source_lines: "list[str] | tuple[int, list[str]]",
    context_blocks: "list[str] | list[tuple[str, str]]",
    project_style_hint: str = "",
) -> list[dict[str, str]]:
    """按 docs/03 §3.3 Fix Agent 规范构造消息。

    Args:
        issue: Issue 模型（或等价 dict），注入 [issue] JSON（含证据链）。
        file_source_lines: 目标文件行列表（不带行号，本函数负责附加真实行号）；
            也可传 ``(起始行, 行列表)`` 元组，用于大文件按切片注入时保留真实行号。
        context_blocks: 附加上下文块，元素为 ``块标题, 正文`` 元组或纯字符串
            （正文按原样注入，调用方可自行带行号）。
        project_style_hint: 项目风格提示（空则省略该段）。

    Returns:
        OpenAI 风格 messages：system（Fix Agent 规范）+ user（issue + context）。
    """
    if isinstance(file_source_lines, tuple):
        start_line, lines = file_source_lines
        lines = list(lines)
    else:
        start_line, lines = 1, list(file_source_lines)
    issue_payload: Any = issue.to_dict() if hasattr(issue, "to_dict") else issue
    end_line = start_line + max(0, len(lines) - 1)

    parts: list[str] = [
        "[issue]",
        json.dumps(issue_payload, ensure_ascii=False, indent=2, default=str),
        "",
        f"[context] 目标文件 {issue.file}（第 {start_line}-{end_line} 行，行首为真实行号）",
        _numbered(lines, start_line),
    ]
    for i, block in enumerate(context_blocks or [], 1):
        if isinstance(block, tuple):
            title, body = block
            parts += ["", f"[context-{i}] {title}", body]
        else:
            parts += ["", f"[context-{i}]", str(block)]
    if project_style_hint:
        parts += ["", f"[style] {project_style_hint}"]
    parts += [
        "",
        '再次强调：只输出一个 JSON 对象 {"diff": "<unified diff 全文>", '
        '"rationale": "<中文说明>"}，最小改动、git apply 可解析、路径相对项目根。',
    ]
    return [
        {"role": "system", "content": _FIX_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


# ---------------------------------------------------------------- 上下文组装


def _symbol_blocks(issue: Any, workspace: WorkspaceContext, index: Any) -> list[tuple[str, str]]:
    """从 evidence 文本中抽取标识符，经 index.get_symbol 取定义源码作为上下文块。"""
    if index is None:
        return []
    file_stem = Path(str(issue.file).replace("\\", "/")).stem
    names: list[str] = []
    for item in issue.evidence or []:
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        for token in _IDENT_RE.findall(text):
            if token.lower() in _EVIDENCE_STOPWORDS or token == file_stem or token in names:
                continue
            names.append(token)
    blocks: list[tuple[str, str]] = []
    for name in names:
        if len(blocks) >= _MAX_SYMBOL_BLOCKS:
            break
        try:
            symbol = index.get_symbol(name)
        except Exception:  # noqa: BLE001 —— 索引异常不应阻断补丁生成
            symbol = None
        if symbol is None or not getattr(symbol, "file", ""):
            continue
        end = max(symbol.line_start, min(symbol.line_end, symbol.line_start + _MAX_SYMBOL_LINES - 1))
        try:
            body = workspace.read_lines(symbol.file, symbol.line_start, end)
        except (FileNotFoundError, OSError):
            continue
        if not body:
            continue
        blocks.append(
            (
                f"关联符号 {symbol.name}（{symbol.file}:{symbol.line_start}-{symbol.line_end}）",
                _numbered(body, symbol.line_start),
            )
        )
    return blocks


def _file_context(
    issue: Any, workspace: WorkspaceContext, index: Any
) -> tuple[int, list[str], list[tuple[str, str]]]:
    """组装目标文件上下文：≤400 行取全文；超长优先取与 issue 行区间重叠的函数切片。

    Returns:
        (切片起始行, 行列表, 附加上下文块)；文件不可读时行列表为空。
    """
    rel = str(issue.file).replace("\\", "/")
    try:
        lines = workspace.read_file_text(rel).splitlines()
    except (FileNotFoundError, OSError):
        return 1, [], []
    if len(lines) <= _MAX_FULL_FILE_LINES:
        start, selected = 1, lines
    else:
        slices: list[Any] = []
        if index is not None:
            try:
                slices = list(index.slices_for_file(rel) or [])
            except Exception:  # noqa: BLE001
                slices = []
        line_end = issue.line_end or issue.line_start
        overlap = [
            s for s in slices
            if getattr(s, "line_end", 0) >= (issue.line_start or 1) - 5
            and getattr(s, "line_start", 1) <= (line_end or 1) + 5
        ]
        if overlap:
            start = max(1, min(s.line_start for s in overlap))
            end = max(s.line_end for s in overlap)
        else:
            start = max(1, (issue.line_start or 1) - _MAX_FULL_FILE_LINES // 2)
            end = start + _MAX_FULL_FILE_LINES - 1
        selected = lines[start - 1 : end]
    return start, selected, _symbol_blocks(issue, workspace, index)


# ---------------------------------------------------------------- 补丁生成


def _extract_patch(payload: Any) -> tuple[str, str] | None:
    """从解析出的 JSON 中取 (diff, rationale)；结构不合法返回 None。"""
    if not isinstance(payload, dict):
        return None
    diff = payload.get("diff")
    rationale = payload.get("rationale", "")
    if not isinstance(diff, str) or not diff.strip():
        return None
    return diff, rationale if isinstance(rationale, str) else str(rationale)


async def generate_patch(
    llm: Any, issue: Any, workspace: WorkspaceContext, index: Any
) -> tuple[str, str] | None:
    """调用 LLM 生成修复补丁（json_mode 单次调用，解析失败回喂重试 1 次）。

    Args:
        llm: LLMClient（生产为 GlmClient，测试为 FakeLLMClient）。
        issue: 待修复问题。
        workspace: 工作副本上下文（同文件后续 Patch 基于已改内容重新读取）。
        index: IndexStore | None，用于切片与关联符号上下文。

    Returns:
        (diff, rationale)；两次解析均失败或目标文件不可读时返回 None。
    """
    start, lines, extra_blocks = _file_context(issue, workspace, index)
    if not lines:
        return None
    messages = build_fix_messages(issue, (start, lines), extra_blocks)

    last_error = ""
    response = await llm.chat(messages, json_mode=True)
    for attempt in (0, 1):
        try:
            payload = extract_json(response.content)
        except ValueError as exc:
            last_error = str(exc)
        else:
            patch = _extract_patch(payload)
            if patch is not None:
                return patch
            last_error = "JSON 中缺少非空 diff 字段"
        if attempt == 0:  # 带错误回喂重试一次
            retry_messages = list(messages) + [
                {"role": "assistant", "content": response.content or ""},
                {
                    "role": "user",
                    "content": (
                        f"上一次输出无法解析（{last_error}）。请重新输出，且只输出一个 "
                        'JSON 对象 {"diff": "<unified diff 全文>", "rationale": "<中文说明>"}，'
                        "diff 必须是 git apply 可解析的 unified diff。"
                    ),
                },
            ]
            response = await llm.chat(retry_messages, json_mode=True)
    return None


# ---------------------------------------------------------------- diff 校验


def _header_path(header: str) -> str | None:
    """从 '--- a/path' / '+++ b/path' 行提取路径（剥离引号与 tab 时间戳）。"""
    body = header[4:].strip()
    if body.startswith('"') and body.endswith('"') and len(body) >= 2:
        body = body[1:-1]
    body = body.split("\t", 1)[0].strip()
    return body or None


def _strip_ab_prefix(path: str) -> str:
    """剥离 unified diff 的 a/ / b/ 前缀。"""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _path_error(rel_path: str, root: Path) -> str | None:
    """校验单个文件路径：必须是 workspace 内相对路径；合法返回 None。"""
    rel = rel_path.replace("\\", "/")
    if not rel or rel == ".":
        return "空路径"
    if rel.startswith("/"):
        return "绝对路径"
    parts = rel.split("/")
    if ".." in parts:
        return "包含 .. 越级"
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return "越出工作副本根"
    if candidate == root:
        return "指向工作副本根本身"
    return None


def validate_diff(diff: str, workspace: WorkspaceContext) -> list[str]:
    """校验 LLM 产出的 unified diff；返回错误列表（空列表 = 合法）。

    规则：
    - 必须含 --- / +++ / @@ 三类头；
    - 涉及的文件路径必须是 workspace 内相对路径（禁止 ..、绝对路径、越出根）；
    - /dev/null 视为合法（表示新增/删除文件的一侧）。
    """
    if not diff or not diff.strip():
        return ["diff 为空"]
    lines = diff.splitlines()
    errors: list[str] = []
    if not any(ln.startswith("--- ") for ln in lines):
        errors.append("缺少 '--- ' 文件头")
    if not any(ln.startswith("+++ ") for ln in lines):
        errors.append("缺少 '+++ ' 文件头")
    if not any(ln.startswith("@@") for ln in lines):
        errors.append("缺少 '@@' hunk 头")

    root = Path(workspace.src_root).resolve()
    seen: set[str] = set()
    for header in lines:
        if not header.startswith(("--- ", "+++ ")):
            continue
        raw = _header_path(header)
        if raw is None or raw == "/dev/null":
            continue
        rel = _strip_ab_prefix(raw)
        if rel in seen:
            continue
        seen.add(rel)
        error = _path_error(rel, root)
        if error:
            errors.append(f"文件路径不合法 {raw!r}: {error}")
    return errors


# ---------------------------------------------------------------- diff 应用


def _decode_output(data: bytes | None) -> str:
    """子进程输出解码：utf-8 优先，GBK 容错（与沙箱实现同语义）。"""
    if not data:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def apply_diff(workspace: WorkspaceContext, diff: str, check_only: bool = False) -> tuple[bool, str]:
    """用 git CLI 应用 unified diff（cwd=workspace.src_root，git 支持 repo 外 apply）。

    Args:
        workspace: 工作副本上下文。
        diff: unified diff 全文。
        check_only: True 时只做 `git apply --check` 干跑校验，不落盘。

    Returns:
        (是否成功, 失败原因)。git 不可用/超时/退出码非 0 均以 (False, 原因) 返回，
        不抛异常。
    """
    command = ["git", "apply"]
    if check_only:
        command.append("--check")
    command.append("--whitespace=nowarn")
    try:
        proc = subprocess.run(  # noqa: S603 —— 固定命令列表，无 shell
            command,
            cwd=str(workspace.src_root),
            input=diff.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=APPLY_TIMEOUT_SEC,
        )
    except FileNotFoundError:
        return False, "git 不可用：未找到 git 可执行文件，无法应用补丁"
    except subprocess.TimeoutExpired:
        return False, f"git apply 超时（>{APPLY_TIMEOUT_SEC:.0f}s）"
    except OSError as exc:
        return False, f"git 执行失败: {type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        stderr = _decode_output(proc.stderr)
        return False, f"git apply 失败（exit={proc.returncode}）: {truncate(stderr, 500)}"
    return True, ""
