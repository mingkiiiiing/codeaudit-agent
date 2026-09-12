"""JS/TS 静态规则库·扩充第二期（W6-A3，5 条 JS + 1 条 TS）。

与 javascript.py/typescript.py 同一实现思路：基于 `_js_common` 的逐字符掩码扫描 +
作用域启发式（不依赖 tree-sitter），保证行号精确落在真实代码行上；所有规则 `check()`
纯函数式，不修改 ctx、无 IO。新规则一律放本文件（`*_ext.py`）。

语言声明沿用既有约定：bug/performance 类只作用于 javascript；
语法在两种语言中完全一致的 security 类以 ("javascript", "typescript") 共享。

每条规则均带 good_example/bad_example 类属性（契约 v1.6），供
scripts/gen_rule_docs.py 在规则手册中渲染正反示例。
"""

from __future__ import annotations

import re
from typing import Pattern

from audit.detect.base import Rule, RuleContext, RuleHit
from audit.detect.rules.js._js_common import (
    call_span,
    enclosing_function,
    find_call,
    function_ranges,
    get_scan,
)
from audit.models import Category, Severity

__all__ = ["build_js_ext_rules"]


class _JsExtRule(Rule):
    """仅作用于 JavaScript 的扩充规则基类。"""

    languages = ("javascript",)


class _JsSharedExtRule(Rule):
    """JS/TS 共享扩充规则基类（语法在两种语言中完全一致的安全类问题）。"""

    languages = ("javascript", "typescript")


class _TsExtRule(Rule):
    """仅作用于 TypeScript 的扩充规则基类。"""

    languages = ("typescript",)


# ---------------------------------------------------------------- 本地工具（不改 _js_common）


def _find_block(masked: list[str], head_idx0: int) -> tuple[int, int, int, int] | None:
    """从语句头所在行（0-based）定位其后代码块的 `{...}` 跨度。

    返回 (开括号行1based, 开括号列, 闭括号行1based, 闭括号列)；
    `{` 必须在头行上，或头行之后第一个非空行的行首（避免误抓更远处的无关大括号）。
    找不到配对块返回 None。
    """
    line = masked[head_idx0]
    col = line.find("{")
    open_pos: tuple[int, int] | None = (head_idx0, col) if col >= 0 else None
    if open_pos is None:
        k = head_idx0 + 1
        while k < len(masked) and k <= head_idx0 + 3:
            text = masked[k].strip()
            if not text:
                k += 1
                continue
            if text.startswith("{"):
                open_pos = (k, masked[k].find("{"))
            break
    if open_pos is None:
        return None
    depth = 0
    li, cj = open_pos
    while li < len(masked):
        text = masked[li]
        while cj < len(text):
            ch = text[cj]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return open_pos[0] + 1, open_pos[1], li + 1, cj
            cj += 1
        li += 1
        cj = 0
    return None


def _top_level_split(text: str) -> list[str]:
    """按顶层逗号切分（括号内逗号不切），用于取调用的首个实参。"""
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return parts


def _string_literal_value(raw: str, quote_col: int, quote: str) -> str | None:
    """从 raw 的 quote_col 处开引号提取字符串内容（处理转义；未闭合返回 None）。"""
    i = quote_col + 1
    out: list[str] = []
    while i < len(raw):
        ch = raw[i]
        if ch == "\\":
            out.append(raw[i : i + 2])
            i += 2
            continue
        if ch == quote:
            return "".join(out)
        out.append(ch)
        i += 1
    return None


# ==================================================================== performance 类


_LOOP_HEAD_RE: Pattern[str] = re.compile(r"^\s*(?:for\s*(?:await\s*)?\(|while\s*\(|do\s*(?:\{|$))")
_AWAIT_RE: Pattern[str] = re.compile(r"(?<![\w.$])await\b")
# 内联 async 回调头：async function( / async x => / async (a, b) =>
_ASYNC_HEAD_RE: Pattern[str] = re.compile(
    r"\basync\b\s*(?:function\b\s*[\w$]*\s*\(|[\w$]+\s*=>|\([^()]*\)\s*=>)"
)


def _inside_inline_async(prefix: str) -> bool:
    """prefix 末尾是否存在未终结的内联 async 回调体（await 属回调而非循环体）。

    记录每个 async 回调头开启时的括号深度；遇到与其深度相同的 ``)`` 或 ``,``
    即视为回调实参终结。存在任一存活回调头则 await 在回调体内。
    """
    depth = 0
    live: list[int] = []
    i, n = 0, len(prefix)
    while i < n:
        m = _ASYNC_HEAD_RE.match(prefix, i)
        if m:
            live.append(depth)
            i = m.end()
            continue
        ch = prefix[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            live = [d for d in live if d != depth]
            depth -= 1
        elif ch == ",":
            live = [d for d in live if d != depth]
        i += 1
    return bool(live)


class AwaitInLoopRule(_JsExtRule):
    """for/while 体内直接 await：迭代被串行化。"""

    id = "JS-AWAIT-IN-LOOP"
    category = Category.PERFORMANCE
    severity = Severity.MEDIUM
    description = (
        "循环体内逐次 `await`：每次迭代都要等上一次完成，总耗时为各次之和（串行放大）；"
        "相互独立的请求应先收集 Promise 再用 `Promise.all` 并行。"
    )

    bad_example = (
        "async function loadAll(ids) {\n"
        "  const out = [];\n"
        "  for (const id of ids) {\n"
        "    out.push(await fetchUser(id)); // 串行等待，n 倍时延\n"
        "  }\n"
        "  return out;\n"
        "}\n"
    )
    good_example = (
        "async function loadAll(ids) {\n"
        "  return Promise.all(ids.map((id) => fetchUser(id)));\n"
        "}\n"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        ranges = function_ranges(scan.masked)
        hits: list[RuleHit] = []
        reported: set[int] = set()
        for idx0, ln in enumerate(scan.masked):
            if not _LOOP_HEAD_RE.match(ln):
                continue
            block = _find_block(scan.masked, idx0)
            if block is None:
                continue  # 单语句体/头行跨行等形态，启发式不覆盖
            open_line, _oc, close_line, _cc = block
            head_line = idx0 + 1
            prefix_chars: list[str] = []
            for lineno in range(open_line, close_line + 1):
                text = scan.masked[lineno - 1]
                if lineno == open_line and lineno == close_line:
                    text = text[text.find("{") + 1 : text.rfind("}")]
                elif lineno == open_line:
                    text = text[text.find("{") + 1 :]
                elif lineno == close_line:
                    text = text[: text.rfind("}")]
                for m in _AWAIT_RE.finditer(text):
                    if lineno in reported:
                        break
                    prefix = "".join(prefix_chars) + text[: m.start()]
                    if _inside_inline_async(prefix):
                        continue  # await 位于循环体内声明的内联 async 回调中
                    fn = enclosing_function(ranges, lineno)
                    if fn is not None and fn.start > head_line:
                        continue  # await 位于循环体内声明的具名函数/箭头赋值中
                    reported.add(lineno)
                    hits.append(
                        self.make_hit(
                            ctx,
                            lineno,
                            lineno,
                            f"第 {lineno} 行在第 {head_line} 行循环体内直接 `await`：各次迭代被串行化，"
                            "总耗时随迭代次数线性叠加（n 次请求 = n 倍时延）；"
                            "相互独立的调用请收集为 Promise 数组后用 `Promise.all` 并行执行。",
                        )
                    )
                prefix_chars.append(text + "\n")
        return hits


# ==================================================================== bug 类


class DoubleEqNullRule(_JsExtRule):
    """== null / != null 宽松判空。"""

    id = "JS-DOUBLE-EQ-NULL"
    category = Category.BUG
    severity = Severity.LOW
    description = (
        "用 `== null`/`!= null` 判空：宽松相等会把 undefined 也判进 null，"
        "而 0/''/false 又不判空，边界语义含混；应显式写 `=== null`/`=== undefined` 或收敛为统一判空工具。"
    )

    bad_example = "if (value == null) {\n  value = DEFAULT;\n}\n"
    good_example = "if (value === null || value === undefined) {\n  value = DEFAULT;\n}\n"

    _RE: Pattern[str] = re.compile(r"(?<![=!<>])(?:==|!=)(?!=)\s*null\b|null\b\s*(?:==|!=)(?![=])")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            if self._RE.search(ln):
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行使用 `== null`/`!= null` 宽松判空：宽松相等把 `undefined` 一并判为 null，"
                        "而 `0`/`''`/`false` 不判空，边界值下行为与直觉相反；"
                        "请改用 `=== null`/`=== undefined` 显式判断，或封装统一的判空工具函数。",
                    )
                )
        return hits


class EmptyCatchRule(_JsExtRule):
    """catch 捕获块为空：错误被静默吞掉。"""

    id = "JS-EMPTY-CATCH"
    category = Category.BUG
    severity = Severity.MEDIUM
    description = (
        "`catch` 块体为空：异常被捕获后不做任何处理，故障既无日志也无恢复动作，"
        "线上只能以『数据莫名不对』的形式暴露；至少应记录日志，无法处理时向上重新抛出。"
    )

    bad_example = (
        "try {\n"
        "  saveProfile(data);\n"
        "} catch (e) {} // 失败无声无息\n"
    )
    good_example = (
        "try {\n"
        "  saveProfile(data);\n"
        "} catch (e) {\n"
        "  logger.error('saveProfile failed', e);\n"
        "  throw e;\n"
        "}\n"
    )

    _CATCH_RE: Pattern[str] = re.compile(r"(?<![\w.$])catch\b")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            for m in self._CATCH_RE.finditer(ln):
                block = self._block_after_catch(scan.masked, idx, m.end())
                if block is None:
                    continue
                if self._body_text(scan.masked, block).strip():
                    continue
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        block[2],
                        f"第 {idx + 1} 行的 `catch` 块为空（第 {block[2]} 行收口）：异常被静默吞掉，"
                        "故障发生时无任何日志与痕迹可查；请至少记录错误日志，无法处理时向上重新抛出。",
                    )
                )
        return hits

    @staticmethod
    def _block_after_catch(masked: list[str], idx0: int, from_col: int) -> tuple[int, int, int, int] | None:
        """从 catch 关键字末尾起定位 `{...}` 块，返回 (开行1b, 开列, 闭行1b, 闭列)。"""
        li, cj = idx0, from_col
        # 跳过空白与可选的 (params)
        while li < len(masked):
            text = masked[li]
            while cj < len(text) and text[cj].isspace():
                cj += 1
            if cj >= len(text):
                li += 1
                cj = 0
                continue
            if text[cj] != "(":
                break
            depth = 0
            closed = False
            while li < len(masked):
                text2 = masked[li]
                while cj < len(text2):
                    ch = text2[cj]
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                        if depth == 0:
                            closed = True
                            break
                    cj += 1
                if closed:
                    break
                cj = 0
                li += 1
            if not closed:
                return None
            cj += 1  # 越过 ')'
            if cj >= len(masked[li]):
                li += 1
                cj = 0
        # 捕获关键字与 `{` 之间只允许空白
        open_pos: tuple[int, int] | None = None
        while li < len(masked) and li <= idx0 + 3:
            text = masked[li]
            while cj < len(text):
                ch = text[cj]
                if ch == "{":
                    open_pos = (li, cj)
                    break
                if not ch.isspace():
                    return None
                cj += 1
            if open_pos is not None:
                break
            li += 1
            cj = 0
        if open_pos is None:
            return None
        depth = 0
        li, cj = open_pos
        while li < len(masked):
            text = masked[li]
            while cj < len(text):
                ch = text[cj]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return open_pos[0] + 1, open_pos[1], li + 1, cj
                cj += 1
            li += 1
            cj = 0
        return None

    @staticmethod
    def _body_text(masked: list[str], block: tuple[int, int, int, int]) -> str:
        open_line, open_col, close_line, close_col = block
        if open_line == close_line:
            return masked[open_line - 1][open_col + 1 : close_col]
        parts = [masked[open_line - 1][open_col + 1 :]]
        parts.extend(masked[k] for k in range(open_line, close_line - 1))
        parts.append(masked[close_line - 1][:close_col])
        return "\n".join(parts)


# ==================================================================== security 类


_SENSITIVE_TOKENS = frozenset(
    ["token", "secret", "password", "passwd", "pwd", "apikey", "api_key", "credential", "key"]
)
_IDENT_TOKEN_SPLIT_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|\d+")


def _name_tokens(name: str) -> set[str]:
    """键名分词：下划线/连字符/点切分后再按驼峰切，全部转小写。"""
    tokens: set[str] = set()
    for part in re.split(r"[\s_\-.\[\]]+", name):
        tokens.update(t.lower() for t in _IDENT_TOKEN_SPLIT_RE.findall(part))
    return tokens


class LocalStorageSensitiveRule(_JsSharedExtRule):
    """localStorage.setItem 存敏感信息：XSS 可直接读走。"""

    id = "JS-LOCALSTORAGE-SENSITIVE"
    category = Category.SECURITY
    severity = Severity.HIGH
    description = (
        "把疑似敏感凭据（键名含 token/secret/password/key 等）写入 localStorage："
        "localStorage 对同源任意 JS 完全开放，一个 XSS 漏洞就能把凭据读走外传；"
        "应改用 HttpOnly + Secure Cookie 或服务端会话。"
    )

    bad_example = (
        "// XSS 一旦发生，攻击者即可读走会话凭据\n"
        "localStorage.setItem('access_token', token);\n"
    )
    good_example = (
        "// HttpOnly Cookie 对 JS 不可见，XSS 无法窃取\n"
        "document.cookie = `session=${encodeURIComponent(token)}; Secure; SameSite=Strict`;\n"
    )

    _CALL_RE: Pattern[str] = re.compile(r"localStorage\s*\.\s*setItem\s*\(")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            for col in find_call(ln, self._CALL_RE):
                span = call_span(scan.masked, idx + 1, col)
                if span is None:
                    continue
                key = self._first_arg_value(ctx, idx + 1, col, span)
                if not key or not (_name_tokens(key) & _SENSITIVE_TOKENS):
                    continue
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        span[0],
                        f"第 {idx + 1} 行将疑似敏感凭据写入 localStorage（键 `{key}`）："
                        "localStorage 对同源任意 JS 可读，存在 XSS 时凭据会被直接窃取，"
                        "且数据持久化在用户磁盘上；请改用 HttpOnly + Secure Cookie 或服务端会话。",
                        meta={"key": key},
                    )
                )
        return hits

    @staticmethod
    def _first_arg_value(ctx: RuleContext, line: int, open_col: int, span: tuple) -> str:
        """取 setItem 首参键名：字符串字面量取内容，表达式取末位标识符。"""
        raw = ctx.lines[line - 1]
        k = open_col + 1
        while k < len(raw) and raw[k].isspace():
            k += 1
        if k < len(raw) and raw[k] in ("'", '"', "`"):
            value = _string_literal_value(raw, k, raw[k])
            return value.strip() if value else ""
        args = span[2] if len(span) > 2 else ""
        first = _top_level_split(args)[0]
        idents = re.findall(r"[\w$]+", first)
        return idents[-1] if idents else ""


class DocumentCookieWriteRule(_JsSharedExtRule):
    """document.cookie 赋动态值：注入与凭据暴露风险。"""

    id = "JS-DOCUMENT-COOKIE-WRITE"
    category = Category.SECURITY
    severity = Severity.MEDIUM
    description = (
        "向 `document.cookie` 赋值变量/拼接内容：键值未经编码可能注入 `;` 破坏 cookie 结构，"
        "动态值常含凭据且对同源 JS 完全可见；请统一封装 setCookie 并做 encodeURIComponent 与 HttpOnly 评估。"
    )

    bad_example = "document.cookie = 'session=' + token + '; path=/';\n"
    good_example = (
        "document.cookie = `session=${encodeURIComponent(token)}; path=/; Secure; SameSite=Strict`;\n"
    )

    _RE: Pattern[str] = re.compile(r"\bdocument\s*\.\s*cookie\s*=\s*(?!=)")
    _CONST_LITERAL_RE: Pattern[str] = re.compile(r"^(['\"`])[^'\"`]*\1$")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            m = self._RE.search(ln)
            if not m:
                continue
            rhs = ln[m.end() :].strip()
            extra = 0
            while rhs.endswith("+") and idx + extra + 1 < len(scan.masked) and extra < 3:
                extra += 1
                rhs = (rhs + " " + scan.masked[idx + extra].strip()).strip()
            rhs = rhs.rstrip(";").strip()
            if not rhs:
                continue
            if "${" not in rhs and self._CONST_LITERAL_RE.fullmatch(rhs):
                continue  # 常量字面量（清空 cookie/固定说明串）不报；模板插值除外
            hits.append(
                self.make_hit(
                    ctx,
                    idx + 1,
                    idx + 1,
                    f"第 {idx + 1} 行向 `document.cookie` 赋值动态内容（变量/拼接/模板插值）："
                    "键值未经编码可被注入 `;` 篡改 cookie 结构，动态值常含凭据且对同源 JS 完全可见；"
                    "请对值做 encodeURIComponent，并评估改用 HttpOnly Cookie。",
                )
            )
        return hits


# ==================================================================== TypeScript 类


class TsDependsOnAnyRule(_TsExtRule):
    """函数缺返回类型注解且参数含 any：契约实际退化为 any。"""

    id = "TS-DEPENDS-ON-ANY"
    category = Category.STYLE
    severity = Severity.LOW
    description = (
        "函数参数使用 `any` 且未标注返回类型：返回值类型经 any 推导后失去约束，"
        "契约名存实亡；请为参数与返回值补具体类型（或 unknown + 类型收窄）。"
    )

    bad_example = (
        "function transform(raw: any) { // 返回类型缺省，随 any 一并失守\n"
        "  return JSON.parse(raw);\n"
        "}\n"
    )
    good_example = (
        "function transform(raw: string): Record<string, unknown> {\n"
        "  return JSON.parse(raw) as Record<string, unknown>;\n"
        "}\n"
    )

    _FUNC_RE: Pattern[str] = re.compile(
        r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*[*]?\s*([\w$]+)\s*[(<]"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            m = self._FUNC_RE.match(ln)
            if not m:
                continue
            signature = self._signature(scan.masked, idx)
            if not signature or signature.rstrip().endswith(";"):
                continue  # 重载/declare 声明无函数体，跳过
            params = self._params_text(signature)
            if not re.search(r":\s*any\b", params):
                continue
            tail = self._tail_after_params(signature)
            if re.match(r"\s*:\s*\S", tail):
                continue  # 已有显式返回类型
            hits.append(
                self.make_hit(
                    ctx,
                    idx + 1,
                    idx + 1,
                    f"第 {idx + 1} 行函数 `{m.group(1)}` 参数使用了 `any` 且缺少显式返回类型标注："
                    "any 沿返回值向外扩散，调用方拿到无约束类型，字段拼写错误要到运行时才暴露；"
                    "请为参数与返回值补充具体类型，或使用 unknown + 类型收窄。",
                )
            )
        return hits

    @staticmethod
    def _signature(masked: list[str], start: int, max_lines: int = 6) -> str:
        parts: list[str] = []
        for k in range(start, min(start + max_lines, len(masked))):
            text = masked[k].strip()
            parts.append(text)
            if "{" in text or ";" in text:
                break
        return " ".join(parts)

    @staticmethod
    def _params_text(signature: str) -> str:
        start = signature.find("(")
        if start < 0:
            return ""
        depth = 0
        for k in range(start, len(signature)):
            ch = signature[k]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return signature[start + 1 : k]
        return signature[start + 1 :]

    @staticmethod
    def _tail_after_params(signature: str) -> str:
        start = signature.find("(")
        depth = 0
        for k in range(start, len(signature)):
            ch = signature[k]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return signature[k + 1 :]
        return ""


# ==================================================================== 注册


def build_js_ext_rules() -> list[Rule]:
    """构建扩充第二期全部 JS/TS 规则实例（顺序即默认报告顺序）。"""
    return [
        # performance
        AwaitInLoopRule(),
        # bug
        DoubleEqNullRule(),
        EmptyCatchRule(),
        # security（与 TypeScript 共享）
        LocalStorageSensitiveRule(),
        DocumentCookieWriteRule(),
        # typescript
        TsDependsOnAnyRule(),
    ]
