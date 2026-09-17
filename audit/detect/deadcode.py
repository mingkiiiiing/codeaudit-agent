"""W16 全库后处理扫描器：死代码（全库零引用的模块级私有符号）检测。

对外契约（audit.detect.engine._post_scan_issues 延迟导入本模块）：
- run(ctx) -> list[Issue]；永不抛异常——内部失败写
  ctx.extra["post_scan_errors"]["audit.detect.deadcode"] 后返回空列表；
- Issue 不填 id（engine 统一补号），category=STYLE、severity=LOW、
  confidence=0.7、source=RULE，evidence 携带 "rule:PY-DEADCODE" 等规则 id。

口径（保守优先，宁漏勿误）：
- Python：模块级 `def _name(...)` / `_name = ...`（单下划线私有，天然排除
  dunder 与双下划线名）；全库文本级引用计数（\\b_name\\b 出现次数）== 1
  （仅定义处）才报 PY-DEADCODE。
  豁免：__init__.py 中定义（re-export 惯例）、dunder、被 __all__ 字符串提及、
  def 行上方最近非空行以 @ 开头（装饰器注册，如 Flask 路由）、tests/ 目录或
  test_ 前缀文件；类方法不在扫描范围（只查模块级）。
- JS/TS：顶层（零缩进）非 export 的 `function name(...)` / `async function
  name(...)` 声明；引用计数在剔除字符串字面量与 // 注释后统计（被字符串提及
  的名字不算引用，照报）；== 1（仅定义处）报 JS-DEADCODE / TS-DEADCODE。
  豁免：export 开头的声明（export function/export async function 天然不匹配
  采集正则）、被赋值给 module.exports 的名字。

已知局限（description 同步告知）：引用计数为文本级正则近似；getattr/字符串
动态调用无法识别；python 多行签名 def、JS 跨行模板字符串按单行简单处理；
同名符号多处定义时引用计数互相保护（均不报）。
"""

from __future__ import annotations

import re
from typing import Any

from audit.models import Category, Issue, IssueSource, Severity
from audit.utils import guess_language

MODULE_NAME = "audit.detect.deadcode"

_CONFIDENCE = 0.7

_RULE_BY_LANG = {
    "javascript": "JS-DEADCODE",
    "typescript": "TS-DEADCODE",
}

# Python 模块级下划线私有定义（零缩进；单下划线+字母开头，天然排除 dunder/
# 双下划线名与类方法）。def 与模块级赋值（含简单注解赋值 _x: int = 1）。
_PY_FUNC_DEF_RE = re.compile(r"^(?:async\s+)?def\s+(_[A-Za-z]\w*)\s*\(")
_PY_VAR_DEF_RE = re.compile(r"^(_[A-Za-z]\w*)\s*(?::[^=]+)?=(?!=)")
# js/ts 顶层（零缩进）函数声明；export 开头的声明天然不匹配（采集即豁免）
_JS_FUNC_DEF_RE = re.compile(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(")

# js/ts 单行字符串字面量（用于引用计数前剔除字符串提及）
_JS_STRING_RES = (
    re.compile(r"`(?:[^`\\]|\\.)*`"),
    re.compile(r"'(?:[^'\\\n]|\\.)*'"),
    re.compile(r'"(?:[^"\\\n]|\\.)*"'),
)
_DUNDER_RE = re.compile(r"__.+__")


# ---------------------------------------------------------------- 入口与降级


def run(ctx: Any) -> list[Issue]:
    """引擎挂点入口：全库死代码检测，返回 Issue 列表（不填 id）。永不抛异常。"""
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


# ---------------------------------------------------------------- 采集定义


def _is_test_file(rel: str) -> bool:
    """tests/ 目录下或 test_ 前缀文件视为测试代码，不参与死代码定义采集。"""
    parts = rel.split("/")
    if any(part == "tests" for part in parts[:-1]):
        return True
    return parts[-1].startswith("test_")


def _collect_all_names(lines: list[str]) -> set[str]:
    """收集模块 __all__ 赋值中的字符串元素（含跨行列表的简单处理）。"""
    names: set[str] = set()
    in_all = False
    for line in lines:
        if "__all__" in line:
            in_all = True
        if in_all:
            for quoted in re.findall(r"[\"']([^\"']+)[\"']", line):
                names.add(quoted)
            if "]" in line:
                in_all = False
    return names


def _collect_py_defs(rel: str, lines: list[str]) -> list[tuple[str, str, int, str, str]]:
    """收集模块级下划线私有定义。

    返回 (name, rel, 1-based 行号, 定义行文本, kind)；kind ∈ {"函数", "变量"}。
    豁免：__init__.py（re-export 惯例）、tests/ 或 test_ 前缀文件、dunder、
    __all__ 字符串提及、def 上方最近非空行以 @ 开头（装饰器注册式用法）。
    """
    filename = rel.split("/")[-1]
    if filename == "__init__.py" or _is_test_file(rel):
        return []
    all_names = _collect_all_names(lines)
    defs: list[tuple[str, str, int, str, str]] = []
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        match = _PY_FUNC_DEF_RE.match(line)
        if match:
            kind = "函数"
        else:
            match = _PY_VAR_DEF_RE.match(line)
            if not match:
                continue
            kind = "变量"
        name = match.group(1)
        if _DUNDER_RE.fullmatch(name) or name in all_names:
            continue
        # 装饰器豁免：def 行上方最近非空行以 @ 开头（Flask 路由等注册式用法）
        decorated = False
        for above in reversed(lines[: lineno - 1]):
            if above.strip():
                decorated = above.strip().startswith("@")
                break
        if decorated:
            continue
        defs.append((name, rel, lineno, stripped, kind))
    return defs


def _collect_js_defs(rel: str, lines: list[str]) -> list[tuple[str, str, int, str, str]]:
    """收集 js/ts 顶层非导出函数声明。

    返回 (name, rel, 1-based 行号, 定义行文本, language)。
    export function / export async function 天然不匹配采集正则（豁免）；
    被赋值给 module.exports 的名字单独豁免。
    """
    language = guess_language(rel) or ""
    if language not in _RULE_BY_LANG or _is_test_file(rel):
        return []
    export_lines = [line for line in lines if "module.exports" in line]
    defs: list[tuple[str, str, int, str, str]] = []
    for lineno, line in enumerate(lines, start=1):
        if line != line.lstrip():
            continue  # 只查零缩进的顶层声明
        match = _JS_FUNC_DEF_RE.match(line)
        if not match:
            continue
        name = match.group(1)
        if any("module.exports" in entry and name in entry for entry in export_lines):
            continue  # 赋值给 module.exports：视为对外导出
        defs.append((name, rel, lineno, line.strip(), language))
    return defs


# ---------------------------------------------------------------- 引用计数


def _js_code_only(text: str) -> str:
    """剔除 js/ts 文本中的单行字符串字面量与 // 注释：字符串提及不算引用。"""
    out_lines: list[str] = []
    for raw in text.splitlines():
        line = raw
        for pattern in _JS_STRING_RES:
            line = pattern.sub(" ", line)
        line = line.split("//", 1)[0]
        out_lines.append(line)
    return "\n".join(out_lines)


def _count_refs(names: set[str], texts: list[str]) -> dict[str, int]:
    """全库文本级引用计数：对每个候选名统计出现次数（一次扫描多名字）。"""
    if not names:
        return {}
    alternation = "|".join(sorted((re.escape(name) for name in names), key=len, reverse=True))
    pattern = re.compile(rf"(?<![A-Za-z0-9_$])(?:{alternation})(?![A-Za-z0-9_$])")
    counts: dict[str, int] = dict.fromkeys(names, 0)
    for text in texts:
        for name in pattern.findall(text):
            counts[name] += 1
    return counts


# ---------------------------------------------------------------- 主扫描


def _scan(ctx: Any) -> list[Issue]:
    try:
        paths = list(ctx.workspace.source_files(None))
    except Exception as exc:
        _record_error(ctx, f"workspace.source_files 失败: {exc!r}")
        return []
    # files: (rel, language, 行列表, 原文)
    files: list[tuple[str, str, list[str], str]] = []
    for path in paths:
        try:
            rel = ctx.workspace.rel(path)
        except Exception:
            continue  # 单文件路径异常不阻断
        language = guess_language(rel) or ""
        try:
            text = ctx.workspace.read_file_text(rel)
        except Exception:
            continue  # 单文件读取失败不阻断
        files.append((rel, language, text.splitlines(), text))

    # defs: (name, rel, 行号, 定义行, kind/language)
    py_defs: list[tuple[str, str, int, str, str]] = []
    js_defs: list[tuple[str, str, int, str, str]] = []
    for rel, language, lines, _text in files:
        if language == "python":
            py_defs.extend(_collect_py_defs(rel, lines))
        elif language in _RULE_BY_LANG:
            js_defs.extend(_collect_js_defs(rel, lines))

    if not py_defs and not js_defs:
        return []
    # 引用计数范围 = 全部源文件：python 用原文（注释/字符串提及也计入，保守）；
    # js/ts 先剔除字符串与 // 注释（字符串提及不算引用，照报）。
    raw_texts = [text for (_rel, _lang, _lines, text) in files]
    py_counts = _count_refs({entry[0] for entry in py_defs}, raw_texts)
    js_texts = [_js_code_only(text) for (_rel, _lang, _lines, text) in files]
    js_counts = _count_refs({entry[0] for entry in js_defs}, js_texts)

    issues: list[Issue] = []
    for name, rel, lineno, def_line, kind in py_defs:
        if py_counts.get(name, 0) <= 1:  # 仅定义处出现 -> 全库零引用
            issues.append(
                _make_issue(
                    rule_id="PY-DEADCODE",
                    name=name,
                    rel=rel,
                    lineno=lineno,
                    def_line=def_line,
                    label=f"模块级私有{kind}",
                    limit_note="引用计数为文本级正则近似，注释/字符串中的提及同样计入（宁可漏报）。",
                )
            )
    for name, rel, lineno, def_line, language in js_defs:
        if js_counts.get(name, 0) <= 1:
            issues.append(
                _make_issue(
                    rule_id=_RULE_BY_LANG[language],
                    name=name,
                    rel=rel,
                    lineno=lineno,
                    def_line=def_line,
                    label="未导出的顶层函数",
                    limit_note=(
                        "引用计数已剔除字符串字面量与 // 注释；跨行模板字符串等"
                        "复杂情形按单行简单处理。"
                    ),
                )
            )
    issues.sort(key=lambda issue: (issue.file, issue.line_start))
    return issues


def _make_issue(
    rule_id: str,
    name: str,
    rel: str,
    lineno: int,
    def_line: str,
    label: str,
    limit_note: str,
) -> Issue:
    """构造一条死代码 Issue（category=STYLE / severity=LOW / confidence=0.7）。"""
    return Issue(
        category=Category.STYLE,
        severity=Severity.LOW,
        title=f"{label} {name} 全库零引用（疑似死代码）"[:120],
        file=rel,
        line_start=lineno,
        line_end=lineno,
        code_snippet=def_line,
        description=(
            f"{label} `{name}`（第 {lineno} 行）在全库的引用计数为 1（仅定义处出现），"
            f"静态分析视角下无任何调用或读取。\n"
            f"静态保守口径：仅报告下划线私有（或非导出）且全库零引用的符号；"
            f"若为反射/动态调用目标请忽略。\n{limit_note}"
        ),
        evidence=[
            f"rule:{rule_id}",
            f"loc:{rel}:{lineno}",
            "refs_total:1",
        ],
        suggestion=(
            "确认非反射/动态调用目标后删除该符号；若确需保留，"
            "请补充调用、显式导出或在 __all__/module.exports 中声明用途。"
        ),
        confidence=_CONFIDENCE,
        source=IssueSource.RULE,
    )
