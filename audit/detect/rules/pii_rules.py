"""Python 静态规则库·个人信息（PII）入日志/明文入库（W16 静态规则补缺，2 条 security/medium）。

与 python.py/python_ext.py 同一实现思路：基于 `_python_common` 的逐行掩码扫描
（不依赖 tree-sitter），保证行号精确落在真实代码行上；`check()` 纯函数式，
不修改 ctx、无 IO。

口径说明（W16 验收缺口 ②：敏感个人信息入日志 0%）：
- 单行口径（已知局限）：PII 变量名与 logger/print 调用必须落在同一行；
  跨行拼接（先把 PII 字段攒进变量、隔行再打日志）不做数据流追踪，属已知漏报。
- PII 变量名在「原始行」上匹配（f-string/% 格式串里的字段名也是命中证据），
  但排除注释区（行内 # 之后的文本不作为证据）；logger/print 调用本身在
  「掩码行」上匹配，天然跳过字符串/注释中的假调用。
- 常量形态（18 位身份证 / 11 位手机号）只认字符串字面量内的取值（掩码扫描
  的 strings 跨度），print 与 logger 均算写日志上下文（print 的残留性由
  PY-PRINT-DEBUG 负责，本条只看 PII 语义，两角度并存不冲突，去重由 engine 处理）。

PY-PII-SQL（PII 明文入库）口径：
- 同为单行口径（已知局限）：`.execute(` / `.executemany(` / `.executescript(`
  调用与 SQL 文本/绑定参数必须落在同一行，跨行拼 SQL 不做追踪，属已知漏报。
- 口径 a）：先粗判行内含写库语句关键词（INSERT INTO/UPDATE/CREATE TABLE），
  再查 SQL 字符串字面量中是否出现 PII 列名（\\b 词边界口径，phones/emails 等
  复数一并覆盖）；口径 b）：绑定参数区（调用内第一个顶层逗号到调用收尾括号
  之间的原文，保留下标取参字符串）含 PII 变量名（复用下方 _PII_NAME_RE）。
- 本规则关注个人信息最小化而非注入：参数化绑定（占位符）本身防注入，但 PII
  变量明文绑定入库仍报；SELECT 读取 PII 列（非写库语句且参数非 PII）不报。
"""

from __future__ import annotations

import re
from typing import Pattern

from audit.detect.base import Rule, RuleContext, RuleHit
from audit.detect.rules._python_common import get_scan
from audit.models import Category, Severity

__all__ = ["build_pii_rules"]


class _PiiRule(Rule):
    """Python PII 规则公共基类：声明语言。"""

    languages = ("python",)


# PII 变量名（大小写不敏感）：前后沿用「非字母数字」作软边界——下划线视为
# 分隔符（user_tel 可命中），而 hotel/intel 这类包含 tel 的英文词不命中。
_PII_NAME_RE: Pattern[str] = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(?:email_addr|social_credit|shenfenzheng|id_card|identity|idcard"
    r"|phone|mobile|shouji|ssn|tel)"
    r"(?![A-Za-z0-9])"
)

# 写日志上下文：logger.xxx(...) / log.xxx(...) / logging.xxx(...) /
# logging.getLogger(...).xxx(...)（在掩码行上匹配，规避字符串/注释假调用）
_LOGGER_CALL_RE: Pattern[str] = re.compile(
    r"(?<![\w.])(?:logging\s*\.\s*getLogger\s*\([^()]*\)|logger|log|logging)"
    r"\s*\.\s*(?:debug|info|warning|warn|error|exception|critical|log)\s*\("
)
_PRINT_CALL_RE: Pattern[str] = re.compile(r"(?<![\w.])print\s*\(")

# 常量形态：18 位身份证（末位可为 X/x）、11 位手机号；身份证加前后沿界定，
# 避免更长数字串（订单号/时间戳）被截取 18 位误报。
_IDCARD_LIT_RE: Pattern[str] = re.compile(r"(?<![0-9Xx])[0-9]{17}[0-9Xx](?![0-9Xx])")
_MOBILE_LIT_RE: Pattern[str] = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


class PiiLogRule(_PiiRule):
    """个人敏感信息（PII）写入日志且未脱敏。"""

    id = "PY-PII-LOG"
    category = Category.SECURITY
    severity = Severity.MEDIUM
    description = (
        "疑似将个人敏感信息（手机号/身份证号等）写入日志且未脱敏：依据个人信息"
        "保护相关要求（如《个人信息保护法》的最小必要原则），日志中的个人信息应"
        "脱敏或最小化，明文落盘会随日志采集/归档扩散泄露面，且难以事后回收。"
    )

    bad_example = (
        "import logging\n"
        "\n"
        "logger = logging.getLogger(__name__)\n"
        "\n"
        "def log_login(user):\n"
        "    logger.info(\"login phone=%s idcard=%s\", user[\"phone\"], user[\"idcard\"])\n"
    )
    good_example = (
        "import logging\n"
        "\n"
        "logger = logging.getLogger(__name__)\n"
        "\n"
        "def log_login(user):\n"
        "    logger.info(\"login phone=%s\", mask_tail(user[\"phone\"]))  # 仅保留脱敏后的尾 4 位\n"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        # 字符串字面量内容跨度（行号 -> [(起列, 止列), ...]），供常量形态取证
        spans_by_line: dict[int, list[tuple[int, int]]] = {}
        for lineno, a, b, _prefix in scan.strings:
            spans_by_line.setdefault(lineno, []).append((a, b))
        hits: list[RuleHit] = []
        for idx, raw in enumerate(ctx.lines):
            lineno = idx + 1
            masked = scan.masked[idx]
            is_log_ctx = bool(_LOGGER_CALL_RE.search(masked) or _PRINT_CALL_RE.search(masked))
            if not is_log_ctx:
                continue
            # 形态一：PII 变量名与 logger/print 同行（f-string/%/.format 拼进
            # 日志的同条件；注释区内的 PII 词不作证据）。同一行至多报一条，
            # 形态一优先于形态二。
            comment_col = scan.comment_start.get(lineno)
            names = sorted(
                {
                    m.group(0).lower()
                    for m in _PII_NAME_RE.finditer(raw[:comment_col] if comment_col is not None else raw)
                }
            )
            if names:
                hits.append(
                    self.make_hit(
                        ctx,
                        lineno,
                        lineno,
                        f"第 {lineno} 行在日志输出中引用了疑似个人敏感信息字段（{', '.join(names)}，"
                        "如手机号/身份证号等）且未见脱敏：日志通常被长期留存并随采集链路扩散，"
                        "明文 PII 落盘违反个人信息最小化原则，泄露后难以回收；"
                        "请移除该字段，或先做掩码/截断（如手机号仅保留后 4 位）再输出。",
                        meta={"pii_names": ",".join(names), "kind": "pii-var"},
                    )
                )
                continue
            # 形态二：字符串字面量中内嵌 18 位身份证 / 11 位手机号常量
            for a, b in spans_by_line.get(lineno, []):
                content = raw[a:b]
                kind = ""
                if _IDCARD_LIT_RE.search(content):
                    kind = "idcard-literal"
                elif _MOBILE_LIT_RE.search(content):
                    kind = "mobile-literal"
                if kind:
                    hits.append(
                        self.make_hit(
                            ctx,
                            lineno,
                            lineno,
                            f"第 {lineno} 行把疑似 18 位身份证号/11 位手机号的字面量写入日志："
                            "日志中的个人敏感信息应脱敏或最小化（个人信息保护要求），"
                            "明文常量会随日志留存扩散；请移除或打码后再输出。",
                            meta={"kind": kind},
                        )
                    )
                    break  # 每行报一条即可
        return hits


# ---------------------------------------------------------------- SQL 明文入库（PY-PII-SQL）

# 执行 SQL 的方法调用（在掩码行上匹配，规避字符串/注释假调用）：
# cur.execute(...) / conn.executemany(...) / db.executescript(...)
_SQL_EXEC_CALL_RE: Pattern[str] = re.compile(
    r"\.\s*(?:executemany|executescript|execute)\s*\("
)

# 写库语句粗判（不区分大小写）：INSERT INTO / UPDATE / CREATE TABLE
_SQL_WRITE_STMT_RE: Pattern[str] = re.compile(
    r"(?i)\b(?:insert\s+into|update|create\s+table)\b"
)

# PII 列名（\b 词边界口径：下划线不构成边界，user_phone 列不由本口径命中，
# 由变量名口径兜底；结尾 s? 覆盖 phones/emails 等复数形式）
_PII_COLUMN_RE: Pattern[str] = re.compile(
    r"(?i)\b(?:phone|mobile|tel|idcard|id_card|identity|ssn|email|shouji|shenfenzheng)s?\b"
)


def _binding_param_span(masked: str, search_from: int) -> tuple[int, int] | None:
    """在掩码行上定位 execute 调用的绑定参数区跨度（列区间，左闭右开）。

    search_from 为调用 '(' 之后的位置（括号深度按 1 起算）；SQL 字符串内容在
    掩码行上已被置换为空格，串内逗号/括号不会干扰首个顶层逗号的判定。区间为
    「第一个顶层逗号 -> 调用收尾括号」；单参调用（如 executescript）无逗号，
    返回 None。
    """
    depth = 1
    comma = -1
    for i in range(search_from, len(masked)):
        ch = masked[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return (comma, i) if comma >= 0 else None
        elif ch == "," and depth == 1 and comma < 0:
            comma = i
    return None


class PiiSqlRule(_PiiRule):
    """个人敏感信息（PII）以明文写入数据库。"""

    id = "PY-PII-SQL"
    category = Category.SECURITY
    severity = Severity.MEDIUM
    description = (
        "疑似将个人敏感信息（手机号/身份证号/邮箱等）以明文写入数据库：SQL 写库"
        "语句（INSERT INTO/UPDATE/CREATE TABLE）中出现 PII 列名，或执行 SQL 时"
        "绑定了 PII 变量。依据个人信息保护相关要求（如《个人信息保护法》的最小"
        "必要原则），敏感个人信息的存储应加密/脱敏/最小化，明文入库会随数据库、"
        "备份与导出链路长期留存并扩大泄露面。注意：本规则关注数据最小化而非 "
        "SQL 注入——参数化绑定（占位符）本身能防注入，但 PII 变量明文绑定入库"
        "仍会命中；非敏感字段的参数化查询不报。建议：对敏感字段做字段级加密、"
        "单向哈希（需检索时用 HMAC/盲索引），或仅收集与存储业务必需的最小字段。"
    )

    bad_example = (
        "import sqlite3\n"
        "\n"
        "\n"
        "def save_user(conn, user):\n"
        "    conn.execute(\"INSERT INTO users(phone) VALUES (?)\", (user[\"phone\"],))\n"
    )
    good_example = (
        "import sqlite3\n"
        "\n"
        "\n"
        "def save_user(conn, user):\n"
        "    # 敏感字段先脱敏/加密再入库：手机号仅存掩码（保留尾 4 位），检索走哈希盲索引\n"
        "    conn.execute(\"INSERT INTO users(contact_masked) VALUES (?)\", (mask_tail(user[\"contact\"]),))\n"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        # 字符串字面量内容跨度（行号 -> [(起列, 止列), ...]），供 SQL 文本取证
        spans_by_line: dict[int, list[tuple[int, int]]] = {}
        for lineno, a, b, _prefix in scan.strings:
            spans_by_line.setdefault(lineno, []).append((a, b))
        hits: list[RuleHit] = []
        for idx, raw in enumerate(ctx.lines):
            lineno = idx + 1
            masked = scan.masked[idx]
            exec_m = _SQL_EXEC_CALL_RE.search(masked)
            if exec_m is None:
                continue
            comment_col = scan.comment_start.get(lineno)
            code_raw = raw[:comment_col] if comment_col is not None else raw
            # 形态一：写库语句（先粗判行内 INSERT INTO/UPDATE/CREATE TABLE 关键词）
            # 且 SQL 字符串字面量中出现 PII 列名。同一行至多报一条，形态一优先。
            hit_column: RuleHit | None = None
            if _SQL_WRITE_STMT_RE.search(code_raw):
                for a, b in spans_by_line.get(lineno, []):
                    cols = sorted(
                        {m.group(0).lower() for m in _PII_COLUMN_RE.finditer(raw[a:b])}
                    )
                    if cols:
                        hit_column = self.make_hit(
                            ctx,
                            lineno,
                            lineno,
                            f"第 {lineno} 行的 SQL 写库语句（INSERT/UPDATE/CREATE TABLE）"
                            f"包含疑似个人敏感信息字段（{', '.join(cols)}）：依据个人信息"
                            "保护要求（最小必要原则），敏感个人信息存储应加密/脱敏/最小化，"
                            "明文入库会随数据库与备份长期留存、扩大泄露面；建议对字段做"
                            "字段级加密或哈希（检索走盲索引），或仅存储业务必需的最小字段。",
                            meta={"pii_names": ",".join(cols), "kind": "pii-column"},
                        )
                        break
            if hit_column is not None:
                hits.append(hit_column)
                continue
            # 形态二：绑定参数区含 PII 变量名（取第一个顶层逗号之后的原文，
            # 保留下标取参字符串里的字段名证据；掩码行只用于定位，不用于取证）
            span = _binding_param_span(masked, exec_m.end())
            if span is None:
                continue
            names = sorted(
                {
                    m.group(0).lower()
                    for m in _PII_NAME_RE.finditer(raw[span[0] + 1 : span[1]])
                }
            )
            if names:
                hits.append(
                    self.make_hit(
                        ctx,
                        lineno,
                        lineno,
                        f"第 {lineno} 行执行 SQL 时绑定了疑似个人敏感信息变量"
                        f"（{', '.join(names)}）：参数化绑定只防注入、不改变明文入库"
                        "事实（本规则关注个人信息最小化而非注入），敏感个人信息明文"
                        "落库违反最小化原则；建议先做字段级加密/哈希/脱敏，或仅存储"
                        "业务必需的最小字段。",
                        meta={"pii_names": ",".join(names), "kind": "pii-param"},
                    )
                )
        return hits


# ---------------------------------------------------------------- 注册


def build_pii_rules() -> list[Rule]:
    """构建 PII 入日志/明文入库规则实例（顺序即默认报告顺序）。"""
    return [
        PiiLogRule(),
        PiiSqlRule(),
    ]
