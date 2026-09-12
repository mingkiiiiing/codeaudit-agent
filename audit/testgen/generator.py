"""TestGen 生成器（W2-A2 Stage6 单测生成闭环；W7-A3 扩展 JS/TS）。

职责边界（只依赖 Wave 1 契约，不重写任何 Wave 1 成果）：
- select_targets：目标函数 (file, symbol) 三来源按优先级选取
  （verified Patch hunk → ctx.extra["testgen_targets"] → critical/high Issue 文件首切片）；
- generate_tests：按 docs/03 §3.4 TestGen Agent 规范构造 prompt 并调用 LLM
  （Python→pytest；W7-A3 JS/TS→node:test 变体），UNTESTABLE 判定 → None；
  代码用 ```python / ```javascript 围栏正则抽取；解析失败带报错回喂重试 1 次；
- count_asserts：Python 用 ast.parse 统计 Assert 节点数（解析失败返回 0）；
  JS/TS 用文本启发式（"assert." 出现次数）；
- validate_code：静态校验生成代码（可解析 / assert ≥1 / 危险调用黑名单 / 越界写入启发）；
  validate_code_js：JS 分支（ESM 守卫 / assert 启发式 / 第三方框架黑名单 /
  node --check 语法校验——node 不可用则跳过并返回 syntax_checked=False 供标注）。

安全边界：validate_code / validate_code_js 是写入 tests/generated/ 前的硬闸门，
任何校验错误都会让该版本代码被判非法（由 stage 记一次失败并回喂重试）。
"""

from __future__ import annotations

import ast
import contextlib
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
from typing import Any

from audit.models import FunctionSlice
from audit.workspace import WorkspaceContext

__all__ = [
    "select_targets",
    "generate_tests",
    "count_asserts",
    "validate_code",
    "validate_code_js",
    "js_import_specifier",
    "parse_hunks",
]

_MAX_DEPENDENCY_BLOCKS = 3
_MAX_DEPENDENCY_LINES = 60
_MAX_FALLBACK_FILE_LINES = 200
_JS_CHECK_TIMEOUT_SEC = 20.0

# ---------------------------------------------------------------- 常量

_SEVERITY_VALUES = {"critical", "high"}

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_CODE_FENCE_RE = re.compile(r"```(?:python)?[ \t]*\r?\n(.*?)\r?\n?```", re.DOTALL)
# JS/TS 围栏：接受 javascript/js/jsx/typescript/ts/mjs 标签或无标签围栏
_CODE_FENCE_JS_RE = re.compile(
    r"```(?:javascript|js|jsx|typescript|ts|mjs)?[ \t]*\r?\n(.*?)\r?\n?```", re.DOTALL
)

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_IDENT_STOPWORDS = {
    "return", "none", "true", "false", "self", "this", "from", "import", "def",
    "class", "and", "not", "with", "for", "while", "else", "elif", "try", "except",
    "raise", "yield", "lambda", "print", "len", "str", "int", "float", "bool",
    "list", "dict", "set", "tuple", "range", "type", "isinstance", "getattr",
    "setattr", "hasattr", "super", "assert", "async", "await", "append", "items",
    "keys", "values", "data", "result", "value", "name", "path", "error", "test",
}

# 生成代码的黑名单 token（出现即非法）：危险执行 / 进程 / 删除 / 动态求值
_DANGEROUS_TOKENS = (
    "os.system",
    "os.popen",
    "os.remove",
    "os.unlink",
    "os.rename",
    "subprocess",
    "shutil",
    "eval(",
    "exec(",
    "__import__",
    "marshal.load",
    "pickle.load",
)

# JS 生成代码的黑名单（W7-A3）：动态执行 / 子进程；require( 由 .mjs ESM 守卫单独处理
_JS_DANGEROUS_TOKENS = (
    "child_process",
    "eval(",
    "new Function(",
    "process.binding",
)
# 仅允许 node 内置测试框架；以下第三方框架的 import 一律拒绝
_JS_FRAMEWORK_IMPORTS = (
    "from 'jest'", 'from "jest"',
    "from 'vitest'", 'from "vitest"',
    "from 'mocha'", 'from "mocha"',
    "from 'chai'", 'from "chai"',
    "from 'sinon'", 'from "sinon"',
    "from 'jasmine'", 'from "jasmine"',
    "@jest/globals",
)

_TESTGEN_SYSTEM_PROMPT = """你是测试工程师，为指定函数生成 pytest 回归测试。

要求：
1. ≥ 5 条用例：正常路径 ≥ 2、边界 ≥ 2（空输入/极值/空集合）、异常 ≥ 1。
2. 每条用例必须有显式 assert，断言值需你根据函数语义推演，禁止 assert True。
3. 不 mock 被测函数自身；仅对 IO/网络/时间等外部依赖使用 monkeypatch。
4. 每个测试函数的首行写注释 # kind: normal|boundary|error，标注该用例类别。
5. 直接输出完整测试代码（import 自包含，import 路径相对项目根，如 from app.utils.mathx import compare），
   用 ```python 围栏包裹，不要输出解释文字。
6. 若代码不可单测（强耦合全局状态无法隔离），输出 "# UNTESTABLE: 原因" 并说明。"""

_JS_TESTGEN_SYSTEM_PROMPT = """你是测试工程师，为指定 JavaScript/TypeScript 函数生成 node:test 回归测试。

要求：
1. 只用 Node.js 内置测试能力（零第三方依赖，禁止 jest/vitest/mocha/chai/sinon）：
   import { test } from 'node:test';
   import assert from 'node:assert';
2. ≥ 5 条用例：正常路径 ≥ 2、边界 ≥ 2（空输入/极值/空集合）、异常 ≥ 1。
3. 每条用例必须有显式断言（assert.strictEqual / assert.deepStrictEqual /
   assert.throws 等），断言值需你根据函数语义推演，禁止 assert(true)/assert.ok(true)。
4. 不 mock 被测函数自身；仅对 IO/网络/时间等外部依赖使用注入替换。
5. 生成文件是 .mjs（ESM 模块），必须使用 import 语法，禁止 require(...)。
6. 被测模块用用户给出的相对导入说明符导入（相对生成的测试文件解析）。
7. 每条用例的首行写注释 // kind: normal|boundary|error，标注该用例类别。
8. 用 ```javascript 围栏输出完整测试代码，不要输出解释文字。
9. 若代码不可单测（强耦合全局状态无法隔离），输出 "// UNTESTABLE: 原因" 并说明。"""


# ---------------------------------------------------------------- 目标选取


def _severity_value(issue: Any) -> str:
    """Issue.severity 兼容枚举/字符串两种形态。"""
    severity = getattr(issue, "severity", "")
    return severity.value if hasattr(severity, "value") else str(severity)


def _header_path(header: str) -> str | None:
    """从 '+++ b/path' / '--- a/path' 行提取路径；/dev/null 返回 None。"""
    body = header[4:].strip()
    if body.startswith('"') and body.endswith('"') and len(body) >= 2:
        body = body[1:-1]
    body = body.split("\t", 1)[0].strip()
    if body.startswith(("a/", "b/")):
        body = body[2:]
    return body or None


def parse_hunks(diff: str) -> dict[str, list[tuple[int, int]]]:
    """解析 unified diff，返回 {文件相对路径: [(新侧起始行, 新侧结束行), ...]}。

    只关心 +++ 侧（修复后的文件）；删除侧（/dev/null）与 count<=0 的空区间不产出。
    行区间为 1-based 闭区间，供与函数切片区间做相交判断。
    """
    result: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in str(diff or "").splitlines():
        if line.startswith("+++ "):
            path = _header_path(line)
            current = path if path else None
        elif line.startswith("@@ ") and current:
            match = _HUNK_RE.match(line)
            if not match:
                continue
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            if count <= 0:
                continue
            ranges = result.setdefault(current, [])
            ranges.append((start, start + count - 1))
    return result


def _slice_symbols_for_hunks(index: Any, file: str, ranges: list[tuple[int, int]]) -> list[str]:
    """取与 hunk 区间相交的切片符号；索引异常按空处理（不阻断目标选取）。"""
    try:
        slices: list[FunctionSlice] = list(index.slices_for_file(file) or [])
    except Exception:  # noqa: BLE001 —— 索引层异常不应阻断阶段
        return []
    symbols: list[str] = []
    for item in slices:
        start = int(getattr(item, "line_start", 0) or 0)
        end = int(getattr(item, "line_end", 0) or 0)
        symbol = str(getattr(item, "symbol", "") or "")
        if not symbol:
            continue
        if any(end >= lo and start <= hi for lo, hi in ranges) and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _targets_from_patches(ctx: Any) -> list[tuple[str, str]]:
    """来源①：verified Patch 的 hunk 区间 ∩ 索引切片；index 缺失时跳过该来源。"""
    index = getattr(ctx, "index", None)
    if index is None:
        return []
    targets: list[tuple[str, str]] = []
    for patch in getattr(ctx, "patches", None) or []:
        if getattr(patch, "apply_status", "") != "verified":
            continue
        for file, ranges in parse_hunks(getattr(patch, "diff", "") or "").items():
            for symbol in _slice_symbols_for_hunks(index, file, ranges):
                targets.append((file, symbol))
    return targets


def _targets_from_extra(ctx: Any) -> list[tuple[str, str]]:
    """来源②：ctx.extra['testgen_targets']（list[tuple[str, str]]），容错解析。"""
    raw = (getattr(ctx, "extra", None) or {}).get("testgen_targets") or []
    targets: list[tuple[str, str]] = []
    for item in raw:
        try:
            file, symbol = item
        except (TypeError, ValueError):
            continue
        if isinstance(file, str) and isinstance(symbol, str) and file and symbol:
            targets.append((file, symbol))
    return targets


def _targets_from_issues(ctx: Any) -> list[tuple[str, str]]:
    """来源③：critical/high Issue 涉及文件去重，每文件取首个切片符号（需索引）。"""
    index = getattr(ctx, "index", None)
    if index is None:
        return []
    files: list[str] = []
    for issue in getattr(ctx, "issues", None) or []:
        if _severity_value(issue) not in _SEVERITY_VALUES:
            continue
        file = str(getattr(issue, "file", "") or "").replace("\\", "/")
        if file and file not in files:
            files.append(file)
    targets: list[tuple[str, str]] = []
    for file in files:
        try:
            slices: list[FunctionSlice] = list(index.slices_for_file(file) or [])
        except Exception:  # noqa: BLE001
            slices = []
        symbol = str(getattr(slices[0], "symbol", "") or "") if slices else ""
        if symbol:
            targets.append((file, symbol))
    return targets


def select_targets(ctx: Any) -> list[tuple[str, str]]:
    """选取待生成单测的目标函数 (file, symbol)，三来源按优先级取首个非空者。

    ① ctx.patches 中 apply_status=="verified" 的 Patch → 解析 diff 的 @@ hunk 行区间
       → ctx.index.slices_for_file(file) 中区间相交的 symbol（index 为 None 时跳过）；
    ② ctx.extra["testgen_targets"]（list[tuple[str, str]]）；
    ③ 都没有 → ctx.issues 中 severity ∈ {critical, high} 的 issue.file 去重，
       每个文件取首个切片符号（需索引，无切片则跳过该文件）。

    结果去重排序，上限 ctx.config.testgen_max_functions。
    """
    targets = _targets_from_patches(ctx)
    if not targets:
        targets = _targets_from_extra(ctx)
    if not targets:
        targets = _targets_from_issues(ctx)
    unique = sorted(set(targets))
    limit = max(0, int(getattr(getattr(ctx, "config", None), "testgen_max_functions", 30) or 0))
    return unique[:limit]


# ---------------------------------------------------------------- prompt 构造


def _numbered(lines: list[str], start_line: int = 1) -> str:
    """给源码行附加真实行号（1-based），便于 LLM 对齐源码。"""
    return "\n".join(f"{no:4d}: {text}" for no, text in enumerate(lines, start_line))


def _target_block(workspace: WorkspaceContext, index: Any, file: str, symbol: str) -> str:
    """[target] 段：优先用索引切片（源码+签名）；无切片时回退读文件（截断保护）。"""
    if index is not None:
        try:
            slices = list(index.slices_for_file(file) or [])
        except Exception:  # noqa: BLE001
            slices = []
        for item in slices:
            if str(getattr(item, "symbol", "")) == symbol:
                code = str(getattr(item, "code", "") or "")
                signature = str(getattr(item, "signature", "") or "")
                start = int(getattr(item, "line_start", 1) or 1)
                end = int(getattr(item, "line_end", 1) or 1)
                body = _numbered(code.splitlines(), start) if code else "（切片内容为空）"
                return (
                    f"[target] {file} 的符号 {symbol}"
                    f"（第 {start}-{end} 行，行首为真实行号）\n"
                    f"签名：{signature or '（无）'}\n"
                    f"{body}"
                )
    # 回退：整文件注入（防超长截断），符号由 LLM 自行定位
    try:
        lines = workspace.read_file_text(file).splitlines()[:_MAX_FALLBACK_FILE_LINES]
    except (FileNotFoundError, OSError):
        lines = []
    body = _numbered(lines, 1) if lines else "（文件不可读）"
    return f"[target] {file} 的符号 {symbol}（整文件注入，行首为真实行号）\n{body}"


def _dependency_blocks(workspace: WorkspaceContext, index: Any, file: str, symbol: str, code: str) -> list[str]:
    """[dependencies] 段：从目标源码抽标识符，经索引取定义源码（≤3 块、每块 ≤60 行）。"""
    if index is None or not code:
        return []
    short_name = symbol.split(".")[-1]
    names: list[str] = []
    for token in _IDENT_RE.findall(code):
        if token.lower() in _IDENT_STOPWORDS or token == short_name or token == symbol or token in names:
            continue
        names.append(token)
    blocks: list[str] = []
    for name in names:
        if len(blocks) >= _MAX_DEPENDENCY_BLOCKS:
            break
        try:
            dep = index.get_symbol(name, file)
        except Exception:  # noqa: BLE001
            dep = None
        if dep is None or not str(getattr(dep, "file", "") or ""):
            continue
        start = int(getattr(dep, "line_start", 1) or 1)
        end = max(start, min(int(getattr(dep, "line_end", 1) or 1), start + _MAX_DEPENDENCY_LINES - 1))
        try:
            body = workspace.read_lines(str(dep.file), start, end)
        except (FileNotFoundError, OSError):
            continue
        if not body:
            continue
        blocks.append(
            f"{str(dep.file)} 的 {str(getattr(dep, 'name', name))}"
            f"（第 {start}-{end} 行）\n{_numbered(body, start)}"
        )
    return blocks


def js_import_specifier(file: str) -> str:
    """计算生成的 .test.mjs（位于 tests/generated/）导入目标模块的相对说明符。

    app/utils/mathx.js → ../../app/utils/mathx.js；store.js → ../../store.js。
    结果保证以 ./ 或 ../ 开头（ESM 相对导入的硬性要求）。
    """
    target = "/" + str(file).replace("\\", "/").lstrip("/")
    rel = posixpath.relpath(target, "/tests/generated")
    return rel if rel.startswith(".") else f"./{rel}"


def build_testgen_messages(
    workspace: WorkspaceContext,
    index: Any,
    file: str,
    symbol: str,
    error_context: str = "",
    language: str = "python",
) -> list[dict[str, str]]:
    """按 docs/03 §3.4 TestGen Agent 规范构造消息（要求首尾双写关键约束）。

    W7-A3：language 为 javascript/typescript 时切换 node:test 变体 prompt，
    并给出从生成文件（tests/generated/*.test.mjs）指向目标模块的 ESM 相对导入说明符。
    """
    is_js = language in ("javascript", "typescript")
    try:
        slices = list(index.slices_for_file(file) or []) if index is not None else []
    except Exception:  # noqa: BLE001
        slices = []
    target_code = ""
    for item in slices:
        if str(getattr(item, "symbol", "")) == symbol:
            target_code = str(getattr(item, "code", "") or "")
            break

    parts: list[str] = [
        _target_block(workspace, index, file, symbol),
    ]
    if is_js:
        parts += [
            "",
            "[import] 生成的测试文件位于 tests/generated/*.test.mjs（ESM），"
            f"请用以下说明符导入被测模块：import {{ ... }} from '{js_import_specifier(file)}';"
            "（被测模块为 CommonJS 时按其 module.exports 的键做命名导入）。",
        ]
    deps = _dependency_blocks(workspace, index, file, symbol, target_code)
    if deps:
        parts.append("")
        parts.append("[dependencies]")
        parts.extend(f"- {block}" for block in deps)
    if error_context:
        parts += [
            "",
            "[previous_failure] 上一版生成代码未通过（校验或沙箱运行失败），报错如下：",
            error_context,
            "请修正上述问题后重新生成完整测试代码。",
        ]
    if is_js:
        parts += [
            "",
            "再次强调：≥5 条用例（正常≥2/边界≥2/异常≥1）；只允许 node:test + node:assert，"
            "禁止第三方测试框架与 require(...)（生成文件是 .mjs）；"
            "每条用例首行注释 // kind: normal|boundary|error；"
            "必须显式 assert，禁止 assert(true)；"
            "被测模块用 import {...} from '" + js_import_specifier(file) + "' 导入；"
            "用 ```javascript 围栏输出完整代码。",
        ]
    else:
        parts += [
            "",
            "再次强调：≥5 条用例（正常≥2/边界≥2/异常≥1）；每个测试函数首行注释 "
            "# kind: normal|boundary|error；必须显式 assert，禁止 assert True；"
            "不 mock 被测函数自身；用 ```python 围栏输出完整代码。",
        ]
    return [
        {"role": "system", "content": _JS_TESTGEN_SYSTEM_PROMPT if is_js else _TESTGEN_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


# ---------------------------------------------------------------- 代码生成


def _extract_code(text: str) -> str:
    """从 LLM 输出中用 ```python 围栏正则抽取代码；无围栏返回空串。"""
    match = _CODE_FENCE_RE.search(text or "")
    return match.group(1).strip() if match else ""


def _extract_code_js(text: str) -> str:
    """从 LLM 输出中抽取 JS/TS 代码（```javascript/js/ts 等标签或无标签围栏）。"""
    match = _CODE_FENCE_JS_RE.search(text or "")
    return match.group(1).strip() if match else ""


async def generate_tests(
    llm: Any,
    workspace: WorkspaceContext,
    index: Any,
    file: str,
    symbol: str,
    error_context: str = "",
    language: str = "python",
) -> tuple[str, str] | None:
    """调用 LLM 为 (file, symbol) 生成测试代码。

    Python 目标生成 pytest 代码（```python 围栏）；W7-A3 JS/TS 目标生成
    node:test 代码（```javascript 等围栏）。

    Args:
        llm: LLMClient（生产为 GlmClient，测试为 FakeLLMClient）。
        workspace: 工作副本上下文。
        index: IndexStore | None，用于切片与依赖符号注入（可为 None）。
        file: 目标文件相对路径。
        symbol: 目标符号限定名。
        error_context: 上一轮失败的报错上下文（stage 重试时回喂），空则首次生成。
        language: 目标语言（python / javascript / typescript）。

    Returns:
        (code, symbol)；LLM 判定不可测（"UNTESTABLE:"）或两次输出均无法抽取代码时
        返回 None。
    """
    is_js = language in ("javascript", "typescript")
    fence_name = "```javascript" if is_js else "```python"
    extract = _extract_code_js if is_js else _extract_code
    messages = build_testgen_messages(workspace, index, file, symbol, error_context, language)
    response = await llm.chat(messages)
    last_error = ""
    for attempt in (0, 1):
        content = response.content or ""
        if "# UNTESTABLE:" in content or "// UNTESTABLE:" in content:
            return None
        code = extract(content)
        if code:
            return code, symbol
        last_error = f"未找到 {fence_name} 代码围栏或代码为空"
        if attempt == 0:  # 解析失败带报错回喂重试 1 次
            retry_messages = list(messages) + [
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": (
                        f"上一次输出无法解析（{last_error}）。请重新输出：用 {fence_name} 围栏"
                        "包裹完整测试代码，不要输出围栏以外的任何文字；"
                        "若确实不可单测，输出 \"// UNTESTABLE: 原因\"。"
                    ),
                },
            ]
            response = await llm.chat(retry_messages)
    return None


# ---------------------------------------------------------------- 静态校验


def count_asserts(code: str, language: str = "python") -> int:
    """统计代码中的断言数。

    Python：ast.parse 统计 Assert 节点数（语法解析失败返回 0）；
    JS/TS（W7-A3）：文本启发式——"assert." 出现次数（无需 JS 解析器）。
    """
    code = code or ""
    if language in ("javascript", "typescript"):
        return code.count("assert.")
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return 0
    return sum(1 for node in ast.walk(tree) if isinstance(node, ast.Assert))


def _abs_write_target(code: str) -> str | None:
    """简单启发：检出对项目外绝对路径的写入（open(path, 'w'/'a'/...)）。

    只处理字面量参数的 open 调用：路径以 '/'、'\\\\' 或盘符（如 C:/）开头且
    第二参含写模式字符 → 返回该路径描述；否则 None。
    """
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return None
    drive_re = re.compile(r"^[A-Za-z]:")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "open"):
            continue
        if len(node.args) < 2:
            continue
        target, mode = node.args[0], node.args[1]
        if not (isinstance(target, ast.Constant) and isinstance(target.value, str)):
            continue
        if not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)):
            continue
        path_value = target.value
        if mode.value and any(ch in mode.value for ch in "wax+") and (
            path_value.startswith(("/", "\\")) or drive_re.match(path_value)
        ):
            return f"open({path_value!r}, {mode.value!r})"
    return None


def validate_code(code: str) -> list[str]:
    """静态校验 LLM 生成的测试代码；返回错误列表（空列表 = 合法）。

    规则：
    - 必须 ast.parse 通过；
    - 至少 1 条显式 assert；
    - 禁止 assert True（占位断言）；
    - 黑名单：os.system / subprocess / shutil / eval( / exec( / __import__ 等
      危险调用与模块（含 import 语句形式，如 from subprocess import run）；
    - 简单启发：禁止对被测项目外的绝对路径执行写入（open 字面量参数 + 写模式）。
    """
    code = code or ""
    if not code.strip():
        return ["生成代码为空"]
    errors: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"语法解析失败：{exc.msg}（第 {exc.lineno} 行）"]
    except ValueError:
        return ["语法解析失败：代码含空字节等非法内容"]

    if not any(isinstance(node, ast.Assert) for node in ast.walk(tree)):
        errors.append("缺少显式 assert（至少 1 条）")

    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            test = node.test
            if isinstance(test, ast.Constant) and test.value is True:
                errors.append("禁止 assert True（占位断言）")
                break

    for token in _DANGEROUS_TOKENS:
        if token in code:
            errors.append(f"包含被禁止的调用/模块：{token.rstrip('(')}")

    write_target = _abs_write_target(code)
    if write_target:
        errors.append(f"疑似对被测项目外的路径执行写入：{write_target}")
    return errors


_JS_REQUIRE_RE = re.compile(r"\brequire\s*\(")
_JS_ASSERT_TRUE_RE = re.compile(r"assert(?:\s*\.\s*ok)?\s*\(\s*true\s*[,)]", re.IGNORECASE)


def _js_node_syntax_check(code: str) -> tuple[bool, str, bool]:
    """用 node --check 对生成代码做语法校验（.mjs 模块语义）。

    Returns:
        (语法是否通过, 失败详情, 是否真的执行了校验)。node 不可用时
        返回 (True, "", False)——跳过语法校验（诚实降级，由调用方标注）。
    """
    node = shutil.which("node")
    if not node:
        return True, "", False
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".mjs", encoding="utf-8", newline="\n", delete=False
        ) as handle:
            handle.write(code)
            tmp_path = handle.name
        proc = subprocess.run(  # noqa: S603 —— 固定命令列表，无 shell
            [node, "--check", tmp_path],
            capture_output=True,
            timeout=_JS_CHECK_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"node --check 执行异常: {type(exc).__name__}: {exc}", True
    finally:
        if tmp_path:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
        return False, detail.strip()[:200], True
    return True, "", True


def validate_code_js(code: str) -> tuple[list[str], bool]:
    """静态校验 LLM 生成的 JS 测试代码（node:test 风格，输出为 .mjs）。

    Returns:
        (错误列表, 语法是否经过 node --check 真实校验)。错误列表为空 = 合法；
        node 不可用时跳过语法校验，syntax_checked=False 供调用方标注。

    规则：
    - 非空；
    - ESM 守卫：禁止 require(...)（生成文件是 .mjs，require 运行时直接 ReferenceError）；
    - assert 计数启发式：至少 1 处 "assert."；
    - 禁止 assert(true)/assert.ok(true) 占位断言；
    - 黑名单：child_process / eval( / new Function( / 第三方测试框架 import；
    - node --check 语法校验（node 不可用则跳过）。
    """
    code = code or ""
    if not code.strip():
        return ["生成代码为空"], False
    errors: list[str] = []
    if _JS_REQUIRE_RE.search(code):
        errors.append("生成文件为 .mjs（ESM 模块），禁止 require(...)：请改用 import 语法")
    if code.count("assert.") == 0:
        errors.append("缺少显式 assert（至少 1 处 assert.xxx）")
    if _JS_ASSERT_TRUE_RE.search(code):
        errors.append("禁止 assert(true)/assert.ok(true)（占位断言）")
    for token in _JS_FRAMEWORK_IMPORTS:
        if token in code:
            errors.append(f"依赖第三方测试框架：{token}（仅允许 node:test 内置框架）")
    for token in _JS_DANGEROUS_TOKENS:
        if token in code:
            errors.append(f"包含被禁止的调用/模块：{token.rstrip('(')}")
    syntax_ok, detail, syntax_checked = _js_node_syntax_check(code)
    if syntax_checked and not syntax_ok:
        errors.append(f"语法校验失败（node --check）：{detail}")
    return errors, syntax_checked
