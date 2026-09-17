"""JS/TS 静态规则库·安全与命名扩充（W16 静态规则补缺，2 条）。

与 javascript.py/js_ext.py 同一实现思路：基于 `_js_common` 的逐字符掩码扫描 +
作用域启发式（不依赖 tree-sitter），保证行号精确落在真实代码行上；所有规则
`check()` 纯函数式，不修改 ctx、无 IO。

语言声明沿用既有约定：命令注入是语法在两种语言中完全一致的 security 类问题，
以 ("javascript", "typescript") 共享；命名风格类只作用于 javascript。

JS 命令注入口径（W16 验收缺口 ③）：
- 前置条件：文件中出现 child_process 导入（require('child_process') /
  import ... from 'child_process' / 'node:child_process' / export...from），
  且调用行不早于导入行；
- 命中条件：`.exec/.execSync/.spawn/.spawnSync` 的首个实参含拼接（`+` 连接、
  模板字符串含 `${` 插值）或为非常量变量标识符；纯字符串字面量（无插值）不报；
- 已知局限：`const { exec } = require('child_process')` 解构后的裸 `exec(...)`
  调用不在本条口径内（spec 仅要求点号调用形态）；正则字面量尾随的
  `/re/.exec(x)`（RegExp.exec）已排除，避免与字符串 API 同名误报。

JS 命名口径（保守，红线优先）：仅当 `function` 声明名首字母大写（PascalCase
惯例表示构造函数）且全文件无 `new 该名` 调用时才报 low；其余命名维度不报。
"""

from __future__ import annotations

import re
from typing import Pattern

from audit.detect.base import Rule, RuleContext, RuleHit
from audit.detect.rules.js._js_common import (
    call_span,
    function_ranges,
    get_scan,
    is_js_test_file,
)
from audit.models import Category, Severity

__all__ = ["build_js_security_ext_rules"]


class _JsSharedSecurityExtRule(Rule):
    """JS/TS 共享扩充安全规则基类（语法在两种语言中完全一致的安全类问题）。"""

    languages = ("javascript", "typescript")


class _JsExtStyleRule(Rule):
    """仅作用于 JavaScript 的扩充风格规则基类。"""

    languages = ("javascript",)


# ---------------------------------------------------------------- 命令注入

_REQUIRE_CP_RE: Pattern[str] = re.compile(
    r"\brequire\s*\(\s*['\"](?:node:)?child_process['\"]\s*\)"
)
_IMPORT_CP_RE: Pattern[str] = re.compile(r"\bimport\b[^;]*['\"](?:node:)?child_process['\"]")
_FROM_CP_RE: Pattern[str] = re.compile(r"\bfrom\s*['\"](?:node:)?child_process['\"]")
# 掩码行上必须残留导入/来源关键字，才认 raw 行上的 child_process 证据
# （纯字符串/注释里出现的 require('child_process') 文本不算导入）
_IMPORT_KEYWORD_RE: Pattern[str] = re.compile(r"\b(?:require|import|from|export)\b")

_CP_CALL_RE: Pattern[str] = re.compile(r"\.\s*(execSync|exec|spawnSync|spawn)\s*\(")
# 调用点之前紧邻字符为正则字面量收口（/foo/.exec）或引号时不算 child_process 调用
_BEFORE_DOT_INVALID = ("/", "'", '"', "`")


class JsCommandInjectionRule(_JsSharedSecurityExtRule):
    """child_process 命令拼接执行（命令注入）。"""

    id = "JS-COMMAND-INJECTION"
    category = Category.SECURITY
    severity = Severity.HIGH
    description = (
        "通过 child_process 的 exec/execSync/spawn/spawnSync 执行拼接了变量或"
        "模板插值的命令：外部输入混入 shell 元字符（如 `; rm -rf /`）即可注入"
        "任意命令，导致远程代码执行；建议改用 execFile/数组参数形式（不经 shell "
        "解析）并对输入做白名单校验。"
    )

    bad_example = (
        "const cp = require('child_process');\n"
        "\n"
        "function ping(host) {\n"
        "  return cp.exec('ping -c 1 ' + host);  // host 可注入 shell 元字符\n"
        "}\n"
    )
    good_example = (
        "const { execFile } = require('child_process');\n"
        "\n"
        "function ping(host) {\n"
        "  return execFile('ping', ['-c', '1', host]);  // 数组参数不经 shell 解析\n"
        "}\n"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        import_line = self._first_import_line(ctx, scan)
        if import_line is None:
            return []
        hits: list[RuleHit] = []
        for idx in range(import_line - 1, len(scan.masked)):
            masked = scan.masked[idx]
            for m in _CP_CALL_RE.finditer(masked):
                open_col = m.end() - 1
                # 以「点号」为界取前缀：/ab+c/.exec 的点号紧随正则字面量收口
                before_dot = masked[: m.start()].rstrip()
                if not before_dot or before_dot.endswith(_BEFORE_DOT_INVALID):
                    continue  # /re/.exec 为正则 API，非命令执行
                span = call_span(scan.masked, idx + 1, open_col)
                if span is None:
                    continue
                first = span[2].split(",")[0].strip()
                if not self._is_dynamic(first):
                    continue  # 纯字符串字面量/常量，无注入面
                fn = m.group(1)
                hits.append(
                    self.make_hit(
                        ctx,
                        idx + 1,
                        span[0],
                        f"第 {idx + 1} 行调用 `child_process` 的 `{fn}` 且首个参数由变量/拼接构成："
                        "外部输入进入 shell 命令可注入任意命令（如 `; rm -rf /`），"
                        "属高危命令执行漏洞；请改用 `execFile`/数组参数形式（不经 shell 解析）"
                        "并对输入做白名单校验。",
                        meta={"func": fn},
                    )
                )
        return hits

    @staticmethod
    def _first_import_line(ctx: RuleContext, scan) -> int | None:
        """首个 child_process 导入行（1-based）；无导入返回 None。

        证据在 raw 行上取（模块名在字符串字面量内，掩码行看不到），但要求掩码
        行上仍残留 require/import/from/export 关键字，排除注释与纯字符串误认。
        """
        for idx, raw in enumerate(ctx.lines):
            masked = scan.masked[idx]
            if not _IMPORT_KEYWORD_RE.search(masked):
                continue
            if (
                _REQUIRE_CP_RE.search(raw)
                or _IMPORT_CP_RE.search(raw)
                or _FROM_CP_RE.search(raw)
            ):
                return idx + 1
        return None

    @staticmethod
    def _is_dynamic(first: str) -> bool:
        """首个实参（掩码文本）是否含动态成分：拼接 / 模板插值 / 非常量标识符。

        掩码把字符串字面量内容置空、保留引号与 `${`/`}` 定界符，因此：
        - `"ls -la"` → 置空后仅剩引号壳 → 非动态；
        - `` `ls ${dir}` `` → `${`/`}` 与插值代码保留 → 动态；
        - `"ls " + dir` → `+` 与标识符字母保留 → 动态。
        """
        if not first:
            return False
        if "`" in first and "${" in first:
            return True
        leftover = (
            first.replace("'", "")
            .replace('"', "")
            .replace("`", "")
            .replace("${", "")
            .replace("}", "")
            .strip()
        )
        if not leftover:
            return False
        if leftover.isdigit() or leftover in ("null", "undefined", "true", "false"):
            return False
        return True


# ---------------------------------------------------------------- 命名风格（保守口径）


class JsNamingStyleRule(_JsExtStyleRule):
    """function 名 PascalCase 但全文件无 `new` 使用（疑似误用构造函数惯例）。"""

    id = "JS-NAMING-STYLE"
    category = Category.STYLE
    severity = Severity.LOW
    description = (
        "function 声明名首字母大写（PascalCase）但全文件未见 `new 该名` 调用："
        "大写开头的函数名按 JS 惯例表示需要 new 的构造函数/类，误用会误导调用方"
        "直接调用；若确为构造函数请改用 class，否则应改为小写驼峰命名。"
    )

    bad_example = (
        "function RenderUser(user) {\n"
        "  return user.name;  // 全文件无 new RenderUser(...)，疑似普通函数\n"
        "}\n"
    )
    good_example = (
        "function renderUser(user) {\n"
        "  return user.name;\n"
        "}\n"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        if is_js_test_file(ctx.rel_path):
            return []
        scan = get_scan(ctx.lines, ctx.meta)
        masked_all = "\n".join(scan.masked)
        hits: list[RuleHit] = []
        for fr in function_ranges(scan.masked):
            if fr.kind != "function":
                continue  # 类/方法/箭头函数不在本条口径（红线优先，宁可窄）
            # function_ranges 的跨行头匹配会把「空行 + 下一行函数头」拼出一个
            # start 落在空行上的幽灵区间，导致同一函数重复上报：跳过空起始行。
            if not scan.masked[fr.start - 1].strip():
                continue
            name = fr.name
            if not name or not name[0].isupper():
                continue
            if re.search(rf"\bnew\s+{re.escape(name)}\b", masked_all):
                continue  # 存在 new 使用：按构造函数惯例豁免
            hits.append(
                self.make_hit(
                    ctx,
                    fr.start,
                    fr.start,
                    f"第 {fr.start} 行 function 名 `{name}` 为 PascalCase 但全文件无 `new {name}` "
                    "调用：大写开头的函数名按惯例表示需要 new 的构造函数，直接调用会误导使用者；"
                    "请改为小写驼峰命名，若确为构造函数请改用 class。",
                )
            )
        return hits


# ---------------------------------------------------------------- 注册


def build_js_security_ext_rules() -> list[Rule]:
    """构建安全与命名扩充全部 JS/TS 规则实例（顺序即默认报告顺序）。"""
    return [
        JsCommandInjectionRule(),
        JsNamingStyleRule(),
    ]
