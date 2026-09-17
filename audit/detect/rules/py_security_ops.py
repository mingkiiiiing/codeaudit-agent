"""Python 静态规则库·运维安全双形态（W20-C 清偿深度审计缺口，2 条 security）。

与 python.py/python_ext.py/py_dynamic_exec.py 同一实现思路：基于 `_python_common`
的逐行掩码扫描（不依赖 tree-sitter），保证行号精确落在真实代码行上；`check()`
纯函数式，不修改 ctx、无 IO。独立成文件，不触碰 python.py（W20 并行窗口所有权
隔离）；注册接线由集成人统一完成，本文件只提供构建函数。

清偿的两类运维安全缺口：
- PY-DEFAULT-CREDENTIAL（high）：出厂默认/弱口令硬编码——既有 PY-HARDCODED-SECRET
  只认「高熵/已知前缀密钥」（随机性闸门），低熵出厂口令（admin/123456/root 等）
  全部漏报；本条以「凭据语义命名 + 内置默认口令字典整词比对」互补覆盖。
  两规则同一行可并存（如 DATABASE_PASSWORD = "S3cret-...2026" 归前者，
  password = "admin" 归本条），不同规则 id 的同行命中由 engine 去重逻辑处理。
- PY-LOG-FORGERY（low）：日志伪造/日志注入（log forging，CVE 型）——日志格式
  字符串含换行（\\n 转义或多行三引号字面量）且同一调用拼接了非常量变量时，
  攻击者可用外部输入注入伪造日志行，污染审计链/绕过告警。

口径说明（误报红线：clean 语料 0 命中）：
- 默认凭据只认「赋值 / 字典键值 / kwargs / ==/!= 比较」四种绑定形态下、值经
  引号剥离与空白归一后**整词**命中字典（全部小写比对）的字面量；强随机密钥
  （不在字典）不报——那是 PY-HARDCODED-SECRET 的领地，两规则不重叠。
- 占位符豁免：值形态为 `${...}` / `<...>` 的模板占位不算口令；`changeme`
  既在字典又是常见占位——取舍是：仅当绑定名同时含 placeholder/default/demo
  等**占位指令语义**时豁免（如 DEFAULT_PASSWORD = "changeme" 是占位模板），
  否则照报（password = "changeme" 是经典的出厂默认口令，CWE-798）。
- 环境变量引用豁免：值来自 os.environ / os.getenv 等引用形态时无字面量绑定
  （掩码行上绑定位置不邻接字面量），天然不命中；回退默认值
  `os.environ.get("PW", "admin")` 中的 "admin" 因绑定位置被调用表达式隔断，
  同样不命中——保守性取舍：该形态应归配置审查，不归静态默认凭据。
- 单行口径（已知局限）：绑定名与值字面量必须同处一行（跨行续行的赋值漏报）。
- 日志伪造保守双条件：`\\n` 仅认字符串字面量内的换行转义（raw 字符串 r"\\n"
  是字面两字符，不报）或多行三引号字面量，且必须同时存在非常量变量参数
  （全大写常量/True/False/None 不算，kwargs 形参名 sep=/end=/file= 等先剥离
  ——`print("a\\nb", sep=", ")` 纯静态输出不报）；纯静态字符串日志不报。
- 结构化日志场景（json.dumps 拼接 \\n 分隔等）与 print 的 file= 回退形参可能
  误报，属已知限制，请按需 `codeaudit: ignore`。
"""

from __future__ import annotations

import re
from typing import Pattern

from audit.detect.base import Rule, RuleContext, RuleHit
from audit.detect.rules._python_common import call_span, find_call, get_scan
from audit.models import Category, Severity

__all__ = [
    "DefaultCredentialRule",
    "LogForgeryRule",
    "build_security_ops_rules",
]


class _PySecurityOpsRule(Rule):
    """运维安全规则公共基类：声明语言。"""

    languages = ("python",)


# ==================================================================== 规则 1


# 绑定名（变量/字典键/kwargs 形参）的凭据语义：按标识符分词边界比对——
# 下划线/非字母数字/驼峰边界切分后整词命中凭据词（DB_PASS→pass、dbPass→pass、
# cfg["access_key"]→access_key；bypass/passport 等含 pass 片段的普通词不命中）
_CAMEL_SPLIT_RE: Pattern[str] = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NAME_NORM_RE: Pattern[str] = re.compile(r"[^0-9A-Za-z]+")
_CRED_TOKEN_RE: Pattern[str] = re.compile(
    r"(?i)(?:^|_)(?:password|passwd|passw|pass|pwd|secret|token|api_?key|access(?:_key|_token)?)(?:$|_)"
)


def _cred_name_hit(name: str) -> bool:
    """绑定名分词归一后是否含凭据语义词（见 _CRED_TOKEN_RE 口径）。"""
    return bool(_CRED_TOKEN_RE.search(_NAME_NORM_RE.sub("_", _CAMEL_SPLIT_RE.sub("_", name))))

# 知名默认/弱口令字典（CWE-798）：全部小写、每个 ≤12 字符、整词比对
_DEFAULT_CREDENTIALS: frozenset[str] = frozenset(
    {
        "admin", "password", "123456", "12345678", "123456789", "root", "toor",
        "guest", "test", "changeme", "letmein", "qwerty", "monkey", "dragon",
        "master", "abc123", "password1", "admin123", "default", "welcome",
        "sa", "oracle", "postgres", "mysql", "redis", "elastic", "logstash",
        "kibana", "jenkins", "grafana", "prometheus", "sonar",
    }
)

# 占位符值形态：模板变量 ${...} / 尖括号 <...>（部署时替换，非真实口令）
_PLACEHOLDER_VALUE_RE: Pattern[str] = re.compile(r"^(\$\{.*\}|<.*>)$")

# 占位指令命名语义：仅对字典内兼作占位的 changeme 生效（见模块 docstring 取舍）
_PLACEHOLDER_NAME_RE: Pattern[str] = re.compile(
    r"(?i)(placeholder|default|demo|sample|template|dummy|example)"
)

# 绑定形态识别（掩码行上、值字面量开引号之前的文本，已 rstrip）：
# 尾随目标名（变量/属性/下标），可选携带 `: 类型注解`；
# 字典键为字符串字面量时掩码已置空，回原文按引号位置取键名。
_TRAILING_NAME_RE: Pattern[str] = re.compile(
    r"([A-Za-z_][\w.]*(?:\[[^\]]*\])?)\s*(?::[^=]*)?$"
)
_DICT_KEY_RE: Pattern[str] = re.compile(r"(['\"])([^'\"]*)\1\s*:\s*$")
# 单个 = 赋值判定：前一字符不属于这些（排除 ==/!=/<=/>=/+= 等）
_ASSIGN_BAD_PREV = "=!<+-*/%&|^:@"


def _normalize_value(raw: str) -> str:
    """值归一：剥前后空白与嵌套引号（如 "'admin'" → admin），供整词比对。"""
    v = raw.strip()
    while len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        v = v[1:-1].strip()
    return v


def _literal_span_exists(scan, lineno: int, a: int, b: int) -> bool:
    """(lineno, a, b) 是否为真实字符串字面量内容跨度（scan.strings 逐项核对）。

    防守点：字典键名回原文提取时，确认该键确实是独立字面量——整段大字符串
    内容里的假代码（如 doc = "{'password': 'admin'}"）内层引号不构成独立跨度，
    不会误判为字典键。
    """
    return any(item[0] == lineno and item[1] == a and item[2] == b for item in scan.strings)


class DefaultCredentialRule(_PySecurityOpsRule):
    """凭据语义命名 + 出厂默认/弱口令字典整词命中：默认凭据硬编码（CWE-798）。"""

    id = "PY-DEFAULT-CREDENTIAL"
    category = Category.SECURITY
    severity = Severity.HIGH
    description = (
        "凭据形态变量（password/token/api_key 等）被赋以知名出厂默认/弱口令"
        "（admin/123456/root 等，内置字典整词比对）：攻击者可用公开默认凭据直接登录；"
        "与 PY-HARDCODED-SECRET（高熵密钥）互补，专抓低熵默认口令。"
    )

    bad_example = (
        'DB_PASSWORD = "admin"          # 出厂默认口令，公开可查\n'
        'AUTH = {"api_key": "123456"}  # 弱口令\n'
        'if input_password == "root":\n'
        "    grant_admin()\n"
    )
    good_example = (
        "import os\n"
        "\n"
        'DB_PASSWORD = os.environ["DB_PASSWORD"]  # 首次启动强制改密\n'
        'AUTH = {"api_key": os.environ["API_KEY"]}\n'
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        strings_by_line: dict[int, list[tuple[int, int, int, str]]] = {}
        for item in scan.strings:
            strings_by_line.setdefault(item[0], []).append(item)
        for idx, masked_ln in enumerate(scan.masked):
            orig = ctx.lines[idx]
            lineno = idx + 1
            for _sl, a, b, _prefix in strings_by_line.get(lineno, ()):
                value = _normalize_value(orig[a:b])
                if not value:
                    continue  # 空值豁免
                lowered = value.lower()
                if lowered not in _DEFAULT_CREDENTIALS:
                    continue  # 整词比对：强随机/普通值不在字典即不涉本规则
                if _PLACEHOLDER_VALUE_RE.match(value):
                    continue  # ${...}/<...> 模板占位豁免
                name = self._binding_name(scan, lineno, masked_ln, orig, a)
                if name is None or not _cred_name_hit(name):
                    continue
                if lowered == "changeme" and _PLACEHOLDER_NAME_RE.search(name):
                    continue  # changeme 兼作占位：绑定名亦含占位语义时豁免
                hits.append(
                    self.make_hit(
                        ctx,
                        lineno,
                        lineno,
                        f"第 {lineno} 行将出厂默认/弱口令硬编码给凭据形态变量 `{name}`"
                        "（值命中默认口令字典）：使用出厂默认/弱口令，攻击者可用公开默认凭据直接登录；"
                        "建议首次启动强制改密，并将凭据收敛到环境变量/密钥管理服务。",
                        meta={"var": name, "form": "default-credential"},
                    )
                )
        return hits

    @staticmethod
    def _binding_name(
        scan, lineno: int, masked_ln: str, orig: str, value_content_col: int
    ) -> str | None:
        """在掩码行上定位值字面量的绑定名（赋值/字典键/kwargs/比较四种形态）。

        value_content_col 是值内容起始列（开引号在其前一列）；掩码行保证字符串
        内容与注释不干扰结构判定，字典键名与下标名再回原文补取（掩码已置空）。
        """
        if value_content_col < 1:
            return None
        prefix = masked_ln[: value_content_col - 1].rstrip()
        if not prefix:
            return None
        # 1) 比较：== / !=（先于单个 = 判定，避免把 == 拆成赋值）
        if prefix.endswith(("==", "!=")):
            return DefaultCredentialRule._trailing_name(masked_ln, orig, value_content_col, len(prefix) - 2)
        # 2) walrus :=（if (pwd := "admin") 形态；必须先于单个 = 判定）
        if prefix.endswith(":="):
            return DefaultCredentialRule._trailing_name(masked_ln, orig, value_content_col, len(prefix) - 2)
        # 3) 赋值 / kwargs：单个 =（前一字符排除 ==/!=/<=/>=/+= 等复合形态）
        if prefix.endswith("="):
            prev = prefix[-2] if len(prefix) >= 2 else ""
            if prev in _ASSIGN_BAD_PREV:
                return None
            return DefaultCredentialRule._trailing_name(masked_ln, orig, value_content_col, len(prefix) - 1)
        # 4) 字典键值：字符串字面量键回原文取键名，并核对键确为独立字面量跨度
        #   （切片 s[x:"admin"] 等裸冒号形态无键名语义，一律不报）
        if prefix.endswith(":"):
            m = _DICT_KEY_RE.search(orig[: value_content_col - 1].rstrip())
            if m:
                key_a, key_b = m.start(1) + 1, m.end(2)
                if _literal_span_exists(scan, lineno, key_a, key_b):
                    return m.group(2)
            return None
        return None

    @staticmethod
    def _trailing_name(masked_ln: str, orig: str, value_content_col: int, cut: int) -> str | None:
        """取 cut 之前的尾随目标名；下标形态回原文补取（掩码把下标串置空了）。

        掩码与原文逐字符等长，cut 对两串同位点生效。
        """
        m = _TRAILING_NAME_RE.search(masked_ln[:cut].rstrip())
        if not m:
            return None
        name = m.group(1)
        if "[" in name:
            m2 = _TRAILING_NAME_RE.search(orig[:cut].rstrip())
            if m2:
                name = m2.group(1)
        return name


# ==================================================================== 规则 2


# 日志调用形态（find_call 口径：模式以 \s*\( 收尾）：
# logging/logger/log.<级别>( 与 print(（print 走控制台输出，同口径标疑）
_LOG_CALL_RES: tuple[tuple[Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(?:logging|logger|log)\s*\.\s*"
            r"(?:debug|info|warning|warn|error|exception|critical|log)\s*\("
        ),
        "logging",
    ),
    (re.compile(r"(?<![\w.])print\s*\("), "print"),
)
# 非常量判定：关键字与全大写常量（含下划线/数字）不算"非常量变量"
_PY_KEYWORDS = frozenset({"True", "False", "None", "and", "or", "not", "in", "is"})
_CONST_NAME_RE: Pattern[str] = re.compile(r"^[A-Z][A-Z0-9_]*$")
_IDENT_RE: Pattern[str] = re.compile(r"[A-Za-z_]\w*")
_KWARG_NAME_RE: Pattern[str] = re.compile(r"[A-Za-z_]\w*\s*=(?!=)")  # 形参名= 先剥离
_STD_KWARG_RE: Pattern[str] = re.compile(r"file\s*=\s*sys\.(?:stderr|stdout|stdin)\b")  # print 回退目标非注入载体
_FSTRING_FIELD_RE: Pattern[str] = re.compile(r"(?<!\{)\{([^{}]*)\}(?!\})")  # {{字面}} 转义不算插值
_NEWLINE_ESCAPE = "\\n"  # 原文中的反斜杠+n 两字符（源码写作 \n）


class LogForgeryRule(_PySecurityOpsRule):
    """日志格式串含换行（\\n 转义/多行字面量）且拼接非常量变量：日志注入（log forging）。"""

    id = "PY-LOG-FORGERY"
    category = Category.SECURITY
    severity = Severity.LOW
    description = (
        "logger/print 日志调用的格式字符串含换行转义或多行字面量，且同一调用"
        "拼接了非常量变量：外部输入可注入伪造日志行（log forging / CVE 型日志注入）；"
        "建议对用户输入做换行/控制字符清洗或改用结构化日志。"
    )

    bad_example = (
        'logger.info("user %s logged in\\ntrace: %s", user, trace_id)\n'
        '# user 输入 "x\\nERROR disk full" 即可伪造一条 ERROR 日志\n'
    )
    good_example = (
        'logger.info("user %s logged in trace=%s", sanitize(user), trace_id)\n'
        '# 或结构化日志：logger.info("login", extra={"user": user})\n'
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            for pat, api in _LOG_CALL_RES:
                for col in find_call(ln, pat):
                    span = call_span(scan.masked, idx + 1, col)
                    if span is None:
                        continue
                    end_line, end_col, _args = span
                    arg_spans = self._arg_strings(scan, idx + 1, col, end_line, end_col)
                    if not self._has_newline_literal(ctx, arg_spans):
                        continue
                    if not self._has_variable(ctx, scan, idx + 1, col, end_line, end_col, arg_spans):
                        continue  # 纯静态字符串日志（无变量拼接）保守不报
                    hits.append(
                        self.make_hit(
                            ctx,
                            idx + 1,
                            end_line,
                            f"第 {idx + 1} 行 `{api}` 日志调用的格式字符串含换行（\\n 转义或多行字面量）"
                            "且拼接了非常量变量：日志条目可被外部输入注入伪造换行"
                            "（log forging / CVE 型日志注入），污染审计链或绕过告警；"
                            "建议对用户输入做换行/控制字符清洗或改用结构化日志。",
                            meta={"api": api},
                        )
                    )
        return hits

    @staticmethod
    def _arg_strings(scan, start_line: int, open_col: int, end_line: int, end_col: int) -> list:
        """落在调用参数区内的字符串字面量跨度（排除调用名之前/收尾括号之后）。"""
        out = []
        for item in scan.strings:
            lineno, a, b = item[0], item[1], item[2]
            if lineno < start_line or lineno > end_line:
                continue
            if lineno == start_line and a - 1 <= open_col:
                continue
            if lineno == end_line and b >= end_col:
                continue
            out.append(item)
        return out

    @staticmethod
    def _has_newline_literal(ctx: RuleContext, arg_spans: list) -> bool:
        """参数区是否存在"含换行"的字面量：\\n 转义（raw 字符串除外）或多行三引号。"""
        for lineno, a, b, prefix in arg_spans:
            content = ctx.lines[lineno - 1][a:b]
            if "r" not in prefix.lower() and _NEWLINE_ESCAPE in content:
                return True  # 源码里的 \n 转义（原文为反斜杠+n 两字符；raw 串是字面量不报）
            line = ctx.lines[lineno - 1]
            opener = line[max(0, a - 3) : a]
            if opener.endswith(('"""', "'''")) and b >= len(line):
                return True  # 三引号开引号且内容到行尾未闭合：多行字面量必含真实换行
        return False

    @staticmethod
    def _has_variable(
        ctx: RuleContext, scan, start_line: int, open_col: int, end_line: int, end_col: int,
        arg_spans: list,
    ) -> bool:
        """参数区是否存在非常量变量：掩码后残留标识符（f-string 插值回原文查）。

        kwargs 形参名（sep=/end=/file= 等）先剥离——它们不是注入数据的载体；
        全大写常量与 True/False/None 亦不算变量。
        """
        lines = []
        for lineno in range(start_line, end_line + 1):
            ln = scan.masked[lineno - 1]
            if lineno == start_line:
                ln = ln[open_col + 1 :]
            if lineno == end_line:
                ln = ln[:end_col]
            lines.append(ln)
        # 先剥 file=sys.stderr/stdout 整体（形参名剥离会破坏该模式的匹配），再剥形参名=
        region = _KWARG_NAME_RE.sub(" ", _STD_KWARG_RE.sub(" ", " ".join(lines)))
        for name in _IDENT_RE.findall(region):
            if name in _PY_KEYWORDS or _CONST_NAME_RE.match(name):
                continue
            return True
        for lineno, a, b, prefix in arg_spans:
            if "f" not in prefix.lower():
                continue
            content = ctx.lines[lineno - 1][a:b]
            for field in _FSTRING_FIELD_RE.findall(content):
                for name in _IDENT_RE.findall(field):
                    if name in _PY_KEYWORDS or _CONST_NAME_RE.match(name):
                        continue
                    return True
        return False


# ==================================================================== 注册


def build_security_ops_rules() -> list[Rule]:
    """构建运维安全双形态规则实例（顺序即默认报告顺序）。

    注册接线由集成人统一完成（W20 并行窗口约定），本文件只提供构建函数。
    """
    return [
        DefaultCredentialRule(),
        LogForgeryRule(),
    ]
