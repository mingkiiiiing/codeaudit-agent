"""Java 静态规则库（W24-A 首轮：3 条，形态对齐 python 版同款规则）。

实现方式与 python.py / js/javascript.py 同思路：基于模块内 JavaScan 的逐行掩码
扫描 + 大括号配对作用域启发式（行级逻辑不依赖 tree-sitter，保证行号精确落在
真实代码行上）；AST 佐证走 AstParseGate 既有通路（ctx.tree 可用且证实动态拼接
形态时提升置信，tree=None 时行级命中原样保留——行为等价铁律与 python 侧同款）。

首轮口径（已知边界，语料按此写）：泛型/注解/lambda 内调用不做；
JAVA-SQL-INJECTION 只做单行"SQL 字面量 + 拼接"口径（不做 python 版的
二级常量模板传播）；JAVA-HARDCODED-SECRET 复用 python 同款熵闸门口径。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from audit.detect.ast_util import confirm_hit, line_node_map
from audit.detect.base import Rule, RuleContext, RuleHit
from audit.detect.rules._rule_families import HardcodedSecretRuleFamily, LongFunctionRuleFamily
from audit.detect.rules._scan_common import indent_width, string_value
from audit.detect.rules.python import (
    _PASSWORD_FAMILY_TOKENS,
    _SECRET_NAME_TOKENS,
    HardcodedSecretRule,
    _secret_name_tokens,
)
from audit.models import Category, Severity

__all__ = ["build_java_rules"]


class _JavaRule(Rule):
    """Java 规则公共基类：声明语言。"""

    languages = ("java",)


# ---------------------------------------------------------------- 掩码扫描器


@dataclass
class JavaScan:
    """一次 Java 源码扫描的全部产物（行号均为 1-based，列号 0-based）。

    - masked：字符串/文本块/字符字面量内容与注释被替换为空格后的行（引号与
      定界符保留位置），规则在其上匹配可天然避免命中字符串与注释；
    - comments：(行号, 注释文本)，与 PyScan/JsScan 同形状；
    - strings：(行号, 内容起始列, 内容结束列(不含), 引号字符)，描述每行上
      字符串字面量"内容"的跨度（不含引号）；文本块（连续三引号）内容整体
      掩码、不记录跨度（首轮口径，语料不使用文本块承载待检内容）。
    """

    masked: list[str]
    comments: list[tuple[int, str]] = field(default_factory=list)
    strings: list[tuple[int, int, int, str]] = field(default_factory=list)


def scan_java(lines: list[str]) -> JavaScan:
    """逐字符状态机扫描：产出掩码行、注释清单与字符串内容跨度。

    java 无模板字符串/正则字面量，状态比 js 版简单：' 字符字面量与 "
    字符串均不跨行（未闭合按行尾截断回 code，防御语法残缺误报扩散）；
    连续三引号的文本块跨行，内容整体掩码。
    """
    chars = [list(ln) for ln in lines]
    scan = JavaScan(masked=[])
    n = len(lines)

    def blank(li: int, a: int, b: int) -> None:
        for k in range(a, min(b, len(chars[li]))):
            chars[li][k] = " "

    def add_span(li: int, a: int, b: int, quote: str) -> None:
        if b > a:
            scan.strings.append((li + 1, a, b, quote))

    mode = "code"  # code | sq | dq | text_block | line_comment | block_comment
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

            if mode == "text_block":
                end = cur.find('"""', j)
                if end < 0:
                    blank(i, j, len(cur))
                    break  # 行尾：文本块跨行继续
                blank(i, j, end + 3)
                j = end + 3
                mode = "code"
                continue

            if mode in ("sq", "dq"):
                q = "'" if mode == "sq" else '"'
                seg_start = j
                closed = False
                while j < len(cur):
                    ch = cur[j]
                    if ch == "\\":
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
            if c == '"' and cur[j : j + 3] == '"""':
                blank(i, j, j + 3)
                j += 3
                mode = "text_block"
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
        # 内层 break（跨行内容/未闭合字符串）保持 mode 进入下一行；行注释分支已就地重置
        i += 1

    scan.masked = ["".join(row) for row in chars]
    return scan


def get_scan(ctx_lines: list[str], meta: dict) -> JavaScan:
    """优先复用引擎预计算的扫描结果（meta["javascan"]），否则现算。"""
    scan = meta.get("javascan")
    if isinstance(scan, JavaScan):
        return scan
    return scan_java(ctx_lines)


# ---------------------------------------------------------------- 作用域


@dataclass
class JavaFuncRange:
    """方法/构造器作用域（1-based 闭区间 [start, end]）。"""

    name: str
    start: int
    end: int
    indent: int
    kind: str = "method"


_METHOD_EXCLUDE = frozenset(["if", "for", "while", "switch", "catch", "return", "new"])

_METHOD_HEAD_RE = re.compile(
    r"^\s*(?:(?:public|private|protected|static|final|abstract|synchronized|native|strictfp|default)\s+)*"
    r"(?:[\w$][\w$.<>\[\],?\s]*?\s+)?"
    r"([\w$]+)\s*\([^;{}]*\)\s*(?:throws\s+[\w$.,\s]+)?\{\s*$"
)


def _block_end(masked: list[str], start_idx0: int) -> int | None:
    """从第 start_idx0 行（0-based）起做 `{` 配对，返回块结束行（1-based）；不闭合返回 None。"""
    bal = 0
    found_open = False
    for k in range(start_idx0, len(masked)):
        for ch in masked[k]:
            if ch == "{":
                bal += 1
                found_open = True
            elif ch == "}":
                bal -= 1
                if found_open and bal <= 0:
                    return k + 1
    return len(masked) if found_open else None


def function_ranges_java(masked: list[str]) -> list[JavaFuncRange]:
    """推断全部方法/构造器作用域（要求方法头 `{` 在行尾的常规格式）。

    掩码行匹配保证字符串/注释中的伪方法头不参与；抽象/接口方法以 `;` 结尾
    无函数体，天然不匹配。泛型返回类型 / throws 子句随头正则一并消化。
    """
    out: list[JavaFuncRange] = []
    for idx, ln in enumerate(masked):
        m = _METHOD_HEAD_RE.match(ln)
        if not m or m.group(1) in _METHOD_EXCLUDE:
            continue
        end = _block_end(masked, idx)
        if end is None or end <= idx:
            continue
        out.append(JavaFuncRange(name=m.group(1), start=idx + 1, end=end, indent=indent_width(ln)))
    return out


# ---------------------------------------------------------------- security 规则

# 与 python.py / js/javascript.py 同款 SQL 关键字形态（三语言各自持有一份拷贝）
_SQL_KEYWORD_RE = re.compile(
    r"\b(?:SELECT\s+[^;]{0,200}?\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM"
    r"|DROP\s+(?:TABLE|DATABASE)|TRUNCATE\s+TABLE|UNION\s+(?:ALL\s+)?SELECT)\b",
    re.IGNORECASE,
)
# java 无模板字符串，拼接形态只有 + 紧邻引号
_CONCAT_HINT_RE = re.compile(r"['\"]\s*\+|\+\s*['\"]")

# SQL 执行点（任务卡口径：拼接 SQL 进 execute/prepareStatement/createQuery 等）
_EXECUTE_CALL_RE = re.compile(
    r"\.\s*(execute(?:Query|Update|Batch|LargeUpdate)?|prepareStatement|prepareCall|createQuery"
    r"|createSQLQuery|createNativeQuery)\s*\("
)


def _is_dynamic_concat(node, source: bytes) -> bool:
    """AST 判定（java 佐证口径）：节点是否为"字符串参与 + 拼接"的二元表达式。"""
    return node.type == "binary_expression" and any(c.type == "string_literal" for c in node.children)


class JavaSqlInjectionRule(_JavaRule):
    """SQL 语句与 + 拼接（注入；形态对齐 PY-SQL-INJECTION 单行口径）。

    判定：SQL 关键字必须出现在字符串字面量内容内，且该行（掩码后）存在
    `+` 紧邻引号的拼接形态；命中行上有执行点调用时在文案中指认执行点。
    AST 佐证：ctx.tree 可用且行上证实 `binary_expression` 含字符串字面量时
    提升置信；tree=None 时行级命中原样保留（绝不删减）。
    """

    id = "JAVA-SQL-INJECTION"
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
                any(a <= km.start() < b for km in _SQL_KEYWORD_RE.finditer(raw)) for a, b, _q in spans
            )
            if not sql_in_string:
                continue
            masked = scan.masked[idx]
            if not _CONCAT_HINT_RE.search(masked):
                continue
            executor = _EXECUTE_CALL_RE.search(masked)
            tail = (
                f"拼接结果传入 `{executor.group(1)}(` 执行" if executor else "拼接后的 SQL 交由执行点执行"
            )
            hits.append(
                self.make_hit(
                    ctx,
                    idx + 1,
                    idx + 1,
                    f"第 {idx + 1} 行的 SQL 语句通过字符串拼接（+）引入变量，{tail}："
                    "外部输入可直接改变语句语义，构成 SQL 注入漏洞，可能导致数据泄露或被整库删除；"
                    "请改为参数化查询（PreparedStatement 占位符 ? + 参数绑定）。",
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


class JavaHardcodedSecretRule(_JavaRule, HardcodedSecretRuleFamily):
    """硬编码密钥/口令（复用 python 同款熵闸门口径，R1-9/R4-1/R4-7 判定一致）。

    标识符按 _/驼峰分词并做单数归一后精确命中敏感词，值满足随机性校验
    （ASCII、长度足够、Shannon 熵 ≥3.5 或字符集 ≥3 类；password 家族放低为
    熵 ≥3.0 或字符集 ≥2 类）；或值为 sk- 前缀的 API key 形态。

    W30 注释行掩码预检：raw 行命中声明形态而 masked 行不再命中（赋值形态只在
    于注释/字符串内容，如块注释中间行、文本块中间行的假声明）时跳过不报；
    masked 行保留引号定界符位置，真实赋值行的 `NAME = "` 形态不受影响。
    """

    id = "JAVA-HARDCODED-SECRET"

    # java 声明形态：[修饰符]* [类型] NAME = "..."（类型段可选以兼容宽松赋值形态）
    _ASSIGN_RE = re.compile(
        r"^\s*(?:(?:public|private|protected|static|final|transient|volatile)\s+)*"
        r"(?:[\w$.]+(?:<[^=]*>)?(?:\[\])?\s+)?"
        r"(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?P<q>['\"])"
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
            # python 同款转义语义（escape_mode="none"：反斜杠不转义跳转）
            value = string_value(raw, q_col, escape_mode="none")
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

        掩码扫描器把字符串内容与注释置为空格但保留引号定界符位置：真实赋值行
        `NAME = "` 形态在 masked 行依旧成立（预检放行、不误杀）；仅当该形态只
        存在于注释/字符串内容（块注释中间行、文本块中间行的假声明等）时
        masked 行才会失配，按注释行跳过不报。
        """
        return self._ASSIGN_RE.match(raw) is not None and self._ASSIGN_RE.match(masked) is None


# ---------------------------------------------------------------- style 规则


class JavaLongFunctionRule(_JavaRule, LongFunctionRuleFamily):
    """方法超过 80 行。"""

    id = "JAVA-LONG-FUNCTION"

    # W15-D 家族钩子：消息尾注与 java 掩码扫描器、大括号配对口径的作用域推断
    _TAIL_DETAIL = "分支爆炸"

    _get_scan = staticmethod(get_scan)
    _function_ranges = staticmethod(function_ranges_java)


# ---------------------------------------------------------------- 注册


def build_java_rules() -> list[Rule]:
    """构建全部内置 Java 规则实例（顺序即默认报告顺序）。"""
    return [
        # security
        JavaSqlInjectionRule(),
        JavaHardcodedSecretRule(),
        # style
        JavaLongFunctionRule(),
    ]
