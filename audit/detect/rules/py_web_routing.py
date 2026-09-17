"""Python Web 框架路由安全标疑规则库（W21 卡3，2 条 security 标疑）。

与 py_security_ops.py/py_orm.py 同一实现思路：基于 `_python_common` 的逐行掩码
扫描（不依赖 tree-sitter），行号精确落在真实代码行上；`check()` 纯函数式，不修改
ctx、无 IO。独立成文件，不触碰既有规则模块；注册接线由集成人统一完成，本文件只
提供 build_py_web_routing_rules()。

标疑的两类路由安全缺口：
- PY-WEB-ROUTE-NO-AUTH（medium，标疑）：FastAPI/Flask 路由处理函数**无鉴权特征**
  ——公开端点可能暴露敏感操作（CWE-306 missing authentication）。静态分析无法
  证明端点确实需要鉴权（健康检查/公开页面本来就不该鉴权），故定位为**标疑**：
  severity=medium、meta["suspect"]=True、消息明示"低置信标疑"，请按业务语义甄别。
- PY-WEB-NO-RATE-LIMIT（low，标疑）：文件引入 Web 框架路由却全程无限流特征
  （slowapi/flask_limiter/fastapi_limiter/ratelimit/limits 等 import，或
  `@x.limit(` 用法）→ 每文件报一次（锚定首个路由装饰器行，天然去重）。

启用前提（两规则一致）：同文件掩码行上存在 fastapi/flask 的 import
（`import fastapi`/`from fastapi import ...`/`import flask`/`from flask import ...`，
含子模块如 fastapi.APIRouter/flask.Blueprint 的来源包）；无框架 import 的普通
装饰器代码（自研注册表、HTTP 客户端封装等）零命中——这是硬性误报红线。

PY-WEB-ROUTE-NO-AUTH 的鉴权豁免口径（满足其一即豁免，保守偏宽松=宁漏勿误）：
1) 路由装饰器参数含 `dependencies=` / `Depends(` / `Security(`（FastAPI 标准鉴权
   依赖注入形态，如 `dependencies=[Depends(get_current_user)]`）；
2) 同文件（函数体内或模块级）出现 token/session/auth/permission 校验线索：
   verify/validate/decode 等 + token 组合、JWT/OAuth/Bearer、authenticate/
   authorize 词族、current_user、require/check + auth/login/permission 组合、
   `xx_required` 形态、裸 session——线索表刻意宽松（`API_TOKEN = ...` 这类出站
   凭据也会豁免全文件），换取接近零误报；标疑规则的取舍方向是不打扰；
3) 处理函数上方（装饰器堆叠中）存在含 auth/login/required 字样的其他装饰器
   （如 @login_required/@auth_required 自研守卫）。

已知限制（如实声明）：
- 函数签名默认参数形态 `def me(user=Depends(get_current_user))` 依赖第 2 条
  （resolver 名含鉴权词）豁免；`Depends(check_access)` 这类不含鉴权词的自研
  resolver 名识别不了，会标疑——低置信可忽略。
- APIRouter(...) 构造器级 `dependencies=[Depends(...)]`（router 级鉴权）不识别，
  会标疑——同上按标疑处理。
- 装饰器行与 def 必须同缩进（类内蓝图方法同缩进可识别）；`app.add_url_rule(...)`
  非装饰器形态不在本规则口径内。
- 单文件口径：跨文件的全局鉴权中间件（如 app 层 middleware）不可见；无任何
  文件内线索时会标疑——请结合架构审查判断。
"""

from __future__ import annotations

import re
from typing import Pattern

from audit.detect.base import Rule, RuleContext, RuleHit
from audit.detect.rules._python_common import call_span, get_scan
from audit.detect.rules._scan_common import indent_width
from audit.models import Category, Severity

__all__ = [
    "WebNoRateLimitRule",
    "WebRouteNoAuthRule",
    "build_py_web_routing_rules",
]


class _PyWebRoutingRule(Rule):
    """Web 路由安全标疑规则公共基类：声明语言。"""

    languages = ("python",)

    # 启用前提：同文件存在 fastapi/flask（含子模块来源包）的 import（掩码行上
    # 匹配，字符串/注释里的 "fastapi" 字样不构成启用条件）
    _FRAMEWORK_IMPORT_RE: Pattern[str] = re.compile(
        r"^\s*(?:from\s+fastapi\b|import\s+fastapi\b|from\s+flask\b|import\s+flask\b)"
    )

    def _framework_enabled(self, masked: list[str]) -> bool:
        return any(self._FRAMEWORK_IMPORT_RE.match(ln) for ln in masked)

    # 路由装饰器：@<obj>.<verb>( / @<obj>.route( / @<obj>.api_route(
    # （obj 限 \w 标识符：app/router/bp/api 等；@obj.sub.get 深层形态不在口径内）
    _ROUTE_DECORATOR_RE: Pattern[str] = re.compile(
        r"^\s*@(\w+)\s*\.\s*(get|post|put|delete|patch|head|options|route|api_route)\s*\("
    )

    @staticmethod
    def _route_decorators(masked: list[str]) -> list[tuple[int, str, str, str, int]]:
        """收集全部路由装饰器：[(首行号, 对象名, 方法名, 装饰器参数文本, 结束行号)]。

        参数文本取自掩码行跨行括号配对（call_span），字符串内容已置空——
        路径字面量 "/login" 不会污染后续鉴权线索判定；结束行号供处理函数
        定位跳过跨行装饰器调用体。
        """
        out: list[tuple[int, str, str, str, int]] = []
        for idx, ln in enumerate(masked):
            m = _PyWebRoutingRule._ROUTE_DECORATOR_RE.match(ln)
            if not m:
                continue
            lineno = idx + 1
            open_col = m.end() - 1  # 模式以 `\s*\(` 收尾，匹配末字符即 '('
            span = call_span(masked, lineno, open_col)
            if span is None:  # 括号永不闭合（语法残缺）：按单行保守处理
                out.append((lineno, m.group(1), m.group(2), "", lineno))
            else:
                out.append((lineno, m.group(1), m.group(2), span[2], span[0]))
        return out


# ==================================================================== 规则 1


# 装饰器参数鉴权特征：dependencies= / Depends( / Security(
_DECORATOR_AUTH_ARG_RE: Pattern[str] = re.compile(
    r"(?i)\b(?:dependencies\s*=|depends\s*\(|security\s*\()"
)

# 同文件鉴权校验线索（保守偏宽松，见模块 docstring 取舍）：
# token 校验组合 / 含 token 词根的标识符（api_token/tokens 等，不带词首边界——
# get_current_user/API_TOKEN 这类下划线复合名前无 \b）/ JWT·OAuth·Bearer /
# authenticate·authorize 词族 / current_user / require·check + auth·login·permission
# 组合 / xx_required / 含 permission·session 词根的标识符
_AUTH_CLUE_RE: Pattern[str] = re.compile(
    r"(?i)(?:"
    r"(?:verify|validate|decode|check|parse|load|read)_?\w*tokens?"
    r"|\w*tokens?"
    r"|jwt\w*"
    r"|oauth[\w.]*"
    r"|bearer"
    r"|authenticat\w+"
    r"|authoriz\w+"
    r"|current_?user"
    r"|(?:require|check|has|need)_?\w*(?:auth|login|permission)\w*"
    r"|(?:auth|login|permission)\w*required"
    r"|\w*permissions?"
    r"|\w*sessions?"
    r")"
)

# 堆叠装饰器豁免：含 auth/login/required 字样（@login_required/@auth_required 等）
_SIBLING_AUTH_DECORATOR_RE: Pattern[str] = re.compile(r"(?i)(auth|login|required)")
_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")


class WebRouteNoAuthRule(_PyWebRoutingRule):
    """Web 路由处理函数无鉴权特征：公开端点标疑（CWE-306，低置信标疑）。"""

    id = "PY-WEB-ROUTE-NO-AUTH"
    category = Category.SECURITY
    severity = Severity.MEDIUM
    description = (
        "FastAPI/Flask 路由装饰器的处理函数无任何鉴权特征（装饰器无 "
        "dependencies=/Depends(/Security(，同文件无 token/session/auth/permission "
        "校验线索，函数上方无 auth/login/required 守卫装饰器）：端点可能匿名暴露"
        "敏感操作。低置信标疑——健康检查/公开页面本就无需鉴权，请按业务语义甄别。"
    )

    bad_example = (
        "from fastapi import FastAPI\n"
        "\n"
        "app = FastAPI()\n"
        "\n"
        '@app.delete("/users/{uid}")  # 删用户接口无任何鉴权\n'
        "def delete_user(uid: int):\n"
        "    db.delete_user(uid)\n"
    )
    good_example = (
        "from fastapi import FastAPI, Depends\n"
        "\n"
        "app = FastAPI()\n"
        "\n"
        "@app.delete(\"/users/{uid}\", dependencies=[Depends(require_admin)])\n"
        "def delete_user(uid: int):\n"
        "    db.delete_user(uid)\n"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        masked = scan.masked
        if not self._framework_enabled(masked):
            return []  # 无框架 import 的普通装饰器代码：硬性红线，零命中
        hits: list[RuleHit] = []
        file_has_auth_clue = any(_AUTH_CLUE_RE.search(ln) for ln in masked)
        for lineno, obj, verb, args, end_line in self._route_decorators(masked):
            if _DECORATOR_AUTH_ARG_RE.search(args):
                continue  # 豁免 1：装饰器参数 dependencies=/Depends(/Security(
            dec_indent = indent_width(masked[lineno - 1])
            handler = self._find_handler(masked, end_line, dec_indent)
            if handler is None:
                continue  # 找不到被装饰的 def（孤装饰器/非法结构）：保守不报
            name, _def_line, siblings_below = handler
            siblings = [*self._siblings_above(masked, lineno, dec_indent), *siblings_below]
            if any(_SIBLING_AUTH_DECORATOR_RE.search(d) for d in siblings):
                continue  # 豁免 3：堆叠装饰器含 auth/login/required 字样（守卫）
            if file_has_auth_clue:
                continue  # 豁免 2：同文件存在 token/session/auth/permission 线索
            hits.append(
                self.make_hit(
                    ctx,
                    lineno,
                    lineno,
                    f"标疑（低置信）：第 {lineno} 行路由装饰器 `@{obj}.{verb}` 的处理函数 "
                    f"`{name}` 无鉴权特征（装饰器无 dependencies=/Depends(/Security(，"
                    "同文件无 token/session/auth/permission 校验线索，堆叠装饰器无 "
                    "auth/login/required 守卫）：端点可能匿名暴露敏感操作"
                    "（CWE-306）。健康检查/公开端点本就无需鉴权，请按业务语义甄别；"
                    "确认后补鉴权依赖或中间件。",
                    meta={"suspect": True, "route_obj": obj, "method": verb, "handler": name},
                )
            )
        return hits

    @staticmethod
    def _siblings_above(masked: list[str], decorator_line: int, dec_indent: int) -> list[str]:
        """向上收集与路由装饰器相邻（同缩进、连续 @ 行）的堆叠装饰器文本。"""
        sibs: list[str] = []
        k = decorator_line - 2  # 装饰器首行的上一行（0-based）
        while k >= 0:
            ln = masked[k]
            if not ln.strip() or indent_width(ln) != dec_indent:
                break
            if not ln.lstrip().startswith("@"):
                break
            sibs.append(ln.strip())
            k -= 1
        return sibs

    @staticmethod
    def _find_handler(
        masked: list[str], dec_end_line: int, dec_indent: int
    ) -> tuple[str, int, list[str]] | None:
        """从路由装饰器结束行的下一行向后找被装饰的处理函数 def。

        返回 (函数名, def 行号, 途经堆叠装饰器行列表)；找不到（孤装饰器/遇到
        同缩进的其他语句）返回 None。途经的同缩进 @ 行视为堆叠装饰器（供守卫
        豁免）；装饰器调用跨行续体（更深缩进）直接跳过。
        """
        siblings: list[str] = []
        for k in range(dec_end_line, len(masked)):  # dec_end_line 为 1-based 结束行
            ln = masked[k]
            if not ln.strip():
                continue
            m = _DEF_RE.match(ln)
            if m is not None:
                if indent_width(ln) != dec_indent:
                    return None  # 装饰器与 def 缩进不一致：结构异常，保守放弃
                return m.group(1), k + 1, siblings
            ind = indent_width(ln)
            if ind > dec_indent:
                continue  # 装饰器调用跨行续体等更深层行
            if ln.lstrip().startswith("@"):
                if ind == dec_indent:
                    siblings.append(ln.strip())  # 同缩进堆叠装饰器（守卫豁免线索）
                continue
            return None  # 同缩进的其他语句：路由装饰器没有修饰任何函数
        return None


# ==================================================================== 规则 2


# 限流特征 import：slowapi / *ratelimit* / *rate_limit* / *limiter* / limits
_RATE_LIMIT_IMPORT_RE: Pattern[str] = re.compile(
    r"(?i)^\s*(?:from|import)\s+[\w.]*(?:rate_?limit|slowapi|limiter|limits)[\w.]*"
)
# 限流特征用法（import 之外的兜底线索，如 star 导入后的 @limiter.limit("5/minute")）
_RATE_LIMIT_USAGE_RE: Pattern[str] = re.compile(r"(?i)@\w+\s*\.\s*limit\s*\(")


class WebNoRateLimitRule(_PyWebRoutingRule):
    """文件含 Web 路由但全程无限流特征：缺少速率限制标疑（每文件至多一条）。"""

    id = "PY-WEB-NO-RATE-LIMIT"
    category = Category.SECURITY
    severity = Severity.LOW
    description = (
        "文件引入 FastAPI/Flask 路由却未 import 任何限流特征（slowapi/"
        "flask_limiter/fastapi_limiter/ratelimit/limits 等）且无 @x.limit( 用法："
        "端点缺少速率限制，可被暴力枚举/爬取/DoS 滥用。低置信标疑——限流可能由"
        "网关/反向代理/全局中间件承担，请按部署架构甄别；每文件只报一次。"
    )

    bad_example = (
        "from fastapi import FastAPI\n"
        "\n"
        "app = FastAPI()\n"
        "\n"
        '@app.post("/login")  # 登录端点无限流：可被暴力破解\n'
        "def login(form: LoginForm):\n"
        "    ...\n"
    )
    good_example = (
        "from fastapi import FastAPI\n"
        "from slowapi import Limiter\n"
        "from slowapi.util import get_remote_address\n"
        "\n"
        "limiter = Limiter(key_func=get_remote_address)\n"
        "app = FastAPI()\n"
        "\n"
        '@app.post("/login")\n'
        '@limiter.limit("5/minute")  # 按来源 IP 限流\n'
        "def login(form: LoginForm):\n"
        "    ...\n"
    )

    def check(self, ctx: RuleContext) -> list[RuleHit]:
        scan = get_scan(ctx.lines, ctx.meta)
        masked = scan.masked
        if not self._framework_enabled(masked):
            return []  # 启用前提与规则 1 一致：无框架 import 零命中
        if any(_RATE_LIMIT_IMPORT_RE.match(ln) for ln in masked):
            return []  # 已有限流 import：豁免
        if any(_RATE_LIMIT_USAGE_RE.search(ln) for ln in masked):
            return []  # 已有 @x.limit( 限流用法：豁免
        decorators = self._route_decorators(masked)
        if not decorators:
            return []
        first_line = decorators[0][0]
        route_desc = "、".join(f"@{obj}.{verb}" for _ln, obj, verb, _a, _e in decorators[:3])
        more = len(decorators) - 3
        return [
            self.make_hit(
                ctx,
                first_line,
                first_line,
                f"标疑（低置信）：本文件含 {len(decorators)} 个 Web 路由装饰器"
                f"（{route_desc}{' 等' if more > 0 else ''}）但未 import 任何限流特征"
                "（slowapi/flask_limiter/fastapi_limiter/ratelimit/limits）且无 "
                "@x.limit( 限流用法：端点缺少速率限制，可被暴力枚举/爬取/DoS 滥用；"
                "限流若由网关/反向代理/全局中间件承担，可忽略本条。",
                meta={"suspect": True, "route_count": len(decorators), "anchor": "first-route"},
            )
        ]


# ==================================================================== 注册


def build_py_web_routing_rules() -> list[Rule]:
    """构建 W21 Web 路由安全标疑规则实例（顺序即默认报告顺序）。

    注册接线由集成人统一完成（并行窗口约定，同 build_security_ops_rules），
    本文件只提供构建函数。
    """
    return [
        WebRouteNoAuthRule(),
        WebNoRateLimitRule(),
    ]
