"""JavaScript 静态规则库（16 条，覆盖 bug/security/performance/style 四类）。

实现方式：基于 `_js_common` 的逐字符掩码扫描 + 作用域启发式（不依赖 tree-sitter），
保证行号精确落在真实代码行上。所有规则 `check()` 纯函数式，不修改 ctx、无 IO。

语言声明约定：bug/style/performance 类规则只作用于 javascript；
语言无关的 security 类规则以 ("javascript", "typescript") 共享，TS 文件同样受益。
"""

from __future__ import annotations

import re
from typing import Pattern

from audit.detect.base import Rule, RuleContext, RuleHit
from audit.detect.rules.js._js_common import (
    call_span,
    find_call,
    function_ranges,
    get_scan,
    indent_unit,
    indent_width,
    is_js_test_file,
)
from audit.models import Category, Severity

__all__ = ["build_javascript_rules"]


class _JsRule(Rule):
    """仅作用于 JavaScript 的规则基类。"""

    languages = ("javascript",)


class _JsSharedRule(Rule):
    """JS/TS 共享规则基类（语法在两种语言中完全一致的安全类问题）。"""

    languages = ("javascript", "typescript")


# ==================================================================== bug 类


class LooseEqualityRule(_JsRule):
    """使用 ==/!= 宽松比较（应使用 ===/!==）。"""

    id = "JS-EQEQEQ"
    category = Category.BUG
    severity = Severity.MEDIUM
    description = "使用 `==`/`!=` 宽松比较：触发隐式类型转换（如 0==''、null==undefined 为真），逻辑易错且难排查；应使用 `===`/`!==`。"

    _RE: Pattern[str] = re.compile(r"(?<![=!<>])(==|!=)(?!=)")

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
                        f"第 {idx + 1} 行使用 `==`/`!=` 宽松比较：JavaScript 会对两侧做隐式类型转换"
                        "（`0 == ''`、`'1' == 1`、`null == undefined` 均为真），边界数据下判断结果与直觉相反；"
                        "请改为严格相等 `===`/`!==`。",
                    )
                )
        return hits


class VarDeclarationRule(_JsRule):
    """使用 var 声明变量（无块级作用域、可重复声明、存在变量提升）。"""

    id = "JS-VAR"
    category = Category.BUG
    severity = Severity.LOW
    description = "使用 `var` 声明变量：作用域提升到函数级，循环闭包捕获与重复声明都会产生隐蔽缺陷；应使用 `let`/`const`。"

    _RE: Pattern[str] = re.compile(r"(?<![\w.$])var\s+[\w$]")

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
                        f"第 {idx + 1} 行使用 `var` 声明变量：var 的作用域是整个函数而非代码块，"
                        "声明会被提升、且允许重复声明，循环内闭包捕获的变量值全部相同；"
                        "请改用 `let`/`const` 获得块级作用域。",
                    )
                )
        return hits


class DebuggerStatementRule(_JsRule):
    """debugger 语句残留。"""

    id = "JS-DEBUGGER"
    category = Category.BUG
    severity = Severity.HIGH
    description = "残留 `debugger` 语句：用户浏览器开启 DevTools 时执行到这里会强制断点，直接阻塞线上页面。"

    _RE: Pattern[str] = re.compile(r"(?<![\w.$])debugger\b")

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
                        f"第 {idx + 1} 行存在 `debugger` 语句：本地调试用的断点被提交进代码库，"
                        "用户开启开发者工具时页面会在该处强制暂停，属于阻塞级的调试残留；请删除。",
                    )
                )
        return hits


class TimerStringArgRule(_JsRule):
    """setTimeout/setInterval 首参为字符串（隐式 eval）。"""

    id = "JS-SETTIMEOUT-STRING"
    category = Category.BUG
    severity = Severity.HIGH
    description = "setTimeout/setInterval 的回调以字符串传入：等价于隐式 eval，存在代码注入风险且无法被静态检查；应传函数。"

    _CALL_RE: Pattern[str] = re.compile(r"(?<![\w.$])(?:setTimeout|setInterval)\s*\(")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            for col in find_call(ln, self._CALL_RE):
                span = call_span(scan.masked, idx + 1, col)
                if span is None:
                    continue
                first = span[2].split(",")[0].strip()
                if first[:1] in ("'", '"'):
                    hits.append(
                        self.make_hit(
                            ctx,
                            idx + 1,
                            span[0],
                            f"第 {idx + 1} 行将字符串字面量作为 `setTimeout`/`setInterval` 回调："
                            "字符串会在全局作用域被 eval 执行，拼接外部数据即构成代码注入，"
                            "且延迟执行使错误难以定位；请改为传入函数或箭头函数。",
                        )
                    )
        return hits


class ConstReassignRule(_JsRule):
    """const 声明后同作用域再赋值（启发式，运行时直接 TypeError）。"""

    id = "JS-REASSIGN-CONST-LOOKALIKE"
    category = Category.BUG
    severity = Severity.LOW
    description = "对 const 声明的变量再赋值：运行时抛 TypeError 'Assignment to constant variable'，属必现缺陷。"

    _CONST_RE: Pattern[str] = re.compile(r"^\s*(?:export\s+)?const\s+([\w$]+)\s*(?::[^=]+)?=")
    _ASSIGN_RE: Pattern[str] = re.compile(r"(?<![\w.$'\"`])([\w$]+)\s*(?:[+\-*/%]?=(?![=>]))")
    _DECL_TAIL_RE: Pattern[str] = re.compile(r"\b(?:const|let|var)\s+$")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        # 记录 (声明行号, 变量名, 声明所在大括号深度)
        consts: list[tuple[int, str, int]] = []
        for idx, ln in enumerate(scan.masked):
            m = self._CONST_RE.match(ln)
            if m:
                consts.append((idx + 1, m.group(1), scan.depth_before[idx]))
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            depth = scan.depth_before[idx]
            stripped = ln.strip()
            if not stripped or stripped.startswith(("//", "/*", "*")):
                continue
            for m in self._ASSIGN_RE.finditer(ln):
                name = m.group(1)
                decl = next(
                    (c for c in consts if c[1] == name and c[2] == depth and c[0] < idx + 1),
                    None,
                )
                if decl is None:
                    continue
                # 声明语句本身（const x = ...）不算再赋值
                if self._DECL_TAIL_RE.search(ln[: m.start(1)]):
                    continue
                # 声明后再未重新声明（遮蔽）才报告
                shadowed = any(
                    c[1] == name and c[2] == depth and decl[0] < c[0] < idx + 1 for c in consts
                )
                if shadowed:
                    continue
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行对第 {decl[0]} 行 const 声明的 `{name}` 再次赋值："
                        "const 绑定不可重新赋值，运行到此处会抛出 "
                        "`TypeError: Assignment to constant variable`；"
                        "若确需重新赋值请改用 `let` 声明。",
                        meta={"variable": name},
                    )
                )
        return hits


# ==================================================================== security 类


class EvalExecRule(_JsSharedRule):
    """eval / new Function 动态执行代码。"""

    id = "JS-EVAL-EXEC"
    category = Category.SECURITY
    severity = Severity.CRITICAL
    description = "使用 eval/new Function 动态执行代码：输入可被控制时等价于任意代码执行漏洞。"

    _RE: Pattern[str] = re.compile(r"(?<![\w.$])eval\s*\(|\bnew\s+Function\s*\(")

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
                        f"第 {idx + 1} 行使用 `eval`/`new Function` 动态执行代码：参数一旦受外部输入影响，"
                        "攻击者即可执行任意代码（窃取 cookie/发请求/篡改页面）；"
                        "请改用 JSON.parse、显式分支映射等静态可分析的实现。",
                    )
                )
        return hits


class InnerHtmlAssignRule(_JsSharedRule):
    """innerHTML 赋值动态内容（XSS）。"""

    id = "JS-INNERHTML"
    category = Category.SECURITY
    severity = Severity.HIGH
    description = "向 innerHTML 赋值拼接/变量内容：未转义的 HTML 会被浏览器执行，构成存储型或反射型 XSS。"

    _RE: Pattern[str] = re.compile(r"\.\s*innerHTML\s*=\s*(?!=)")
    _EMPTY_LITERAL_RE: Pattern[str] = re.compile(r"^(['\"`])\1$")
    # 单个纯常量字面量（内容中不再出现任何引号/反引号）
    _CONST_LITERAL_RE: Pattern[str] = re.compile(r"^(['\"`])[^'\"`]*\1$")
    _IDENT_RE: Pattern[str] = re.compile(r"^[A-Za-z_$][\w$.]*$")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            m = self._RE.search(ln)
            if not m:
                continue
            rhs = ln[m.end() :].strip()
            # 右值跨行（以 + 结尾）时向下拼接最多 3 行
            extra = 0
            while rhs.endswith("+") and idx + extra + 1 < len(scan.masked) and extra < 3:
                extra += 1
                rhs = (rhs + " " + scan.masked[idx + extra].strip()).strip()
            rhs = rhs.rstrip(";").strip()
            if not rhs or self._EMPTY_LITERAL_RE.fullmatch(rhs):
                continue  # 清空 innerHTML 属安全用法
            if "+" in rhs or "${" in rhs:
                # 掩码后常量字符串内容已被置空：残留的 +/`${` 只能来自动态拼接
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行向 `innerHTML` 赋值动态内容（拼接/变量/模板插值）："
                        "内容未经过 HTML 转义即被浏览器解析执行，攻击者可注入 "
                        "`<script>` 或事件属性窃取用户会话（XSS）；"
                        "请改用 textContent 或先做转义/消毒（DOMPurify）。",
                    )
                )
                continue
            if self._CONST_LITERAL_RE.fullmatch(rhs):
                continue  # 单个纯常量字符串不含外部输入
            if self._IDENT_RE.fullmatch(rhs):
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行向 `innerHTML` 赋值动态内容（拼接/变量/模板插值）："
                        "内容未经过 HTML 转义即被浏览器解析执行，攻击者可注入 "
                        "`<script>` 或事件属性窃取用户会话（XSS）；"
                        "请改用 textContent 或先做转义/消毒（DOMPurify）。",
                    )
                )
        return hits


class DocumentWriteRule(_JsSharedRule):
    """document.write / document.writeln 调用。"""

    id = "JS-DOCUMENT-WRITE"
    category = Category.SECURITY
    severity = Severity.MEDIUM
    description = "使用 document.write 输出内容：可被注入恶意 HTML（XSS），且在已加载文档上调用会清空整个页面。"

    _RE: Pattern[str] = re.compile(r"\bdocument\s*\.\s*write(?:ln)?\s*\(")

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
                        f"第 {idx + 1} 行调用 `document.write`：写入内容不经过转义即可执行 HTML/JS（XSS 风险），"
                        "页面加载完成后调用还会清空整个文档；请改用 DOM API（createElement/textContent）插入内容。",
                    )
                )
        return hits


_SQL_KEYWORD_RE = re.compile(
    r"\b(?:SELECT\s+[^;]{0,200}?\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM"
    r"|DROP\s+(?:TABLE|DATABASE)|TRUNCATE\s+TABLE|UNION\s+(?:ALL\s+)?SELECT)\b",
    re.IGNORECASE,
)
_CONCAT_HINT_RE = re.compile(r"['\"`]\s*\+|\+\s*['\"`]")


class SqlConcatRule(_JsSharedRule):
    """SQL 语句与 + / 模板插值拼接（注入）。"""

    id = "JS-SQL-CONCAT"
    category = Category.SECURITY
    severity = Severity.CRITICAL
    description = "SQL 语句以字符串拼接方式引入变量：攻击者可注入任意 SQL，导致数据泄露/篡改/删除。"

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        spans_by_line: dict[int, list[tuple[int, int, str]]] = {}
        for lineno, a, b, quote in scan.strings:
            spans_by_line.setdefault(lineno, []).append((a, b, quote))
        hits: list[RuleHit] = []
        for idx, raw in enumerate(ctx.lines):
            spans = spans_by_line.get(idx + 1, [])
            if not spans:
                continue
            # SQL 关键字必须出现在字符串字面量内容内
            sql_in_string = any(
                any(a <= km.start() < b for km in _SQL_KEYWORD_RE.finditer(raw))
                for a, b, _q in spans
            )
            if not sql_in_string:
                continue
            masked = scan.masked[idx]
            # 反引号字符串内容段之后紧跟 `${` 才构成模板插值
            template_interp = any(q == "`" and masked[b : b + 2] == "${" for a, b, q in spans)
            if _CONCAT_HINT_RE.search(masked) or template_interp:
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行的 SQL 语句通过字符串拼接（+ 或模板 `${{}}`）引入变量："
                        "外部输入可直接改变语句语义，构成 SQL 注入漏洞，可能导致数据泄露或被整库删除；"
                        "请改为参数化查询（占位符 ?/$1 + 参数数组）。",
                    )
                )
        return hits


_SECRET_NAME_HINTS = (
    "key", "secret", "token", "password", "passwd", "pwd",
    "apikey", "api_key", "credential", "private",
)
_SECRET_VALUE_PREFIXES = ("sk-", "AKIA", "ghp_")


class HardcodedSecretRule(_JsSharedRule):
    """硬编码密钥/口令（变量名启发式 + 已知密钥前缀）。"""

    id = "JS-HARDCODED-SECRET"
    category = Category.SECURITY
    severity = Severity.CRITICAL
    description = "密钥/口令硬编码在源码中：随代码库扩散，任何有读权限的人都能获取凭据。"

    _EQ_RE: Pattern[str] = re.compile(r"(?P<name>[\w$]+)\s*(?::[^=;]+)?=(?!=)\s*(?P<q>['\"`])")
    _COLON_RE: Pattern[str] = re.compile(r"(?P<name>[\w$]+)\s*:\s*(?P<q>['\"`])")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        hits: list[RuleHit] = []
        for idx, raw in enumerate(ctx.lines):
            m = self._EQ_RE.search(raw) or self._COLON_RE.search(raw)
            if not m:
                continue
            q = m.group("q")
            value = self._string_value(raw, m.end() - 1, q)
            if value is None or "${" in value:
                continue
            name = m.group("name").lower()
            name_hit = any(h in name for h in _SECRET_NAME_HINTS) and len(value) >= 16
            prefix_hit = value.startswith(_SECRET_VALUE_PREFIXES) and len(value) >= 16
            if name_hit or prefix_hit:
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行将疑似敏感凭据硬编码在 `{m.group('name')}` 的字符串字面量中："
                        "密钥随代码库扩散且会永久进入版本历史，任何获得仓库读权限的人都能直接使用；"
                        "应改从环境变量/密钥管理服务读取，并立即轮换已泄露的凭据。",
                        meta={"var": m.group("name")},
                    )
                )
        return hits

    @staticmethod
    def _string_value(raw: str, quote_col: int, quote: str) -> str | None:
        """从 quote_col 处的引号提取字符串内容（处理转义；未闭合返回 None）。"""
        if quote_col >= len(raw) or raw[quote_col] != quote:
            return None
        i = quote_col + 1
        while i < len(raw):
            if raw[i] == "\\":
                i += 2
                continue
            if raw[i] == quote:
                return raw[quote_col + 1 : i]
            i += 1
        return None


# ==================================================================== performance 类


_HTTP_CALL_RE = re.compile(
    r"(?<![\w.$])(?:fetch|axios(?:\s*\.\s*(?:get|post|put|delete|patch|head|request))?)\s*\("
)


class FetchNoTimeoutRule(_JsRule):
    """fetch/axios 请求未设置 timeout 或 signal。"""

    id = "JS-FETCH-NO-TIMEOUT"
    category = Category.PERFORMANCE
    severity = Severity.MEDIUM
    description = "网络请求未设置 timeout/AbortSignal：默认无限等待，远端无响应时请求永久挂起并占用连接。"

    _GUARD_RE: Pattern[str] = re.compile(r"\btimeout\b|\bsignal\b|AbortSignal")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            for col in find_call(ln, _HTTP_CALL_RE):
                span = call_span(scan.masked, idx + 1, col)
                if span is None:
                    continue
                if self._GUARD_RE.search(span[2]):
                    continue
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        span[0],
                        f"第 {idx + 1} 行发起 `fetch`/`axios` 请求未设置 `timeout`/`AbortSignal`："
                        "请求默认无限等待，对端挂起时连接与内存被持续占用，前端表现为页面永久 loading；"
                        "请通过 AbortSignal.timeout(ms) 或 axios 的 timeout 配置显式限定超时。",
                    )
                )
        return hits


# ==================================================================== style 类


class ConsoleLogRule(_JsRule):
    """console.log 调试残留。"""

    id = "JS-CONSOLE-LOG"
    category = Category.STYLE
    severity = Severity.LOW
    description = "使用 console.log 直接输出：绕过日志体系，无级别/上下文，常为调试残留并可能泄露内部数据。"

    _RE: Pattern[str] = re.compile(r"(?<![\w.$])console\s*\.\s*log\s*\(")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        if is_js_test_file(ctx.rel_path):
            return []
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            if self._RE.search(ln):
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行使用 `console.log` 输出：无法按级别关闭、缺乏上下文，"
                        "疑似调试残留，且可能把内部数据打印到用户可见的控制台；"
                        "请删除或改用统一的 logger 封装。",
                    )
                )
        return hits


class LongFunctionRule(_JsRule):
    """函数超过 80 行。"""

    id = "JS-LONG-FUNCTION"
    category = Category.STYLE
    severity = Severity.MEDIUM
    description = "函数体超过 80 行：职责过多、难以测试与复用，应拆分为更小的函数。"

    MAX_LINES = 80

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for fr in function_ranges(scan.masked):
            length = fr.end - fr.start + 1
            if length > self.MAX_LINES:
                hits.append(
                    self.make_hit(
                        ctx,
                        fr.start,
                        fr.end,
                        f"函数 `{fr.name}` 共 {length} 行（第 {fr.start}~{fr.end} 行），超过 80 行上限："
                        "过长函数通常职责混杂、分支爆炸，难以测试和维护；建议按职责拆分。",
                        meta={"lines": length},
                    )
                )
        return hits


class DeepNestingRule(_JsRule):
    """缩进达到 4 层及以上（按文件基础缩进单位换算）。"""

    id = "JS-DEEP-NESTING"
    category = Category.STYLE
    severity = Severity.MEDIUM
    description = "代码嵌套达到 4 层以上：认知负担大、分支组合爆炸，应通过提前返回/抽取函数降低深度。"

    LEVEL_THRESHOLD = 4

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        unit = indent_unit(scan.masked)
        min_indent = self.LEVEL_THRESHOLD * unit
        groups: list[tuple[int, int]] = []
        cur_start: int | None = None
        prev: int | None = None
        for idx in range(1, len(scan.masked) + 1):
            ln = scan.masked[idx - 1]
            stripped = ln.strip()
            deep = bool(stripped) and indent_width(ln) >= min_indent
            if deep:
                if cur_start is None:
                    cur_start = idx
                prev = idx
            elif stripped:
                # 空行不打断分组，非空且未达深度的行打断
                if cur_start is not None and prev is not None:
                    groups.append((cur_start, prev))
                cur_start = None
                prev = None
        if cur_start is not None and prev is not None:
            groups.append((cur_start, prev))
        return [
            self.make_hit(
                ctx,
                a,
                b,
                f"第 {a}~{b} 行代码嵌套达到 {self.LEVEL_THRESHOLD} 层及以上（缩进 ≥{min_indent} 空格）："
                "深层嵌套显著增加理解与测试成本；建议用卫语句提前返回或将内层逻辑抽取成函数。",
            )
            for a, b in groups
        ]


class MagicNumberRule(_JsRule):
    """参与比较/赋值的无解释大数字面量（≥1000，支持十进制与十六进制）。"""

    id = "JS-MAGIC-NUMBER"
    category = Category.STYLE
    severity = Severity.LOW
    description = "代码中出现无命名的大数字面量（≥1000）：含义不明、散落多处难以统一修改，应提取为具名常量。"

    _NUM_RE: Pattern[str] = re.compile(
        r"\b(0[xX][0-9a-fA-F_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)\b"
    )
    # 只跳过 import 与 re-export 行（export const/let 的字面量仍参与检查）
    _IMPORT_RE: Pattern[str] = re.compile(r"^\s*import\b|^\s*export\s*(?:\*|\{)[^;]*\bfrom\b")
    _NAMED_CONST_RE: Pattern[str] = re.compile(
        r"^\s*(?:export\s+)?const\s+[A-Z][A-Z0-9_]*\s*=\s*[^;]*;\s*$"
    )
    MIN_VALUE = 1000.0

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            stripped = ln.strip()
            if not stripped:
                continue
            if self._NAMED_CONST_RE.match(ln):
                continue  # 已提取为全大写具名常量，属推荐写法
            if self._IMPORT_RE.match(ln):
                continue
            for m in self._NUM_RE.finditer(ln):
                text = m.group(1)
                try:
                    if text[:2].lower() == "0x":
                        value = float(int(text.replace("_", ""), 16))
                    else:
                        value = float(text.replace("_", ""))
                except ValueError:  # pragma: no cover
                    continue
                if abs(value) >= self.MIN_VALUE:
                    hits.append(
                        self.make_hit(
                            ctx,
                            idx + 1,
                            idx + 1,
                            f"第 {idx + 1} 行使用魔法数字 `{text}`：字面量含义不明确且多处出现时难以统一维护；"
                            "请提取为具名常量（如 const MAX_RETRY = 3000）并注释业务含义。",
                            meta={"value": value},
                        )
                    )
        return hits


class TodoFixmeCommentRule(_JsRule):
    """TODO/FIXME/HACK 注释残留。"""

    id = "JS-TODO-FIXME"
    category = Category.STYLE
    severity = Severity.LOW
    description = "注释中的 TODO/FIXME/HACK 标记：代表已知未完成事项或临时绕过，应在交付前清理或建跟踪项。"

    _RE: Pattern[str] = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for lineno, text in scan.comments:
            m = self._RE.search(text)
            if m:
                hits.append(
                    self.make_hit(
                        ctx,
                        lineno,
                        lineno,
                        f"第 {lineno} 行注释存在 `{m.group(1)}` 标记：{text.strip()[:80]}——"
                        "这是显式的未完成事项，应转为任务跟踪或立即处理，避免长期滞留。",
                        meta={"tag": m.group(1)},
                    )
                )
        return hits


# ==================================================================== 注册


def build_javascript_rules() -> list[Rule]:
    """构建全部内置 JavaScript 规则实例（顺序即默认报告顺序）。"""
    return [
        # bug
        LooseEqualityRule(),
        VarDeclarationRule(),
        DebuggerStatementRule(),
        TimerStringArgRule(),
        ConstReassignRule(),
        # security（与 TypeScript 共享）
        EvalExecRule(),
        InnerHtmlAssignRule(),
        DocumentWriteRule(),
        SqlConcatRule(),
        HardcodedSecretRule(),
        # performance
        FetchNoTimeoutRule(),
        # style
        ConsoleLogRule(),
        LongFunctionRule(),
        DeepNestingRule(),
        MagicNumberRule(),
        TodoFixmeCommentRule(),
    ]
