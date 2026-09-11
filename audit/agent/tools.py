"""默认工具集（T3）：docs/03 §2 定义的 10 个工具。

设计约束：
- JSON Schema 与 docs/03 §2 逐字段一致（tests/unit/agent/test_tools.py 有全量比对兜底）；
- handler 统一签名 async def(**kwargs) -> str | dict，永不抛异常，错误返回 {"error": "..."}；
- 读类输出带行号、超 200 行截断并提示续读区间（复用 audit.utils.truncate_lines 与
  workspace.read_lines）；
- 写出口收敛：record_issues（唯一问题写入口，做行号范围硬校验）、submit_patch（只返回
  接收确认，落库由集成层接手）、run_tests（白名单 pytest/jest，委托沙箱）。
"""

from __future__ import annotations

import difflib
import fnmatch
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from audit.agent.base import ToolSpec
from audit.models import Symbol
from audit.utils import truncate_lines
from audit.workspace import WorkspaceContext

__all__ = [
    "build_default_tools",
    "format_numbered_lines",
    "normalize_test_target",
    "validate_issue_payload",
    "READ_WINDOW_LINES",
    "SEARCH_MAX_RESULTS",
    "SEARCH_MAX_PATTERN_LEN",
    "LIST_MAX_RESULTS",
    "VALID_CATEGORIES",
    "VALID_SEVERITIES",
    "REQUIRED_ISSUE_FIELDS",
]

READ_WINDOW_LINES = 200  # 单次返回最大行数
SEARCH_MAX_RESULTS = 50  # 搜索最多返回条数
SEARCH_MAX_PATTERN_LEN = 500  # 正则长度上限（R1-12：防灾难性回溯的超大模式）
LIST_MAX_RESULTS = 500  # 列目录最多返回条数
MAX_SEARCH_FILE_BYTES = 1_000_000  # 搜索时跳过超 1MB 的文件

VALID_CATEGORIES = {"bug", "performance", "style", "security"}
VALID_SEVERITIES = {"critical", "high", "medium", "low"}
REQUIRED_ISSUE_FIELDS = (
    "category",
    "severity",
    "title",
    "file",
    "line_start",
    "line_end",
    "description",
    "suggestion",
    "confidence",
)

Handler = Callable[..., Any]


# ---------------------------------------------------------------------- 公共小工具


def format_numbered_lines(lines: list[str], start: int = 1) -> str:
    """把行列表渲染为 '12: code' 形式（1-based 行号）。"""
    return "\n".join(f"{i}: {line}" for i, line in enumerate(lines, start=start))


def normalize_rel_path(path: Any) -> str | None:
    """规范化 LLM 给出的相对路径；非法（绝对路径/盘符/.. 逃逸）返回 None。"""
    text = str(path or "").replace("\\", "/").strip()
    if not text:
        return None
    pp = PurePosixPath(text)
    if pp.is_absolute() or (len(text) > 1 and text[1] == ":") or ".." in pp.parts:
        return None
    return pp.as_posix()


def validate_issue_payload(issue: Any, line_count_of: Callable[[str], int | None]) -> str | None:
    """校验单条 issue 载荷；返回错误文本，合法返回 None。

    行号硬校验（任务书 C）：0 < line_start <= 文件行数。
    """
    if not isinstance(issue, dict):
        return "issue 必须是对象"
    missing = [f for f in REQUIRED_ISSUE_FIELDS if f not in issue]
    if missing:
        return f"缺少必填字段: {missing}"
    if issue["category"] not in VALID_CATEGORIES:
        return f"非法 category: {issue['category']!r}（可选: {sorted(VALID_CATEGORIES)}）"
    if issue["severity"] not in VALID_SEVERITIES:
        return f"非法 severity: {issue['severity']!r}（可选: {sorted(VALID_SEVERITIES)}）"
    line_start = issue["line_start"]
    line_end = issue["line_end"]
    if not isinstance(line_start, int) or isinstance(line_start, bool) or line_start <= 0:
        return f"line_start 必须为正整数，收到 {line_start!r}"
    if not isinstance(line_end, int) or isinstance(line_end, bool) or line_end < line_start:
        return f"line_end 必须 >= line_start，收到 {line_end!r}"
    total = line_count_of(str(issue["file"]))
    if total is None:
        return f"文件不存在或不可读: {issue['file']}"
    if line_start > total:
        return f"line_start={line_start} 超出文件行数 {total}"
    return None


# ---------------------------------------------------------------------- JSON Schema（docs/03 §2 逐字段）

SCHEMA_LIST_FILES = {
    "type": "object",
    "properties": {
        "prefix": {"type": "string", "description": "目录前缀，如 'app/services'"},
        "pattern": {"type": "string", "description": "glob 模式，如 '*.py'"},
    },
    "required": [],
}

SCHEMA_READ_FILE = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "相对项目根的路径"},
        "start_line": {"type": "integer", "description": "起始行（1-based，含）"},
        "end_line": {"type": "integer", "description": "结束行（含），单次最大 200 行"},
    },
    "required": ["path"],
}

SCHEMA_SEARCH_CODE = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "正则表达式或普通文本"},
        "file_glob": {"type": "string", "description": "可选，限定文件范围如 'app/**/*.py'"},
        "is_regex": {"type": "boolean", "default": True},
    },
    "required": ["query"],
}

SCHEMA_GET_SYMBOL = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "符号名，如 'get_user' 或 'UserService.create'"},
        "file_hint": {"type": "string", "description": "可选，限定定义所在文件"},
    },
    "required": ["name"],
}

SCHEMA_FIND_REFERENCES = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "file_hint": {"type": "string", "description": "定义所在文件，消歧同名符号"},
    },
    "required": ["name"],
}

SCHEMA_GET_CALL_CHAIN = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string"},
        "direction": {"type": "string", "enum": ["callers", "callees"], "default": "callees"},
        "depth": {"type": "integer", "minimum": 1, "maximum": 2, "default": 1},
    },
    "required": ["symbol"],
}

SCHEMA_GET_DEPENDENCIES = {
    "type": "object",
    "properties": {
        "target": {"type": "string", "description": "文件路径或模块名"},
        "direction": {"type": "string", "enum": ["imports", "imported_by"], "default": "imports"},
    },
    "required": ["target"],
}

SCHEMA_RECORD_ISSUES = {
    "type": "object",
    "properties": {
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": ["bug", "performance", "style", "security"]},
                    "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                    "title": {"type": "string", "description": "一句话问题标题（中文）"},
                    "file": {"type": "string"},
                    "line_start": {"type": "integer"},
                    "line_end": {"type": "integer"},
                    "description": {"type": "string", "description": "为什么是问题、触发条件、后果（中文）"},
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "证据，如 'orders.py:88 调用 get_user；users.py:41 return None'",
                    },
                    "suggestion": {"type": "string", "description": "修复建议（中文）"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": [
                    "category",
                    "severity",
                    "title",
                    "file",
                    "line_start",
                    "line_end",
                    "description",
                    "suggestion",
                    "confidence",
                ],
            },
        }
    },
    "required": ["issues"],
}

SCHEMA_SUBMIT_PATCH = {
    "type": "object",
    "properties": {
        "issue_id": {"type": "string"},
        "diff": {"type": "string", "description": "unified diff，含 ---/+++ 头与 @@ hunk"},
        "rationale": {"type": "string", "description": "修复思路说明（中文，将写入报告）"},
    },
    "required": ["issue_id", "diff", "rationale"],
}

SCHEMA_RUN_TESTS = {
    "type": "object",
    "properties": {
        "target": {"type": "string", "description": "测试文件/用例路径，空为全部"},
        "framework": {"type": "string", "enum": ["pytest", "jest"]},
    },
    "required": ["framework"],
}


# ---------------------------------------------------------------------- 工具构造


def build_default_tools(
    workspace: WorkspaceContext,
    index: Any = None,
    sandbox: Any = None,
) -> list[ToolSpec]:
    """构建 10 个默认工具；index/sandbox 缺失时对应工具返回 error（不缺失功能位）。"""
    return [
        _list_files_tool(workspace),
        _read_file_tool(workspace),
        _search_code_tool(workspace),
        _get_symbol_tool(workspace, index),
        _find_references_tool(workspace, index),
        _get_call_chain_tool(index),
        _get_dependencies_tool(index),
        _record_issues_tool(workspace),
        _submit_patch_tool(),
        _run_tests_tool(workspace, sandbox),
    ]


def _line_count_safe(workspace: WorkspaceContext, rel_path: str) -> int | None:
    try:
        return workspace.line_count(rel_path)
    except OSError:
        return None


def _list_files_tool(workspace: WorkspaceContext) -> ToolSpec:
    async def handler(prefix: str = "", pattern: str = "") -> dict[str, Any]:
        norm_prefix = ""
        if prefix:
            norm_prefix = PurePosixPath(str(prefix).replace("\\", "/")).as_posix().strip("/")
            if not norm_prefix or norm_prefix.startswith("..") or norm_prefix.startswith("/"):
                return {"error": f"非法 prefix: {prefix}"}
        files: list[str] = []
        for path in workspace.source_files():
            rel = path.relative_to(workspace.src_root).as_posix()
            if norm_prefix and not (rel == norm_prefix or rel.startswith(norm_prefix + "/")):
                continue
            if pattern and not fnmatch.fnmatch(rel, pattern):
                continue
            files.append(rel)
        files.sort()
        truncated = len(files) > LIST_MAX_RESULTS
        result: dict[str, Any] = {"files": files[:LIST_MAX_RESULTS], "count": len(files)}
        if truncated:
            result["hint"] = f"结果过多，仅返回前 {LIST_MAX_RESULTS} 条；请用 prefix/pattern 收窄范围"
        return result

    return ToolSpec(
        name="list_files",
        description="列出项目文件。可按目录前缀或 glob 模式过滤。返回相对路径列表。",
        parameters=SCHEMA_LIST_FILES,
        handler=handler,
    )


def _read_file_tool(workspace: WorkspaceContext) -> ToolSpec:
    async def handler(path: str | None = None, start_line: int = 0, end_line: int = 0) -> dict[str, Any]:
        rel = normalize_rel_path(path)
        if rel is None:
            return {"error": f"非法路径: {path!r}（必须是相对项目根的路径，不允许 .. 逃逸）"}
        try:
            text = workspace.read_file_text(rel)
        except OSError as exc:
            return {"error": f"文件不存在或不可读: {rel} ({type(exc).__name__})"}
        total = len(text.splitlines())
        if total == 0:
            return {"path": rel, "total_lines": 0, "start_line": 0, "end_line": 0, "content": "（空文件）"}
        s = int(start_line or 0) or 1
        e = int(end_line or 0) or total
        if s < 1 or s > total:
            return {"error": f"start_line={s} 超出范围（文件共 {total} 行）"}
        e = min(max(e, s), total)
        window = workspace.read_lines(rel, s, e)
        had_marker = len(window) > READ_WINDOW_LINES  # truncate_lines 截断时追加标记行
        capped = truncate_lines(window, READ_WINDOW_LINES)
        body = capped[:-1] if had_marker else capped
        end_shown = s + len(body) - 1
        content = format_numbered_lines(body, start=s)
        if had_marker:
            next_start = end_shown + 1
            continue_hint = (
                f"文件共 {total} 行，本次显示第 {s}-{end_shown} 行；"
                f"续读请调用 read_file(path={rel!r}, start_line={next_start}, "
                f"end_line={min(total, next_start + READ_WINDOW_LINES - 1)})"
            )
            content += f"\n{capped[-1]}"  # truncate_lines 的 "...[截断，共 N 行]" 标记
            content += f"\n[提示] {continue_hint}"
        result: dict[str, Any] = {
            "path": rel,
            "total_lines": total,
            "start_line": s,
            "end_line": end_shown,
            "truncated": had_marker,
            "content": content,
        }
        if had_marker:
            result["hint"] = continue_hint  # type: ignore[possibly-undefined]
        return result

    return ToolSpec(
        name="read_file",
        description="读取文件内容，每行前带行号（'12: code'）。大文件自动截断到 200 行，返回值会提示剩余区间。",
        parameters=SCHEMA_READ_FILE,
        handler=handler,
    )


def _search_code_tool(workspace: WorkspaceContext) -> ToolSpec:
    async def handler(query: str | None = None, file_glob: str = "", is_regex: bool = True) -> dict[str, Any]:
        if not str(query or "").strip():
            return {"error": "query 不能为空"}
        if len(str(query)) > SEARCH_MAX_PATTERN_LEN:
            return {"error": f"query 过长（>{SEARCH_MAX_PATTERN_LEN} 字符），请缩短后重试"}
        try:
            pattern = re.compile(str(query) if is_regex else re.escape(str(query)))
        except re.error as exc:
            return {"error": f"正则表达式非法: {exc}"}
        matches: list[str] = []
        truncated = False
        for path in workspace.source_files():
            rel = path.relative_to(workspace.src_root).as_posix()
            if file_glob and not fnmatch.fnmatch(rel, file_glob):
                continue
            try:
                if path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
                text = workspace.read_file_text(rel)
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    matches.append(f"{rel}:{i}: {line.strip()[:300]}")
                    if len(matches) >= SEARCH_MAX_RESULTS:
                        truncated = True
                        break
            if truncated:
                break
        result: dict[str, Any] = {"query": str(query), "matches": matches, "count": len(matches)}
        if truncated:
            result["hint"] = f"命中过多，仅返回前 {SEARCH_MAX_RESULTS} 条；请收窄 query 或用 file_glob 限定范围"
        return result

    return ToolSpec(
        name="search_code",
        description="全库正则/文本搜索，返回 'file:line: 匹配行' 列表（最多 50 条）。用于确认某模式的所有出现位置。",
        parameters=SCHEMA_SEARCH_CODE,
        handler=handler,
    )


def _symbol_to_dict(sym: Symbol) -> dict[str, Any]:
    return {
        "name": sym.name,
        "file": sym.file,
        "kind": sym.kind,
        "line_start": sym.line_start,
        "line_end": sym.line_end,
        "signature": sym.signature,
    }


def _candidate_names(index: Any, name: str, limit: int = 8) -> list[str]:
    """找不到精确符号时给近似候选名（difflib 相似度 + 包含关系）。"""
    try:
        all_syms = index.all_symbols()
    except Exception:
        return []
    names: list[str] = []
    for s in all_syms:
        if s.name not in names:
            names.append(s.name)
    simple = str(name).split(".")[-1]
    close = difflib.get_close_matches(simple, names, n=limit, cutoff=0.6)
    for cand in names:
        if len(close) >= limit:
            break
        if (simple in cand or cand in str(name)) and cand not in close:
            close.append(cand)
    return close[:limit]


def _get_symbol_tool(workspace: WorkspaceContext, index: Any) -> ToolSpec:
    async def handler(name: str | None = None, file_hint: str = "") -> dict[str, Any]:
        if not str(name or "").strip():
            return {"error": "name 不能为空"}
        if index is None:
            return {"error": "index not built"}
        try:
            sym = index.get_symbol(str(name), file_hint or None)
        except Exception as exc:
            return {"error": f"索引查询失败: {type(exc).__name__}: {exc}"}
        if sym is None:
            return {
                "error": f"symbol not found: {name}",
                "candidates": _candidate_names(index, str(name)),
            }
        out = _symbol_to_dict(sym)
        try:
            lines = workspace.read_lines(sym.file, sym.line_start, sym.line_end)
            out["source"] = format_numbered_lines(truncate_lines(lines, READ_WINDOW_LINES), start=sym.line_start)
        except OSError:
            out["source"] = ""
        return out

    return ToolSpec(
        name="get_symbol",
        description="按名称取代码符号（函数/类/方法）的定义：源码、签名、所在文件与行号。找不到时返回近似候选名。",
        parameters=SCHEMA_GET_SYMBOL,
        handler=handler,
    )


def _find_references_tool(workspace: WorkspaceContext, index: Any) -> ToolSpec:
    async def handler(name: str | None = None, file_hint: str = "") -> dict[str, Any]:
        if not str(name or "").strip():
            return {"error": "name 不能为空"}
        if index is None:
            return {"error": "index not built"}
        try:
            refs = index.references(str(name), file_hint or None)
        except Exception as exc:
            return {"error": f"索引查询失败: {type(exc).__name__}: {exc}"}
        return {
            "name": str(name),
            "references": [f"{r.file}:{r.line}: {r.snippet}" for r in refs],
            "count": len(refs),
        }

    return ToolSpec(
        name="find_references",
        description="查找符号在项目中的全部引用位置（调用点），基于预建调用图，未解析引用以搜索兜底。用于评估影响面与取证。",
        parameters=SCHEMA_FIND_REFERENCES,
        handler=handler,
    )


def _get_call_chain_tool(index: Any) -> ToolSpec:
    async def handler(symbol: str | None = None, direction: str = "callees", depth: int = 1) -> dict[str, Any]:
        if not str(symbol or "").strip():
            return {"error": "symbol 不能为空"}
        if index is None:
            return {"error": "index not built"}
        if direction not in ("callers", "callees"):
            return {"error": f"非法 direction: {direction!r}（可选: callers / callees）"}
        try:
            d = max(1, min(2, int(depth)))
        except (TypeError, ValueError):
            return {"error": f"非法 depth: {depth!r}（1~2）"}
        try:
            chains = index.call_chain(str(symbol), direction, d)
        except Exception as exc:
            return {"error": f"索引查询失败: {type(exc).__name__}: {exc}"}
        return {"symbol": str(symbol), "direction": direction, "depth": d, "chains": list(chains), "count": len(chains)}

    return ToolSpec(
        name="get_call_chain",
        description="查询某函数向上（谁调它）或向下（它调谁）两跳的调用链，输出 'A.f → B.g' 路径列表。",
        parameters=SCHEMA_GET_CALL_CHAIN,
        handler=handler,
    )


def _get_dependencies_tool(index: Any) -> ToolSpec:
    async def handler(target: str | None = None, direction: str = "imports") -> dict[str, Any]:
        if not str(target or "").strip():
            return {"error": "target 不能为空"}
        if index is None:
            return {"error": "index not built"}
        if direction not in ("imports", "imported_by"):
            return {"error": f"非法 direction: {direction!r}（可选: imports / imported_by）"}
        try:
            deps = index.dependencies(str(target), direction)
        except Exception as exc:
            return {"error": f"索引查询失败: {type(exc).__name__}: {exc}"}
        return {"target": str(target), "direction": direction, "dependencies": list(deps), "count": len(deps)}

    return ToolSpec(
        name="get_dependencies",
        description="查询文件或模块的导入依赖与被依赖关系。",
        parameters=SCHEMA_GET_DEPENDENCIES,
        handler=handler,
    )


def _record_issues_tool(workspace: WorkspaceContext) -> ToolSpec:
    async def handler(issues: list[Any] | None = None) -> dict[str, Any]:
        if not isinstance(issues, list):
            return {"error": "issues 必须是数组（无问题时提交空数组）"}
        details: list[str] = []
        for i, item in enumerate(issues):
            msg = validate_issue_payload(item, lambda p: _line_count_safe(workspace, p))
            if msg:
                details.append(f"issues[{i}]: {msg}")
        if details:
            return {"error": "record_issues 校验失败，未记录任何问题", "details": details}
        return {
            "ok": True,
            "recorded": len(issues),
            "message": (
                "问题已接收，落库由集成层处理" if issues else "已确认：本文件无问题"
            ),
        }

    return ToolSpec(
        name="record_issues",
        description="提交本文件审查结论。必须恰好调用一次：有问题提交问题数组，无问题提交空数组。行号必须来自你读到的文件内容，禁止猜测。",
        parameters=SCHEMA_RECORD_ISSUES,
        handler=handler,
    )


def _submit_patch_tool() -> ToolSpec:
    async def handler(issue_id: str | None = None, diff: str | None = None, rationale: str | None = None) -> dict[str, Any]:
        if not str(issue_id or "").strip():
            return {"error": "issue_id 不能为空"}
        diff_text = str(diff or "")
        if "@@" not in diff_text:
            return {"error": "diff 必须是 unified diff 格式（含 @@ hunk，git apply 可解析）"}
        if not str(rationale or "").strip():
            return {"error": "rationale 不能为空"}
        return {
            "ok": True,
            "issue_id": str(issue_id),
            "status": "received",
            "message": "补丁已接收，落库与应用（git apply --check）由集成层处理",
        }

    return ToolSpec(
        name="submit_patch",
        description="提交针对单个 Issue 的修复，unified diff 格式（git apply 可解析）。只允许改动与该 Issue 直接相关的行。",
        parameters=SCHEMA_SUBMIT_PATCH,
        handler=handler,
    )


def normalize_test_target(workspace: WorkspaceContext, target: str) -> str | None:
    """把 run_tests 的 target 归一为 src_root 内的相对 posix 路径（R1-13）。

    返回：
    - ""：空 target（跑全部测试，合法）；
    - 非空字符串：src_root 内相对路径（绝对路径先归一；越出 src_root 判非法）；
    - None：非法（'-' 前缀的 pytest 选项注入、.. 越级、越出工作副本）。
    """
    text = str(target or "").replace("\\", "/").strip()
    if not text:
        return ""
    if text.startswith("-"):
        return None  # 拒绝 pytest/jest 选项注入（如 -p no:cacheprovider / --import-mode）
    path = Path(text)
    if path.is_absolute():
        try:
            rel = path.resolve().relative_to(Path(workspace.src_root).resolve())
        except ValueError:
            return None
    else:
        rel = Path(text.strip("/"))
        if ".." in rel.parts:
            return None
    posix = rel.as_posix()
    if not posix or posix == ".":
        return ""
    return posix


def _run_tests_tool(workspace: WorkspaceContext, sandbox: Any) -> ToolSpec:
    async def handler(framework: str | None = None, target: str = "") -> dict[str, Any]:
        fw = str(framework or "").lower()
        if fw not in ("pytest", "jest"):
            return {"error": f"不支持的测试框架: {framework!r}（白名单: pytest / jest）"}
        if sandbox is None:
            return {"error": "sandbox not available"}
        rel_target = normalize_test_target(workspace, str(target or ""))
        if rel_target is None:
            return {"error": f"target 非法：{target!r}（必须是工作副本内的相对测试路径，禁止 '-' 开头的选项）"}
        try:
            res = await sandbox.run_tests(fw, workspace.src_root, rel_target)
        except Exception as exc:
            return {"error": f"沙箱执行失败: {type(exc).__name__}: {exc}"}
        return {
            "framework": fw,
            "target": rel_target,
            "exit_code": res.exit_code,
            "stdout_tail": res.stdout_tail,
            "stderr_tail": res.stderr_tail,
            "timed_out": res.timed_out,
            "duration_sec": res.duration_sec,
        }

    return ToolSpec(
        name="run_tests",
        description="在沙箱中运行测试（白名单：pytest / jest）。返回退出码与末尾 80 行输出。60 秒超时。",
        parameters=SCHEMA_RUN_TESTS,
        handler=handler,
    )
