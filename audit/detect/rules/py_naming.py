"""Python 静态规则库·命名规范扩充（W16 静态规则补缺，2 条 style/low）。

与 python.py/python_ext.py 同一实现思路：基于 `_python_common` 的逐行掩码扫描
（不依赖 tree-sitter），保证行号精确落在真实代码行上；所有规则 `check()` 纯函数式，
不修改 ctx、无 IO。

口径说明（W16 验收缺口 ①：命名规范 0%）：
- 函数名 snake_case / 类名 PascalCase 是确定性判定（PEP 8）；变量命名误报高，
  不在本规则范围（拼音命名单独走 PY-PINYIN-NAMING 的保守启发式）。
- 拼音判定刻意收窄：分词后整词恰为 ≥2 个内置音节组合、且不在英文技术词白名单，
  才命中——单音节词（jia/ma/shi…）与英文词（join/data/total…）一律不报。
- 测试文件（tests/、test_*.py 等）对两条规则均豁免（与 is_test_file 口径一致）。
"""

from __future__ import annotations

import re
from typing import Pattern

from audit.detect.base import Rule, RuleContext, RuleHit
from audit.detect.rules._python_common import get_scan, is_test_file
from audit.models import Category, Severity

__all__ = ["build_py_naming_rules"]


class _PyNamingRule(Rule):
    """Python 命名规则公共基类：声明语言。"""

    languages = ("python",)


class PyNamingStyleRule(_PyNamingRule):
    """函数名 snake_case / 类名 PascalCase（PEP 8 确定性口径）。"""

    id = "PY-NAMING-STYLE"
    category = Category.STYLE
    severity = Severity.LOW
    description = (
        "命名不符合 PEP 8：函数/方法名应全小写下划线分词（snake_case），"
        "类名应以大写字母开头的驼峰式（PascalCase）；命名不一致会降低可读性"
        "并让「大小写敏感的外部调用」埋下隐蔽缺陷。"
    )

    bad_example = (
        "def GetUserOrder(OrderTotal):\n"
        "    return OrderTotal\n"
        "\n"
        "class order_service:\n"
        "    pass\n"
    )
    good_example = (
        "def get_user_order(order_total):\n"
        "    return order_total\n"
        "\n"
        "class OrderService:\n"
        "    pass\n"
    )

    # 函数名判定：def/async def 后的名字含大写字母即命中。连字符在 Python
    # 标识符中本就不合法（语法层面无法出现），故只做大小写检查。
    _DEF_RE: Pattern[str] = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")
    # 类名判定：class 后的名字首字母为小写字母即命中（_private 前导下划线不报，
    # 属保守口径：下划线开头的「类」多为模块内部约定，误报风险大于收益）。
    _CLASS_RE: Pattern[str] = re.compile(r"^\s*class\s+([A-Za-z_]\w*)")

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        if is_test_file(ctx.rel_path):
            return []
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            m = self._DEF_RE.match(ln)
            if m:
                name = m.group(1)
                # dunder（__xxx__）是解释器协议名，不参与 snake_case 判定
                if name.startswith("__") and name.endswith("__"):
                    continue
                if any(ch.isupper() for ch in name):
                    hits.append(
                        self.make_hit(
                            ctx,
                            idx + 1,
                            idx + 1,
                            f"第 {idx + 1} 行函数名 `{name}` 含大写字母，不符合 snake_case 惯例："
                            "PEP 8 要求函数/方法名全小写并用下划线分词，大小写混用会误导调用方"
                            "（Python 对大小写敏感，调用处极易写错）；请改为对应的小写下划线形式。",
                        )
                    )
                continue
            m = self._CLASS_RE.match(ln)
            if m:
                name = m.group(1)
                if name and name[0].islower():
                    hits.append(
                        self.make_hit(
                            ctx,
                            idx + 1,
                            idx + 1,
                            f"第 {idx + 1} 行类名 `{name}` 首字母小写，不符合 PascalCase 惯例："
                            "PEP 8 要求类名以大写字母开头的驼峰式命名，小写类名与函数/变量难以区分；"
                            "请改为 PascalCase。",
                        )
                    )
        return hits


# ---------------------------------------------------------------- 拼音命名（保守启发式）

# 内置常见拼音音节表（~60 个，去重）：覆盖中文代码库高频词根（结果/计算/数据/
# 用户/文件/时间/查询…）。刻意不收录会拼出常见英文词的音节（如 ta/to/pa/re），
# 从源头避免 data/meta/total/remote/load 等被拆成「拼音音节组合」造成误报。
_PINYIN_SYLLABLES = frozenset(
    [
        "biao",
        "cai", "cha", "ce", "chu",
        "da", "dan", "deng",
        "fa", "fang",
        "guo",
        "hu", "hui",
        "ji", "jia", "jian", "jiang", "jiao", "jie", "jin", "jing", "jiu", "ju", "jun",
        "lian", "lu",
        "ma", "mi", "ming",
        "nei",
        "qing", "qiu", "quan", "que",
        "rong",
        "shan", "shang", "sheng", "shi", "shou", "shu", "su", "suan",
        "ti", "tian", "tong", "tui",
        "wei", "wen",
        "xi", "xian", "xiao", "xie", "xin", "xing", "xun",
        "yong", "yun",
        "zhang", "zhao", "zhen", "zheng", "zhi", "zhong", "zhu", "zi",
    ]
)

# 常见英文技术词白名单：分词命中的整词在此表内则不报（防误报的第二道闸，
# 覆盖 clean 语料与通用业务词；首道闸是音节表本身不含 ta/to/pa/re 等英文原子）。
_ENGLISH_NAME_WHITELIST = frozenset(
    [
        "result", "results", "list", "data", "index", "count", "name", "names",
        "value", "values", "user", "users", "info", "config", "log", "logger",
        "logging", "temp", "str", "int", "float", "bool", "dict", "set", "tuple",
        "get", "put", "post", "delete", "update", "insert", "select",
        "order", "orders", "item", "items", "total", "sum", "avg", "min", "max",
        "known", "load", "save", "run", "sync", "proc", "fetch", "remote",
        "render", "report", "line", "lines", "path", "file", "read", "write",
        "open", "close", "key", "keys", "id", "ids", "type", "kind", "tag", "tags",
        "limit", "offset", "page", "size", "batch", "service", "endpoint",
        "url", "api", "http", "https", "json", "xml", "text", "num", "number",
        "len", "length", "buf", "cache", "store", "find", "search", "query",
        "filter", "map", "row", "rows", "col", "cols", "conn", "db", "sql",
        "table", "column", "active", "valid", "validate", "check", "parse",
        "format", "join", "print", "input", "output", "args", "kwargs",
        "params", "options", "opts", "ctx", "self", "cls", "func", "method",
        "class", "obj", "title", "label", "desc", "comment", "status", "code",
        "time", "date", "year", "month", "day", "amount", "price", "cost",
        "rate", "score", "level", "flag", "state", "mode", "role", "group",
        "msg", "error", "err", "warning", "debug", "test", "case", "app",
        "main", "init", "new", "payload", "repository", "endpoint",
    ]
)

# 标识符分词：先按下划线隐式分隔（下划线不被任何分支匹配），再按驼峰切分
# （API_KEY → api/key；jieGuo → jie/Guo）。
_TOKEN_SPLIT_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+")

_DEF_NAME_RE: Pattern[str] = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)")
_ASSIGN_NAME_RE: Pattern[str] = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?::[^=]+)?=(?!=)")
_FOR_NAME_RE: Pattern[str] = re.compile(r"^\s*for\s+([A-Za-z_]\w*)\s+in\b")


def _min_syllable_count(token: str) -> int:
    """整词能否恰好由内置音节拼接而成；返回最少音节数，拼不成返回 0（DP）。"""
    n = len(token)
    best = [n + 1] * (n + 1)
    best[0] = 0
    for i in range(1, n + 1):
        for j in range(i):
            if best[j] + 1 < best[i] and token[j:i] in _PINYIN_SYLLABLES:
                best[i] = best[j] + 1
    return best[n] if best[n] <= n else 0


def _pinyin_tokens(name: str) -> list[str]:
    """返回标识符分词后「恰为 ≥2 个拼音音节组合、且不在英文白名单」的词。"""
    out: list[str] = []
    for raw in _TOKEN_SPLIT_RE.findall(name):
        token = raw.lower()
        if token in _ENGLISH_NAME_WHITELIST:
            continue
        if _min_syllable_count(token) >= 2:
            out.append(token)
    return out


class PyPinyinNamingRule(_PyNamingRule):
    """拼音/歧义命名（保守启发式，独立 id：PY-PINYIN-NAMING）。

    口径：函数名/变量名按下划线+驼峰分词后，某整词恰为 ≥2 个内置拼音音节的
    组合、且不在英文技术词白名单 → 命中。单音节词（jia/ma/shi…）不报、
    英文词不报；变量命名其余维度（大小写等）不在本条范围（误报高，见
    PY-NAMING-STYLE 的口径说明）。
    """

    id = "PY-PINYIN-NAMING"
    category = Category.STYLE
    severity = Severity.LOW
    description = (
        "标识符疑似拼音命名（如 jieguo/jisuan/shuju）：中英混用的拼音命名会显著"
        "降低代码可读性与可检索性，团队协作时难以理解意图；建议统一改用英文命名。"
        "本条为保守启发式，仅对可完整拆分为 ≥2 个常见拼音音节且非英文词的标识符提示。"
    )

    bad_example = (
        "def jisuan_heji(shuju):\n"
        "    jieguo = sum(shuju)\n"
        "    return jieguo\n"
    )
    good_example = (
        "def calculate_total(values):\n"
        "    total = sum(values)\n"
        "    return total\n"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        if is_test_file(ctx.rel_path):
            return []
        scan = get_scan(ctx.lines, ctx.meta)
        hits: list[RuleHit] = []
        for idx, ln in enumerate(scan.masked):
            name: str | None = None
            m = _DEF_NAME_RE.match(ln)
            if m:
                name = m.group(1)
            else:
                m = _ASSIGN_NAME_RE.match(ln)
                if m:
                    name = m.group(1)
                else:
                    m = _FOR_NAME_RE.match(ln)
                    if m:
                        name = m.group(1)
            if not name:
                continue
            tokens = _pinyin_tokens(name)
            if tokens:
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        idx + 1,
                        f"第 {idx + 1} 行标识符 `{name}` 的分词 `{'/'.join(tokens)}` 疑似拼音命名："
                        "拼音与英文混用会降低代码可读性与可检索性，团队协作时难以理解意图；"
                        "建议改用统一的英文命名（如 jieguo→result、jisuan→calculate、shuju→data）。",
                        meta={"tokens": ",".join(tokens)},
                    )
                )
        return hits


# ---------------------------------------------------------------- 注册


def build_py_naming_rules() -> list[Rule]:
    """构建命名规范扩充全部 Python 规则实例（顺序即默认报告顺序）。"""
    return [
        PyNamingStyleRule(),
        PyPinyinNamingRule(),
    ]
