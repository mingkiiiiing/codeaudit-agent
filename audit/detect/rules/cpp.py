"""C++ 静态规则库（W28-C 首轮：3 条，形态对齐 go/java 版同款规则）。

实现方式与 go.py 同思路：基于模块内 CppScan 的逐行掩码扫描 + 大括号配对作用域
启发式（行级逻辑不依赖 tree-sitter，保证行号精确落在真实代码行上）；AST 佐证走
AstParseGate 既有通路（ctx.tree 可用且证实动态拼接形态时提升置信，tree=None 时
行级命中原样保留——行为等价铁律与 python/java/go 侧同款）。

首轮口径（已知边界，语料按此写）：
  - CPP-SQL-INJECTION 覆盖两形态：字符串 ``+`` 拼接（java/go 同款单行口径）与
    printf 家族（sprintf/snprintf/fprintf）/ format（std::format / fmt::format）
    的「格式串含 SQL 关键字 + 变量实参」格式化构造（C++ 特有惯例，仅识别单层
    参数列表、格式串与变量同行的常规格式）；不做 python 版的二级常量模板传播；
  - CPP-HARDCODED-SECRET 复用 python/java/go 同款熵+字典闸门口径（``#define``
    宏、``const``/``constexpr`` 声明、普通 ``=`` 赋值三种形态；字符串按 C++ 转义
    语义 skip）；命名空间限定赋值（``ns::Name = "..."``）不做；
  - CPP-LONG-FUNCTION 函数头 ``{`` 须在行尾的常规格式（含尾置 const/noexcept/
    override/final 与构造函数初始化列表的常规单段形态）；跨行签名与
    ``operator`` 重载（名含 ``=`` 等非标识符字符）不做。
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

__all__ = ["build_cpp_rules"]


class _CppRule(Rule):
    """C++ 规则公共基类：声明语言。"""

    languages = ("cpp",)


# ---------------------------------------------------------------- 掩码扫描器


@dataclass
class CppScan:
    """一次 C++ 源码扫描的全部产物（行号均为 1-based，列号 0-based）。

    - masked：字符串/字符字面量内容与注释被替换为空格后的行（引号与定界符保留
      位置），规则在其上匹配可天然避免命中字符串与注释；
    - comments：(行号, 注释文本)，与 GoScan 同形状；
    - strings：(行号, 内容起始列, 内容结束列(不含), 引号字符)，描述每行上
      字符串字面量"内容"的跨度（不含引号）。

    已知边界：C++ 原始串 ``R\"(...)\"`` 不做专门扫描（按普通代码字符处理，语料
    不用原始串承载待检内容）。
    """

    masked: list[str]
    comments: list[tuple[int, str]] = field(default_factory=list)
    strings: list[tuple[int, int, int, str]] = field(default_factory=list)


def scan_cpp(lines: list[str]) -> CppScan:
    """逐字符状态机扫描：产出掩码行、注释清单与字符串内容跨度。

    与 java 版同构（' 字符字面量与 \" 字符串均不跨行，未闭合按行尾截断回 code，
    防御语法残缺误报扩散）；反斜杠转义按 C++ 语义跳过。
    """
    chars = [list(ln) for ln in lines]
    scan = CppScan(masked=[])
    n = len(lines)

    def blank(li: int, a: int, b: int) -> None:
        for k in range(a, min(b, len(chars[li]))):
            chars[li][k] = " "

    def add_span(li: int, a: int, b: int, quote: str) -> None:
        if b > a:
            scan.strings.append((li + 1, a, b, quote))

    mode = "code"  # code | sq | dq | line_comment | block_comment
    i = 0
    while i < n:
        cur = lines[i]
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

            if mode in ("sq", "dq"):
                q = "'" if mode == "sq" else '"'
                seg_start = j
                closed = False
                while j < len(cur):
                    ch = cur[j]
                    if ch == "\\":  # C++ 转义语义：跳过 \x 两字符
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
                # 字符/字符串不允许跨行：未闭合按行尾截断回 code，避免误报扩散
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
            if c == '"':
                mode = "dq"
                j += 1
                continue
            if c == "'":
                mode = "sq"
                j += 1
                continue
            j += 1
        # 内层 break（未闭合字符串）已在分支内重置回 code；行注释分支已就地重置
        i += 1

    scan.masked = ["".join(row) for row in chars]
    return scan


def get_scan(ctx_lines: list[str], meta: dict) -> CppScan:
    """优先复用引擎预计算的扫描结果（meta["cppscan"]），否则现算。"""
    scan = meta.get("cppscan")
    if isinstance(scan, CppScan):
        return scan
    return scan_cpp(ctx_lines)


# ---------------------------------------------------------------- 作用域


@dataclass
class CppFuncRange:
    """函数/方法作用域（1-based 闭区间 [start, end]）。"""

    name: str
    start: int
    end: int
    indent: int
    kind: str = "function"


# 函数头：[template<...>] [返回类型] [Class::]Name(args) [尾置说明符] [初始化列表] {
# （`{` 须在行尾，常规格式；`\([^;{}]*\)` 天然排除 for/while 的分号头）
_CPP_FUNC_HEAD_RE = re.compile(
    r"^\s*(?:template\s*<[^>]*>\s*)?"
    r"(?:[\w:<>,~*&\s]+?\s+)?"
    r"(?P<name>~?[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\s*"
    r"\([^;{}]*\)\s*"
    r"(?:\s+(?:const|noexcept|override|final)\b)*\s*"
    r"(?:->\s*[\w:<>*&\s]+?\s*)?"
    r"(?::\s*[^{;]+?\s*)?"
    r"\{\s*$"
)

# 控制流关键字排除（if/for 等与函数头同形态）
_CPP_FUNC_EXCLUDE = frozenset(
    ["if", "for", "while", "switch", "catch", "return", "else", "do", "try", "throw", "sizeof", "new", "delete"]
)


def function_ranges_cpp(masked: list[str]) -> list[CppFuncRange]:
    """推断全部函数/方法作用域（要求函数头 ``{`` 在行尾的常规格式）。

    掩码行匹配保证字符串/注释中的伪函数头不参与；跨行签名/operator 重载不做
    （首轮口径）；lambda（``= [&]() {`` 带非标识符前导字符）天然不匹配。
    """
    out: list[CppFuncRange] = []
    for idx, ln in enumerate(masked):
        m = _CPP_FUNC_HEAD_RE.match(ln)
        if not m or m.group("name").lstrip("~") in _CPP_FUNC_EXCLUDE:
            continue
        end = _block_end(masked, idx)
        if end is None or end <= idx:
            continue
        out.append(CppFuncRange(name=m.group("name"), start=idx + 1, end=end, indent=indent_width(ln)))
    return out


# ---------------------------------------------------------------- security 规则

# 与 python.py / js/javascript.py / java.py / go.py 同款 SQL 关键字形态（各语言持有一份拷贝）
_SQL_KEYWORD_RE = re.compile(
    r"\b(?:SELECT\s+[^;]{0,200}?\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM"
    r"|DROP\s+(?:TABLE|DATABASE)|TRUNCATE\s+TABLE|UNION\s+(?:ALL\s+)?SELECT)\b",
    re.IGNORECASE,
)
# 拼接形态：+ 紧邻引号
_CONCAT_HINT_RE = re.compile(r"['\"]\s*\+|\+\s*['\"]")
# printf 家族 / format 格式化构造：参数列表至少两项（格式串后还有变量实参）
_FORMAT_HINT_RE = re.compile(r"\b(?:(?:sn?|f)printf|format)\s*\([^()]*,")

# SQL 执行点（C++ 常用 DB API：JDBC 风格 -> 调用与 C 风格 mysql/PQ/sqlite 函数）
_EXECUTE_CALL_RE = re.compile(
    r"(?:\.\s*|->\s*)(?:execute(?:Query|Update|Batch)?|prepareStatement|prepareCall|createQuery"
    r"|createSQLQuery|createNativeQuery)\s*\("
    r"|\b(?:mysql_query|mysql_real_query|PQexec|PQexecParams|sqlite3_exec)\s*\("
)


def _is_dynamic_concat(node, source: bytes) -> bool:
    """AST 判定（cpp 佐证口径）：+ 拼接含字符串字面量，或 printf/format 格式化调用。"""
    if node.type == "binary_expression":
        return any(c.type == "string_literal" for c in node.children)
    if node.type == "call_expression":
        func = node.child_by_field_name("function")
        if func is None:
            return False
        head = node_text(func, source).rsplit("::", 1)[-1]
        return head in ("snprintf", "sprintf", "fprintf", "format")
    return False


class CppSqlInjectionRule(_CppRule):
    """SQL 语句以拼接/格式化方式引入变量（注入；形态对齐 GO-SQL-INJECTION）。

    判定：SQL 关键字必须出现在字符串字面量内容内，且该行（掩码后）存在
    `+` 紧邻引号的拼接形态，或 printf 家族 / format 带变量实参的格式化形态；
    命中行上有执行点调用时在文案中指认执行点。AST 佐证：ctx.tree 可用且行上
    证实动态拼接/格式化调用时提升置信；tree=None 时行级命中原样保留（绝不删减）。
    """

    id = "CPP-SQL-INJECTION"
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
            is_format = _FORMAT_HINT_RE.search(masked) is not None
            is_concat = _CONCAT_HINT_RE.search(masked) is not None
            if not is_format and not is_concat:
                continue
            executor = _EXECUTE_CALL_RE.search(masked)
            form = "格式化（printf 家族 / format）" if is_format else "字符串拼接（+）"
            tail = f"拼接结果传入 `{executor.group(1)}(` 执行" if executor else "构造出的 SQL 交由执行点执行"
            hits.append(
                self.make_hit(
                    ctx,
                    idx + 1,
                    idx + 1,
                    f"第 {idx + 1} 行的 SQL 语句通过{form}引入变量，{tail}："
                    "外部输入可直接改变语句语义，构成 SQL 注入漏洞，可能导致数据泄露或被整库删除；"
                    "请改为参数化查询（占位符 ? + 预编译语句参数绑定）。",
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


class CppHardcodedSecretRule(_CppRule, HardcodedSecretRuleFamily):
    """硬编码密钥/口令（复用 python/java/go 同款熵+字典闸门口径，R1-9/R4-1/R4-7 判定一致）。

    标识符按 _/驼峰分词并做单数归一后精确命中敏感词，值满足随机性校验
    （ASCII、长度足够、Shannon 熵 ≥3.5 或字符集 ≥3 类；password 家族放低为
    熵 ≥3.0 或字符集 ≥2 类）；或值为 sk- 前缀的 API key 形态。
    声明形态：``#define NAME \"...\"`` 宏、``const``/``constexpr`` 类型声明、普通
    ``=`` 赋值（含限定名 ns::Name）；字符串按 C++ 转义语义（skip）。

    W30 注释行掩码预检：raw 行命中声明形态而 masked 行不再命中（赋值形态只在
    于注释/字符串内容，如块注释中间行的假 ``#define``/赋值）时跳过不报；
    masked 行保留引号定界符位置，真实赋值行/宏定义行的形态不受影响。
    """

    id = "CPP-HARDCODED-SECRET"

    # cpp 声明形态两分支：#define NAME "..."（宏，无 = 号）与
    # [修饰符]* [类型 [*&]] NAME = "..."（含限定名 ns::Name）
    _ASSIGN_RE = re.compile(
        r"^\s*(?:#\s*define\s+(?P<macro>~?[A-Za-z_]\w*)\s*|"
        r"(?:(?:const(?:expr)?|static|inline|mutable|volatile|unsigned)\s+)*"
        r"(?:[\w:]+(?:<[^=]*>)?(?:\s*[*&])*\s+)?"
        r"(?P<name>~?[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\s*=\s*)\""
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
            # C++ 解释型字符串走转义语义（skip）
            value = string_value(raw, q_col, '"', escape_mode="skip")
            if value is None:
                continue
            name = m.group("macro") or m.group("name")
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

        掩码扫描器把字符串内容与注释置为空格但保留引号定界符位置：真实赋值行/
        宏定义行 `NAME = "`/`#define NAME "` 形态在 masked 行依旧成立（预检放行、
        不误杀）；仅当该形态只存在于注释/字符串内容（块注释中间行的假声明等）
        时 masked 行才会失配，按注释行跳过不报。
        """
        return self._ASSIGN_RE.match(raw) is not None and self._ASSIGN_RE.match(masked) is None


# ---------------------------------------------------------------- style 规则


class CppLongFunctionRule(_CppRule, LongFunctionRuleFamily):
    """函数超过 80 行。"""

    id = "CPP-LONG-FUNCTION"

    # W15-D 家族钩子：消息尾注与 cpp 掩码扫描器、大括号配对口径的作用域推断
    _TAIL_DETAIL = "分支爆炸"

    _get_scan = staticmethod(get_scan)
    _function_ranges = staticmethod(function_ranges_cpp)


# ---------------------------------------------------------------- 注册


def build_cpp_rules() -> list[Rule]:
    """构建全部内置 C++ 规则实例（顺序即默认报告顺序）。"""
    return [
        # security
        CppSqlInjectionRule(),
        CppHardcodedSecretRule(),
        # style
        CppLongFunctionRule(),
    ]
