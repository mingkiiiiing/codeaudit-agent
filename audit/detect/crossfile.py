"""W16 全库后处理扫描器：跨文件/同文件重复代码（克隆）检测。

对外契约（audit.detect.engine._post_scan_issues 延迟导入本模块）：
- run(ctx) -> list[Issue]；永不抛异常——内部失败写
  ctx.extra["post_scan_errors"]["audit.detect.crossfile"] 后返回空列表；
- Issue 不填 id（engine 统一补号），category=STYLE、severity=MEDIUM、
  confidence=0.7、source=RULE，evidence 携带 "rule:PY-CLONE" 等规则 id。

算法（O(n) 级，性能红线：12 万行项目 <5s）：
1. 逐文件行归一化：字符串字面量->STR、数字->NUM、去行内注释（python 用 #，
   js/ts 简单处理行内 // 与 /* */，含跨行块注释状态）、空白折叠；空行与
   花括号行保留结构；
2. 对归一化行哈希序列做 K 行窗口多项式滚动哈希（K=CLONE_MIN_LINES，默认 6，
   可用环境变量 CODEAUDIT_CLONE_MIN_LINES 覆盖），前缀和 O(n) 求全部窗口指纹；
3. 全局 dict[窗口指纹 -> 位置列表]：同指纹多位置即克隆候选；同文件相邻窗口先
   折叠为块代表位置（同一片段只保留首个代表，防同块重复配对），再逐行比对
   确认（防哈希碰撞）并向两侧扩展到最大相同片段；块两端的空行 padding 最后
   修剪掉再按阈值判断（防止"短代码 + 空行"被凑满 K 行误报）；
4. 每对报 1 条 Issue 挂在第二处（后续）位置；同一第二处 start 行只报最优
   （最长）对；
5. Type-2 二级匹配（纯新增通道，不改 Type-1 任何产出）：Type-1 命中之后，
   对未命中的行区间做第二级归一化（标识符 -> ID：def 名/参数名/局部变量/
   属性名均折叠，关键字与内置名保留），窗口指纹/块折叠/扩展与 Type-1 同
   口径复用；连续 >= T2_MIN_LINES（env CODEAUDIT_T2_MIN_LINES，默认 8，比
   Type-1 的 6 更严）行、位于不同函数之间且未被 Type-1 覆盖的片段报
   Type-2：severity=LOW、confidence=0.5、evidence 加 "type:2"，提示人工
   确认；同一函数体内部的自相似不报。

性能护栏：
- 全库总行数 > MAX_TOTAL_LINES（300_000）时整体跳过并记 post_scan_errors；
- 单一窗口指纹组的块代表位置数 > MAX_GROUP_REPS 时跳过该组（片段海量重复时
  两两配对会组合爆炸，宁可漏报，保 O(n)）。

已知局限：python 三引号跨行字符串与 js/ts 跨行模板字符串不做跨行处理（仅
同行闭合的参与归一化）；短于 K 行（默认 6 行）/T2 阈值（默认 8 行）的重复
不报；Type-2 仅作疑似线索（标识符折叠口径较宽，误报率高于 Type-1）。
"""

from __future__ import annotations

import builtins
import keyword
import os
import re
from typing import Any

from audit.models import Category, Issue, IssueSource, Severity
from audit.utils import guess_language, truncate

MODULE_NAME = "audit.detect.crossfile"

# 克隆判定的最小归一化行数（环境变量 CODEAUDIT_CLONE_MIN_LINES 可覆盖）
DEFAULT_CLONE_MIN_LINES = 6
CLONE_MIN_LINES_ENV = "CODEAUDIT_CLONE_MIN_LINES"
# 性能护栏：全库总行数上限，超过则跳过本扫描器
MAX_TOTAL_LINES = 300_000
# 单一窗口指纹组的块代表位置数上限（防 O(n^2) 组合爆炸）
MAX_GROUP_REPS = 64
# Type-2 二级匹配最小行数：比 Type-1（6）更严，env CODEAUDIT_T2_MIN_LINES 可覆盖
DEFAULT_T2_MIN_LINES = 8
T2_MIN_LINES_ENV = "CODEAUDIT_T2_MIN_LINES"

_CONFIDENCE = 0.7
_T2_CONFIDENCE = 0.5
_SNIPPET_MAX_CHARS = 2000

_RULE_BY_LANG = {
    "python": "PY-CLONE",
    "javascript": "JS-CLONE",
    "typescript": "TS-CLONE",
}

# 单行字符串字面量（含 r/b/f 等短前缀；js/ts 模板字符串仅处理单行闭合情形）
_STRING_RES = (
    re.compile(r"[A-Za-z]{0,2}'''(?:[^'\\]|\\.)*'''"),
    re.compile(r'[A-Za-z]{0,2}"""(?:[^"\\]|\\.)*"""'),
    re.compile(r"[A-Za-z]{0,2}'(?:[^'\\\n]|\\.)*'"),
    re.compile(r'[A-Za-z]{0,2}"(?:[^"\\\n]|\\.)*"'),
    re.compile(r"`(?:[^`\\]|\\.)*`"),
)
# 数字字面量（负向断言避开标识符内的数字：data2 / op_00 不受影响）
_NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")
# js/ts 同行闭合的块注释
_INLINE_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/")

# ---------------------------------------------------------- Type-2 标识符折叠
# 保留名集合：关键字与内置名不折叠（不同内置/关键字仍造成指纹差异，抑制误报）。
# python 用标准库 keyword/builtins 全集；js/ts 离线硬编码关键字 + 常见全局对象。
_PY_KEEP_NAMES = frozenset(keyword.kwlist) | frozenset(keyword.softkwlist) | frozenset(
    dir(builtins)
)
_JS_TS_KEEP_NAMES = frozenset(
    {
        "abstract", "any", "as", "async", "await", "bigint", "boolean", "break",
        "case", "catch", "class", "const", "continue", "debugger", "declare",
        "default", "delete", "do", "else", "enum", "export", "extends", "false",
        "finally", "for", "from", "function", "get", "if", "implements", "import",
        "in", "instanceof", "interface", "let", "never", "new", "null", "number",
        "object", "of", "private", "protected", "public", "readonly", "return",
        "set", "static", "string", "super", "switch", "symbol", "this", "throw",
        "true", "try", "type", "typeof", "undefined", "unknown", "var", "void",
        "while", "yield",
        # 常见全局对象/函数（折叠会让 fetch/console 等彼此不可区分，抬高误报）
        "Array", "Boolean", "console", "Date", "Error", "globalThis", "JSON",
        "Map", "Math", "Number", "Object", "parseFloat", "parseInt", "Promise",
        "Proxy", "Reflect", "RegExp", "Set", "String", "Symbol", "WeakMap",
        "WeakSet", "clearInterval", "clearTimeout", "document", "exports",
        "fetch", "global", "isNaN", "module", "process", "require", "setInterval",
        "setTimeout", "window",
    }
)
_T2_KEEP_BY_LANG = {
    "python": _PY_KEEP_NAMES,
    "javascript": _JS_TS_KEEP_NAMES,
    "typescript": _JS_TS_KEEP_NAMES,
}
# 标识符 token（一级归一化后 STR/NUM 等占位与标点不受影响）
_T2_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
# 属性/方法名（紧随点号之后的标识符）一律折叠为 ID
_T2_ATTR_IDENT_RE = re.compile(r"(?<=\.)[A-Za-z_]\w*")
# 函数头识别（用于"同函数体内部自相似不报"的归属过滤，尽力而为）
_PY_FUNC_HEADER_RE = re.compile(r"^(\s*)(?:async\s+)?def\s+[A-Za-z_]\w*")
_JS_FUNC_HEADER_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*[A-Za-z_$0-9]*\s*\("
)

# 滚动哈希参数（大素数模，降低碰撞概率；碰撞另有逐行比对兜底）
_HASH_MOD = (1 << 61) - 1
_HASH_BASE = 1_000_003


# ---------------------------------------------------------------- 入口与降级


def run(ctx: Any) -> list[Issue]:
    """引擎挂点入口：全库克隆检测，返回 Issue 列表（不填 id）。永不抛异常。"""
    try:
        return _scan(ctx)
    except Exception as exc:  # 扫描器故障绝不阻断主检测流程
        _record_error(ctx, repr(exc))
        return []


def _record_error(ctx: Any, message: str) -> None:
    """把扫描失败原因记入 ctx.extra["post_scan_errors"]（尽力而为）。"""
    try:
        ctx.extra.setdefault("post_scan_errors", {})[MODULE_NAME] = message
    except Exception:  # pragma: no cover - extra 异常时静默
        pass


def _min_lines() -> int:
    """读取克隆最小行数：默认 6；env 覆盖，非法值回退默认，收敛到 [2, 200]。"""
    try:
        raw = os.environ.get(CLONE_MIN_LINES_ENV, "") or ""
    except Exception:  # pragma: no cover - environ 异常时静默
        raw = ""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CLONE_MIN_LINES
    if value <= 0:
        return DEFAULT_CLONE_MIN_LINES
    return max(2, min(200, value))


# ---------------------------------------------------------------- 行归一化


def _normalize_lines(lines: list[str], language: str) -> list[str]:
    """行归一化：字符串->STR、数字->NUM、去行内注释、空白折叠。

    空行与花括号行保留（归一化后参与结构比对）；归一化后的空行记为空串。
    带快速路径：不含引号/注释符的行跳过对应正则（全库扫描的热点，保 O(n) 常数）。
    """
    is_python = language == "python"
    out: list[str] = []
    in_block_comment = False  # js/ts 跨行 /* */ 的简单状态机
    for raw in lines:
        line = raw
        if in_block_comment:
            end = line.find("*/")
            if end < 0:
                out.append("")
                continue
            line = line[end + 2 :]
            in_block_comment = False
        if '"' in line or "'" in line or "`" in line:
            # 先替换字符串，之后 # 与 // 必为注释
            for pattern in _STRING_RES:
                line = pattern.sub("STR", line)
        if is_python:
            if "#" in line:
                line = line.split("#", 1)[0]
        else:
            if "/*" in line:
                line = _INLINE_BLOCK_COMMENT_RE.sub(" ", line)  # 同行闭合块注释
                if "/*" in line:  # 跨行块注释：截断并记住状态
                    line = line.split("/*", 1)[0]
                    in_block_comment = True
            if "//" in line:
                line = line.split("//", 1)[0]
        line = _NUMBER_RE.sub("NUM", line)
        out.append(" ".join(line.split()))
    return out


# ---------------------------------------------------------------- 窗口指纹


def _window_fingerprints(line_hashes: list[int], k: int, pow_bk: int) -> list[int]:
    """前缀和多项式滚动哈希：O(n) 求每个 k 行窗口的指纹。"""
    prefix = [0] * (len(line_hashes) + 1)
    for i, value in enumerate(line_hashes):
        prefix[i + 1] = (prefix[i] * _HASH_BASE + value) % _HASH_MOD
    return [
        (prefix[s + k] - prefix[s] * pow_bk) % _HASH_MOD
        for s in range(0, len(line_hashes) - k + 1)
    ]


# ---------------------------------------------------------------- 主扫描


def _scan(ctx: Any) -> list[Issue]:
    k = _min_lines()
    # files[i] = (rel, language, 原始行列表, 归一化行列表)
    files: list[tuple[str, str, list[str], list[str]]] = []
    total_lines = 0
    try:
        paths = list(ctx.workspace.source_files(None))
    except Exception as exc:
        _record_error(ctx, f"workspace.source_files 失败: {exc!r}")
        return []
    for path in paths:
        try:
            rel = ctx.workspace.rel(path)
        except Exception:
            continue  # 单文件路径异常不阻断
        language = guess_language(rel)
        if language not in _RULE_BY_LANG:
            continue
        try:
            text = ctx.workspace.read_file_text(rel)
        except Exception:
            continue  # 单文件读取失败不阻断
        raw_lines = text.splitlines()
        total_lines += len(raw_lines)
        if total_lines > MAX_TOTAL_LINES:
            _record_error(
                ctx,
                f"全库总行数 {total_lines} 超过上限 {MAX_TOTAL_LINES}，克隆检测跳过（性能护栏）",
            )
            return []
        files.append((rel, language, raw_lines, _normalize_lines(raw_lines, language)))

    # 1) 全局窗口指纹桶：指纹 -> [(文件下标, 0-based 窗口起始行)]
    line_hash_cache: dict[str, int] = {}

    def _line_hash(norm_line: str) -> int:
        cached = line_hash_cache.get(norm_line)
        if cached is None:
            cached = hash(norm_line) % _HASH_MOD
            line_hash_cache[norm_line] = cached
        return cached

    pow_bk = pow(_HASH_BASE, k, _HASH_MOD)
    buckets: dict[int, list[tuple[int, int]]] = {}
    for idx, (_rel, _lang, _raw, norm) in enumerate(files):
        hashes = [_line_hash(line) for line in norm]
        for start, fingerprint in enumerate(_window_fingerprints(hashes, k, pow_bk)):
            if not any(norm[start : start + k]):
                continue  # 全空行窗口无意义（如连续空行），跳过
            buckets.setdefault(fingerprint, []).append((idx, start))

    # 2) 同指纹组 -> 块代表位置折叠 -> 逐行比对确认 -> 向两侧扩展到最大片段
    norm_all = [f[3] for f in files]
    maximal_pairs: dict[tuple[int, int, int, int], tuple[int, int, int]] = {}
    for positions in buckets.values():
        if len(positions) < 2:
            continue
        ordered = sorted(positions)
        reps: list[tuple[int, int]] = []
        for pos in ordered:
            # 同文件相邻窗口（起始行差 < k）属于同一片段，只保留首个代表
            if reps and reps[-1][0] == pos[0] and pos[1] - reps[-1][1] < k:
                continue
            reps.append(pos)
        if len(reps) < 2 or len(reps) > MAX_GROUP_REPS:
            continue  # 组内位置过少/过多（海量重复片段）均跳过，保 O(n)
        for i in range(len(reps)):
            for j in range(i + 1, len(reps)):
                first_file, first_start = reps[i]
                second_file, second_start = reps[j]
                # 哈希碰撞防御：逐行比对确认窗口确实相同
                if norm_all[first_file][first_start : first_start + k] != norm_all[
                    second_file
                ][second_start : second_start + k]:
                    continue
                a_start, b_start = first_start, second_start
                length = k
                # 向两侧扩展到最大相同片段（空行与代码行一视同仁，块内逐行对齐相等）
                while a_start > 0 and b_start > 0 and (
                    norm_all[first_file][a_start - 1] == norm_all[second_file][b_start - 1]
                ):
                    a_start -= 1
                    b_start -= 1
                    length += 1
                while a_start + length < len(norm_all[first_file]) and (
                    b_start + length < len(norm_all[second_file])
                ) and (
                    norm_all[first_file][a_start + length]
                    == norm_all[second_file][b_start + length]
                ):
                    length += 1
                # 修剪块两端的空行：空行 padding 不计入克隆行数（防止
                # "5 行代码 + 空行"被凑满 K 行误报，也与 5 行不报的口径一致）
                while length > 0 and not norm_all[first_file][a_start]:
                    a_start += 1
                    b_start += 1
                    length -= 1
                while length > 0 and not norm_all[first_file][a_start + length - 1]:
                    length -= 1
                if length < k:
                    continue  # 修剪后不足阈值（短于 K 行的有效重复）不报
                maximal_pairs[(first_file, second_file, a_start, b_start)] = (
                    a_start,
                    b_start,
                    length,
                )

    # 3) 报告去重：同一第二处 start 行只报最优（最长）对
    best_by_second: dict[tuple[str, int], tuple[int, int, int, tuple[int, int, int, int]]] = {}
    for (fi, gi, a_start, b_start), (_a, _b, length) in maximal_pairs.items():
        key = (files[gi][0], b_start + 1)
        current = best_by_second.get(key)
        if current is None or length > current[2]:
            best_by_second[key] = (fi, gi, length, (a_start, b_start))

    issues: list[Issue] = []
    for (rel_b, _second_line), (fi, gi, length, (a_start, b_start)) in best_by_second.items():
        rel_a, _lang_a, raw_a, _norm_a = files[fi]
        _rel_b2, lang_b, raw_b, _norm_b = files[gi]
        a_first, a_last = a_start + 1, a_start + length
        b_first, b_last = b_start + 1, b_start + length
        title = f"第 {b_first}~{b_last} 行与 {rel_a} 第 {a_first}~{a_last} 行存在 {length} 行重复代码块"
        snippet = "\n".join(raw_b[b_start : b_start + length])
        issues.append(
            Issue(
                category=Category.STYLE,
                severity=Severity.MEDIUM,
                title=title[:120],
                file=rel_b,
                line_start=b_first,
                line_end=b_last,
                code_snippet=truncate(snippet, _SNIPPET_MAX_CHARS),
                description=(
                    f"检测到归一化（去注释、字符串->STR、数字->NUM、空白折叠）后连续 {length} 行"
                    f"完全相同的重复代码片段：{rel_a} 第 {a_first}~{a_last} 行与本处逐行一致"
                    f"（同文件或跨文件克隆，Type-2 归一化口径）。"
                ),
                evidence=[
                    f"rule:{_RULE_BY_LANG[lang_b]}",
                    f"loc:{rel_a}:{a_first}-{a_last}",
                    f"loc:{rel_b}:{b_first}-{b_last}",
                    f"clone_lines:{length}",
                ],
                suggestion=(
                    "将重复片段提取为公共函数/模块并在两处复用，"
                    "消除复制粘贴带来的双重维护与漂移风险。"
                ),
                confidence=_CONFIDENCE,
                source=IssueSource.RULE,
            )
        )
    # Type-2 二级匹配：纯新增通道，失败不影响上方既有 Type-1 产出
    try:
        issues.extend(_t2_issues(files, maximal_pairs))
    except Exception as exc:  # Type-2 故障降级：记 post_scan_errors，Type-1 结果照常返回
        _record_error(ctx, f"Type-2 二级匹配异常（Type-1 结果照常返回）: {exc!r}")
    issues.sort(key=lambda issue: (issue.file, issue.line_start))
    return issues


# ------------------------------------------------- Type-2 二级匹配（标识符折叠）


def _t2_min_lines() -> int:
    """读取 Type-2 最小行数：默认 8；env 覆盖，非法值回退默认，收敛到 [2, 200]。"""
    try:
        raw = os.environ.get(T2_MIN_LINES_ENV, "") or ""
    except Exception:  # pragma: no cover - environ 异常时静默
        raw = ""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_T2_MIN_LINES
    if value <= 0:
        return DEFAULT_T2_MIN_LINES
    return max(2, min(200, value))


def _t2_normalize_lines(norm_lines: list[str], language: str) -> list[str]:
    """二级归一化：在一级行归一化结果之上，把"用户标识符"折叠为 ID。

    折叠对象：def/函数名、参数名、局部变量名一律 -> ID；属性/方法名（紧随
    点号的标识符）一律 -> ID。关键字与内置名保留——不同的内置调用/关键字
    仍会造成指纹差异，是抑制误报的关键。输入需为 _normalize_lines 的产物。
    """
    keep = _T2_KEEP_BY_LANG.get(language, _PY_KEEP_NAMES)
    out: list[str] = []
    for line in norm_lines:
        if not line:
            out.append("")
            continue
        if "." in line:
            line = _T2_ATTR_IDENT_RE.sub("ID", line)
        line = _T2_IDENT_RE.sub(lambda m: m.group(0) if m.group(0) in keep else "ID", line)
        out.append(line)
    return out


def _function_owners(
    raw_lines: list[str], norm_lines: list[str], language: str
) -> list[int]:
    """逐行标注所属函数编号（-1 = 模块级/不可识别），供 Type-2 同函数过滤。

    python：def/async def 行开启新函数区域（嵌套 def 亦各算一个），遇到
    非空行缩进 <= def 行缩进即区域结束（装饰器行/类头归上一级归属，保守）；
    js/ts：function 声明行开启区域（尽力而为），基于一级归一化行统计花括号
    深度，回落到声明前深度即结束；箭头函数/类方法等头型不识别，按模块级
    处理（宁可少报，不误报）。
    """
    owners = [-1] * len(raw_lines)
    if language == "python":
        current = -1
        header_indent: int | None = None
        next_id = 0  # 全文件单调递增，避免区域结束后编号复用导致同函数误判
        for i, line in enumerate(raw_lines):
            matched = _PY_FUNC_HEADER_RE.match(line)
            if matched:
                current = next_id
                next_id += 1
                header_indent = len(matched.group(1))
                owners[i] = current
                continue
            if header_indent is not None:
                if not line.strip():
                    owners[i] = current
                    continue
                if len(line) - len(line.lstrip()) <= header_indent:
                    current = -1  # 函数体结束（回到模块级/外层）
                    header_indent = None
            owners[i] = current
        return owners
    depth = 0
    current = -1
    base_depth = 0
    next_id = 0
    for i, norm in enumerate(norm_lines):
        if current == -1 and _JS_FUNC_HEADER_RE.match(raw_lines[i]):
            current = next_id
            next_id += 1
            base_depth = depth
            owners[i] = current
            depth += norm.count("{") - norm.count("}")
            continue
        depth += norm.count("{") - norm.count("}")
        if current != -1 and depth <= base_depth:
            current = -1
        owners[i] = current
    return owners


def _t2_issues(
    files: list[tuple[str, str, list[str], list[str]]],
    t1_pairs: dict[tuple[int, int, int, int], tuple[int, int, int]],
) -> list[Issue]:
    """Type-2 二级通道：在 Type-1 未命中区间上识别"仅标识符/常量不同"的克隆。

    纯新增通道：只读 Type-1 的极大块对照表做覆盖过滤，不改 Type-1 判定与
    产出；自身异常由 _scan 调用处兜底。防误报约束（与任务口径一致）：
    - 只报不同函数之间，同一函数体内部（含同为模块级）的自相似不报；
    - 命中行已被 Type-1 覆盖则整对跳过（逐字克隆归 Type-1，不重复报）；
    - 单指纹组 > MAX_GROUP_REPS 跳过、全库总行数护栏沿用主扫描既有逻辑。
    """
    k = _t2_min_lines()
    # 0) 二级归一化行 + 函数归属表
    t2_norm_all = [
        _t2_normalize_lines(norm, language) for _rel, language, _raw, norm in files
    ]
    owner_all = [_function_owners(raw, norm, language) for _rel, language, raw, norm in files]

    # 1) Type-1 已命中行集合（极大块两侧全部行，含扩展）
    covered: set[tuple[int, int]] = set()
    for (fi, gi, a_start, b_start), (_a, _b, length) in t1_pairs.items():
        for offset in range(length):
            covered.add((fi, a_start + offset))
            covered.add((gi, b_start + offset))

    # 2) k 行窗口指纹桶（与 Type-1 同口径；全空行窗口跳过）
    hash_cache: dict[str, int] = {}

    def _hash(line: str) -> int:
        cached = hash_cache.get(line)
        if cached is None:
            cached = hash(line) % _HASH_MOD
            hash_cache[line] = cached
        return cached

    pow_bk = pow(_HASH_BASE, k, _HASH_MOD)
    buckets: dict[int, list[tuple[int, int]]] = {}
    for idx, t2_norm in enumerate(t2_norm_all):
        hashes = [_hash(line) for line in t2_norm]
        for start, fingerprint in enumerate(_window_fingerprints(hashes, k, pow_bk)):
            if not any(t2_norm[start : start + k]):
                continue
            buckets.setdefault(fingerprint, []).append((idx, start))

    # 3) 块代表折叠 -> 逐行比对确认 -> 两侧扩展 -> 修剪空行（与 Type-1 同口径）
    t2_pairs: dict[tuple[int, int, int, int], tuple[int, int, int]] = {}
    for positions in buckets.values():
        if len(positions) < 2:
            continue
        ordered = sorted(positions)
        reps: list[tuple[int, int]] = []
        for pos in ordered:
            # 同文件相邻窗口（起始行差 < k）属于同一片段，只保留首个代表
            if reps and reps[-1][0] == pos[0] and pos[1] - reps[-1][1] < k:
                continue
            reps.append(pos)
        if len(reps) < 2 or len(reps) > MAX_GROUP_REPS:
            continue
        for i in range(len(reps)):
            for j in range(i + 1, len(reps)):
                first_file, first_start = reps[i]
                second_file, second_start = reps[j]
                # 哈希碰撞防御：逐行比对确认窗口确实相同
                if t2_norm_all[first_file][first_start : first_start + k] != t2_norm_all[
                    second_file
                ][second_start : second_start + k]:
                    continue
                a_start, b_start = first_start, second_start
                length = k
                # 向两侧扩展到最大相同片段（二级归一化行上逐行对齐相等）
                while a_start > 0 and b_start > 0 and (
                    t2_norm_all[first_file][a_start - 1]
                    == t2_norm_all[second_file][b_start - 1]
                ):
                    a_start -= 1
                    b_start -= 1
                    length += 1
                while a_start + length < len(t2_norm_all[first_file]) and (
                    b_start + length < len(t2_norm_all[second_file])
                ) and (
                    t2_norm_all[first_file][a_start + length]
                    == t2_norm_all[second_file][b_start + length]
                ):
                    length += 1
                # 修剪块两端的空行（与 Type-1 同口径：空行 padding 不计行数）
                while length > 0 and not t2_norm_all[first_file][a_start]:
                    a_start += 1
                    b_start += 1
                    length -= 1
                while length > 0 and not t2_norm_all[first_file][a_start + length - 1]:
                    length -= 1
                if length < k:
                    continue
                t2_pairs[(first_file, second_file, a_start, b_start)] = (
                    a_start,
                    b_start,
                    length,
                )

    # 4) 同函数过滤 + Type-1 覆盖过滤 + 报告去重（同一第二处 start 只报最长）
    best_by_second: dict[tuple[str, int], tuple[int, int, int, tuple[int, int]]] = {}
    for (fi, gi, a_start, b_start), (_a, _b, length) in t2_pairs.items():
        if fi == gi:
            owners = owner_all[fi]
            owner_a = owners[a_start] if a_start < len(owners) else -1
            owner_b = owners[b_start] if b_start < len(owners) else -1
            if owner_a == owner_b:
                continue  # 同一函数体内部（或同为模块级）的自相似不报
        overlapped = False
        for offset in range(length):
            if (fi, a_start + offset) in covered or (gi, b_start + offset) in covered:
                overlapped = True
                break
        if overlapped:
            continue  # 命中行已被 Type-1 覆盖：逐字克隆走 Type-1，不重复报 Type-2
        key = (files[gi][0], b_start + 1)
        current = best_by_second.get(key)
        if current is None or length > current[2]:
            best_by_second[key] = (fi, gi, length, (a_start, b_start))

    # 5) 构建 Issue：severity=low、confidence=0.5、evidence 加 type:2，每对挂第二处
    issues: list[Issue] = []
    for (_rel_b, _second_line), (fi, gi, length, (a_start, b_start)) in best_by_second.items():
        rel_a = files[fi][0]
        lang_b = files[gi][1]
        rel_b = files[gi][0]
        a_first, a_last = a_start + 1, a_start + length
        b_first, b_last = b_start + 1, b_start + length
        title = (
            f"第 {b_first}~{b_last} 行与 {rel_a} 第 {a_first}~{a_last} 行结构相似"
            f"（Type-2：标识符/常量不同）——语义克隆疑似，请人工确认"
        )
        snippet = "\n".join(files[gi][2][b_start : b_start + length])
        issues.append(
            Issue(
                category=Category.STYLE,
                severity=Severity.LOW,
                title=title[:120],
                file=rel_b,
                line_start=b_first,
                line_end=b_last,
                code_snippet=truncate(snippet, _SNIPPET_MAX_CHARS),
                description=(
                    f"二级归一化（一级行归一化基础上将标识符折叠为 ID）后连续 {length} 行结构一致："
                    f"{rel_a} 第 {a_first}~{a_last} 行与本处仅标识符命名/常量取值不同"
                    f"（Type-2 语义克隆疑似）。该通道误报率高于逐字克隆，仅作人工确认线索。"
                ),
                evidence=[
                    f"rule:{_RULE_BY_LANG[lang_b]}",
                    "type:2",
                    f"loc:{rel_a}:{a_first}-{a_last}",
                    f"loc:{rel_b}:{b_first}-{b_last}",
                    f"clone_lines:{length}",
                ],
                suggestion=(
                    "两处片段控制结构一致、仅标识符与常量不同，请先人工核对语义是否等价，"
                    "若等价则提取为公共函数/模块复用，避免逻辑漂移。"
                ),
                confidence=_T2_CONFIDENCE,
                source=IssueSource.RULE,
            )
        )
    return issues
