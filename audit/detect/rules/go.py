"""Go 静态规则库（W26-C 首轮：3 条，形态对齐 java 版同款规则）。

实现方式与 java.py 同思路：基于模块内 GoScan 的逐行掩码扫描 + 大括号配对作用域
启发式（行级逻辑不依赖 tree-sitter，保证行号精确落在真实代码行上）；AST 佐证走
AstParseGate 既有通路（ctx.tree 可用且证实动态拼接形态时提升置信，tree=None 时
行级命中原样保留——行为等价铁律与 python/java 侧同款）。

首轮口径（已知边界，语料按此写）：
  - GO-SQL-INJECTION 覆盖两形态：字符串 ``+`` 拼接（java 同款单行口径）与
    ``fmt.Sprintf(格式串含 SQL 关键字, 变量...)`` 格式化构造（go 特有惯例，
    仅识别单层参数列表、格式串与变量同行的常规格式）；
    不做 python 版的二级常量模板传播；
  - GO-HARDCODED-SECRET 复用 python/java 同款熵+字典闸门口径（const/var/:=/=
    四种声明形态，解释型字符串按 go 转义语义 skip、反引号原生串不转义）；
  - GO-LONG-FUNCTION 函数头 ``{`` 须在行尾的常规格式（gofmt 惯例），单行函数与
    跨行签名不做；匿名函数字面量 / ``go func(){...}()`` 不计。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from audit.detect.ast_util import confirm_hit, line_node_map
from audit.detect.base import Rule, RuleContext, RuleHit
from audit.detect.rules._rule_families import HardcodedSecretRuleFamily, LongFunctionRuleFamily
from audit.detect.rules._scan_common import indent_width, string_value
from audit.detect.rules.java import _block_end
from audit.detect.rules.python import (
    _PASSWORD_FAMILY_TOKENS,
    _SECRET_NAME_TOKENS,
    HardcodedSecretRule,
    _secret_name_tokens,
)
from audit.indexer.parsers import node_text
from audit.models import Category, Severity

__all__ = ["build_go_rules"]


class _GoRule(Rule):
    """Go 规则公共基类：声明语言。"""

    languages = ("go",)


# ---------------------------------------------------------------- 掩码扫描器


@dataclass
class GoScan:
    """一次 Go 源码扫描的全部产物（行号均为 1-based，列号 0-based）。

    - masked：字符串/原生串/rune 字面量内容与注释被替换为空格后的行（引号与
      定界符保留位置），规则在其上匹配可天然避免命中字符串与注释；
    - comments：(行号, 注释文本)，与 JavaScan 同形状；
    - strings：(行号, 内容起始列, 内容结束列(不含), 引号字符)，描述每行上
      字符串字面量"内容"的跨度（不含引号）；反引号原生串（raw string）可跨行，
      逐行记录内容跨度（go 惯例用原生串写 SQL，跨行也要能检出 SQL 关键字）。
    """

    masked: list[str]
    comments: list[tuple[int, str]] = field(default_factory=list)
    strings: list[tuple[int, int, int, str]] = field(default_factory=list)


def scan_go(lines: list[str]) -> GoScan:
    """逐字符状态机扫描：产出掩码行、注释清单与字符串内容跨度。

    与 java 版差异两处：反引号原生串（raw string）跨行且无转义；' 是 rune
    字面量（单行、反斜杠转义，如 '\\''）。解释型 " 字符串不跨行（未闭合按
    行尾截断回 code，防御语法残缺误报扩散）。
    """
    chars = [list(ln) for ln in lines]
    scan = GoScan(masked=[])

    def blank(li: int, a: int, b: int) -> None:
        for k in range(a, min(b, len(chars[li]))):
            chars[li][k] = " "

    def add_span(li: int, a: int, b: int, quote: str) -> None:
        if b > a:
            scan.strings.append((li + 1, a, b, quote))

    mode = "code"  # code | dq | rq | rune | line_comment | block_comment
    for i, cur in enumerate(lines):
        j = 0
        while j < len(cur):
            c = cur[j]

            if mode == "line_comment":
                scan.comments.append((i + 1, cur[j:].strip()))
                blank(i, j, len(cur))
                mode = "code"
                break

            if mode == "block_comment":
                end = cur.find("*/", j)
                if end < 0:
                    text = cur[j:].strip()
                    if text:
                        scan.comments.append((i + 1, text))
                    blank(i, j, len(cur))
                    break
                text = cur[j:end].strip()
                if text:
                    scan.comments.append((i + 1, text))
                blank(i, j, end + 2)
                j = end + 2
                mode = "code"
                continue

            if mode == "rq":  # 反引号原生串：跨行、无转义，逐行记录内容跨度
                end = cur.find("`", j)
                if end < 0:
                    add_span(i, j, len(cur), "`")
                    blank(i, j, len(cur))
                    break  # 行尾：原生串跨行继续
                add_span(i, j, end, "`")
                blank(i, j, end + 1)
                j = end + 1
                mode = "code"
                continue

            if mode in ("dq", "rune"):
                q = '"' if mode == "dq" else "'"
                seg_start = j
                closed = False
                while j < len(cur):
                    ch = cur[j]
                    if ch == "\\":  # go 解释型字符串/rune 的转义语义：跳过 \x 两字符
                        nj = min(j + 1, len(cur) - 1)
                        blank(i, j, nj + 1)
                        j = nj + 1
                        continue
                    if ch == q:
                        closed = True
                        break
                    j += 1
                end_col = j if closed else len(cur)
                add_span(i, seg_start, end_col, q)
                blank(i, seg_start, end_col)
                # 单行字面量：未闭合按行尾截断回 code，避免误报扩散
                mode = "code"
                if closed:
                    j = end_col + 1
                    continue
                break

            # ---------------- code 模式 ----------------
            if c == "/" and j + 1 < len(cur) and cur[j + 1] == "/":
                mode = "line_comment"
                continue
            if c == "/" and j + 1 < len(cur) and cur[j + 1] == "*":
                mode = "block_comment"
                j += 2
                continue
            if c == "`":
                mode = "rq"
                j += 1
                continue
            if c == '"':
                mode = "dq"
                j += 1
                continue
            if c == "'":
                mode = "rune"
                j += 1
                continue
            j += 1
        # 内层 break（跨行内容/未闭合字符串）保持 mode 进入下一行；行注释分支已就地重置

    scan.masked = ["".join(row) for row in chars]
    return scan


def get_scan(ctx_lines: list[str], meta: dict) -> GoScan:
    """优先复用引擎预计算的扫描结果（meta["goscan"]），否则现算。"""
    scan = meta.get("goscan")
    if isinstance(scan, GoScan):
        return scan
    return scan_go(ctx_lines)


# ---------------------------------------------------------------- 作用域


@dataclass
class GoFuncRange:
    """函数/方法作用域（1-based 闭区间 [start, end]）。"""

    name: str
    start: int
    end: int
    indent: int
    kind: str = "function"


# 函数头：func [ (receiver) ] [类型形参] Name(args) [结果] {（`{` 须在行尾，gofmt 常规格式）
_FUNC_HEAD_RE = re.compile(
    r"^\s*func\s+(?:\([^)]*\)\s*)?(?:\[[^\]]*\]\s*)?([A-Za-z_]\w*)\s*\([^;{}]*\)[^{]*\{\s*$"
)


def function_ranges_go(masked: list[str]) -> list[GoFuncRange]:
    """推断全部函数/方法作用域（要求函数头 ``{`` 在行尾的常规格式）。

    掩码行匹配保证字符串/注释中的伪函数头不参与；单行函数/跨行签名不做
    （首轮口径）；匿名函数字面量无名字且不以行首 func 开头，天然不匹配。
    """
    out: list[GoFuncRange] = []
    for idx, ln in enumerate(masked):
        m = _FUNC_HEAD_RE.match(ln)
        if not m:
            continue
        end = _block_end(masked, idx)
        if end is None or end <= idx:
            continue
        out.append(GoFuncRange(name=m.group(1), start=idx + 1, end=end, indent=indent_width(ln)))
    return out


# ---------------------------------------------------------------- security 规则

# 与 python.py / js/javascript.py / java.py 同款 SQL 关键字形态（各语言持有一份拷贝）
_SQL_KEYWORD_RE = re.compile(
    r"\b(?:SELECT\s+[^;]{0,200}?\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM"
    r"|DROP\s+(?:TABLE|DATABASE)|TRUNCATE\s+TABLE|UNION\s+(?:ALL\s+)?SELECT)\b",
    re.IGNORECASE,
)
# go 拼接形态：+ 紧邻引号（含反引号原生串）
_CONCAT_HINT_RE = re.compile(r"['\"`]\s*\+|\+\s*['\"`]")
# fmt.Sprintf / Sprintf 格式化构造：参数列表至少两项（格式串后还有变量实参）
_SPRINTF_HINT_RE = re.compile(r"\bSprintf\s*\([^()]*,")

# SQL 执行点（database/sql 惯例 + gorm Raw）
_EXECUTE_CALL_RE = re.compile(
    r"\.\s*(Query(?:Row)?(?:Context)?|Exec(?:Context)?|Prepare(?:Context)?|Raw)\s*\("
)


def _is_dynamic_concat(node, source: bytes) -> bool:
    """AST 判定（go 佐证口径）：+ 拼接含字符串字面量，或 Sprintf 调用。"""
    if node.type == "binary_expression":
        return any(c.type in ("interpreted_string_literal", "raw_string_literal") for c in node.children)
    if node.type == "call_expression":
        func = node.child_by_field_name("function")
        return func is not None and node_text(func, source).endswith("Sprintf")
    return False


class GoSqlInjectionRule(_GoRule):
    """SQL 语句以拼接/格式化方式引入变量（注入；形态对齐 JAVA-SQL-INJECTION）。

    判定：SQL 关键字必须出现在字符串字面量内容内，且该行（掩码后）存在
    `+` 紧邻引号的拼接形态，或 `Sprintf(` 带变量实参的格式化形态；命中行上有
    执行点调用时在文案中指认执行点。AST 佐证：ctx.tree 可用且行上证实
    动态拼接/Sprintf 调用时提升置信；tree=None 时行级命中原样保留（绝不删减）。
    """

    id = "GO-SQL-INJECTION"
    category = Category.SECURITY
    severity = Severity.CRITICAL
    description = "SQL 语句以字符串拼接/格式化方式引入变量：攻击者可注入任意 SQL，导致数据泄露/篡改/删除。"

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
                any(a <= km.start() < b for km in _SQL_KEYWORD_RE.finditer(raw)) for a, b, _q in spans
            )
            if not sql_in_string:
                continue
            masked = scan.masked[idx]
            is_sprintf = _SPRINTF_HINT_RE.search(masked) is not None
            is_concat = _CONCAT_HINT_RE.search(masked) is not None
            if not is_sprintf and not is_concat:
                continue
            executor = _EXECUTE_CALL_RE.search(masked)
            form = "Sprintf 格式化" if is_sprintf else "字符串拼接（+）"
            tail = f"拼接结果传入 `{executor.group(1)}(` 执行" if executor else "构造出的 SQL 交由执行点执行"
            hits.append(
                self.make_hit(
                    ctx,
                    idx + 1,
                    idx + 1,
                    f"第 {idx + 1} 行的 SQL 语句通过{form}引入变量，{tail}："
                    "外部输入可直接改变语句语义，构成 SQL 注入漏洞，可能导致数据泄露或被整库删除；"
                    "请改为参数化查询（占位符 $1/? + db.Query 参数绑定）。",
                )
            )
        # ---- AST 佐证（保守口径：只在 AST 证实动态拼接形态时提升置信） ----
        self._ast_confirm(ctx, hits)
        hits.sort(key=lambda h: h.line_start)
        return hits

    def _ast_confirm(self, ctx: RuleContext, hits: list[RuleHit]) -> None:
        """ctx.tree 为 None（无解析器/降级/环境关闭）→ 空操作；佐证失败 → 命中原样保留。"""
        if ctx.tree is None or not hits:
            return
        source = ctx.source.encode("utf-8", errors="replace")
        nodes_by_line = line_node_map(ctx.tree)
        for hit in hits:
            nodes = nodes_by_line.get(hit.line_start, [])
            if any(_is_dynamic_concat(n, source) for n in nodes):
                confirm_hit(hit)


class GoHardcodedSecretRule(_GoRule, HardcodedSecretRuleFamily):
    """硬编码密钥/口令（复用 python/java 同款熵+字典闸门口径，R1-9/R4-1/R4-7 判定一致）。

    标识符按 _/驼峰分词并做单数归一后精确命中敏感词，值满足随机性校验
    （ASCII、长度足够、Shannon 熵 ≥3.5 或字符集 ≥3 类；password 家族放低为
    熵 ≥3.0 或字符集 ≥2 类）；或值为 sk- 前缀的 API key 形态。
    声明形态：const/var 声明、:= 短声明、= 赋值（含结构体内字段行不匹配的
    防御口径）；解释型字符串按 go 转义语义（skip），反引号原生串不转义。

    W30 注释行掩码预检：raw 行命中声明形态而 masked 行不再命中（赋值形态只在
    于注释/字符串内容，如块注释中间行、反引号原生串中间行的假声明）时跳过
    不报；masked 行保留引号/反引号开定界符位置，真实赋值行（含 `x = ` 原生串）
    的形态不受影响。
    """

    id = "GO-HARDCODED-SECRET"

    # go 声明形态：[const|var] NAME [类型] = / := "..."（go 类型在名后，与 java 相反）
    _ASSIGN_RE = re.compile(
        r"^\s*(?:(?:const|var)\s+)?(?P<name>[A-Za-z_]\w*)\s*(?:(?:[A-Za-z_][\w.*\[\]]*)\s+)?"
        r"(?::=|=)\s*(?P<q>['\"`])"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, raw in enumerate(ctx.lines):
            m = self._ASSIGN_RE.match(raw)
            if not m:
                continue
            # W30 注释行掩码预检（判定口径零变化）：masked 行不再命中 ⇒ 跳过
            if self._is_comment_only(raw, scan.masked[idx]):
                continue
            q_col = m.end() - 1
            # 解释型 " 走 go 转义语义（skip）；反引号原生串无转义（none）
            escape_mode = "none" if raw[q_col] == "`" else "skip"
            value = string_value(raw, q_col, raw[q_col], escape_mode=escape_mode)
            if value is None:
                continue
            name = m.group("name")
            name_tokens = _secret_name_tokens(name) & _SECRET_NAME_TOKENS
            if name_tokens:
                if name_tokens & _PASSWORD_FAMILY_TOKENS:
                    secretish = HardcodedSecretRule._looks_secretish_relaxed(value)  # R4-7：口令家族放低闸门
                else:
                    secretish = HardcodedSecretRule._looks_secretish(value)
                name_hit = len(value) >= 16 and secretish
            else:
                name_hit = False
            sk_hit = value.startswith("sk-") and len(value) >= 20 and HardcodedSecretRule._looks_secretish(value)
            if name_hit or sk_hit:
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行将疑似敏感凭据硬编码在 `{name}` 的字符串字面量中："
                        "密钥随代码库扩散且会进入版本历史，泄露风险极高；应改从环境变量/密钥管理服务读取，"
                        "并立即轮换已泄露的凭据。",
                        meta={"var": name},
                    )
                )
        return hits

    def _is_comment_only(self, raw: str, masked: str) -> bool:
        """W30 注释行掩码预检：raw 行命中声明形态而 masked 行不再命中时为 True。

        掩码扫描器把字符串/原生串内容与注释置为空格并保留开定界符位置（双引号
        开闭均保留，反引号保留开定界符）：真实赋值行 `NAME = "`/``NAME = ` ``
        形态在 masked 行依旧成立（预检放行、不误杀）；仅当该形态只存在于注释/
        字符串内容（块注释中间行、反引号原生串中间行的假声明等）时 masked 行
        才会失配，按注释行跳过不报。
        """
        return self._ASSIGN_RE.match(raw) is not None and self._ASSIGN_RE.match(masked) is None


# ---------------------------------------------------------------- style 规则


class GoLongFunctionRule(_GoRule, LongFunctionRuleFamily):
    """函数超过 80 行。"""

    id = "GO-LONG-FUNCTION"

    # W15-D 家族钩子：消息尾注与 go 掩码扫描器、大括号配对口径的作用域推断
    _TAIL_DETAIL = "分支爆炸"

    _get_scan = staticmethod(get_scan)
    _function_ranges = staticmethod(function_ranges_go)


# ---------------------------------------------------------------- 注册


def build_go_rules() -> list[Rule]:
    """构建全部内置 Go 规则实例（顺序即默认报告顺序）。"""
    return [
        # security
        GoSqlInjectionRule(),
        GoHardcodedSecretRule(),
        # style
        GoLongFunctionRule(),
    ]
