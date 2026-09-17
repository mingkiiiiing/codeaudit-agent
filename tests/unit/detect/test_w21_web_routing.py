"""W21 卡3 Web 路由安全标疑规则（py_web_routing.py）正反用例。

正例覆盖：FastAPI @app/@router 路由（get/post/async/多行装饰器）与 Flask
@app.route/@bp.route 无鉴权形态；限流规则的每文件一次锚定。

豁免反例（标疑降噪口径）：装饰器参数 dependencies=/Depends(/Security(、
签名默认参数 Depends(get_current_user)、同文件 token/session 校验线索、
堆叠 auth/login/required 守卫装饰器（上方与下方两种位置）、限流 import
（slowapi/flask_limiter/ratelimit）与 @x.limit( 用法。

红线反例（误报高危点）：无 fastapi/flask import 的普通装饰器代码（自研注册表）
两规则全 0 命中；字符串/注释里的框架字样不构成启用前提；带鉴权特征的路由 0 命中。
"""

from __future__ import annotations

from audit.detect.rules.py_web_routing import (
    WebNoRateLimitRule,
    WebRouteNoAuthRule,
    build_py_web_routing_rules,
)


class TestWebRouteNoAuth:
    rule = WebRouteNoAuthRule()

    # ------------------------------------------------------------ 正例（标疑）

    def test_fastapi_get_hit(self, make_ctx):
        ctx = make_ctx(
            "from fastapi import FastAPI\n"
            "\n"
            "app = FastAPI()\n"
            "\n"
            '@app.get("/items")\n'
            "def list_items():\n"
            "    return db.all()\n"
        )
        hits = self.rule.check(ctx)
        assert [h.line_start for h in hits] == [5]
        assert hits[0].severity.value == "medium"
        assert hits[0].category.value == "security"
        assert hits[0].meta["suspect"] is True
        assert hits[0].meta["handler"] == "list_items"
        assert hits[0].meta["method"] == "get"
        assert "标疑" in hits[0].message

    def test_fastapi_router_post_hit(self, make_ctx):
        ctx = make_ctx(
            "from fastapi import APIRouter\n"
            "\n"
            "router = APIRouter()\n"
            "\n"
            '@router.post("/orders")\n'
            "def create_order():\n"
            "    return db.insert()\n"
        )
        assert [h.line_start for h in self.rule.check(ctx)] == [5]

    def test_flask_route_hit(self, make_ctx):
        ctx = make_ctx(
            "from flask import Flask\n"
            "\n"
            "app = Flask(__name__)\n"
            "\n"
            '@app.route("/users", methods=["POST"])\n'
            "def create_user():\n"
            "    return db.insert()\n"
        )
        hits = self.rule.check(ctx)
        assert [h.line_start for h in hits] == [5]
        assert hits[0].meta["route_obj"] == "app"
        assert hits[0].meta["method"] == "route"

    def test_flask_blueprint_hit(self, make_ctx):
        ctx = make_ctx(
            "from flask import Blueprint\n"
            "\n"
            "bp = Blueprint(\"pay\", __name__)\n"
            "\n"
            '@bp.route("/refund")\n'
            "def refund():\n"
            "    return do_refund()\n"
        )
        assert [h.line_start for h in self.rule.check(ctx)] == [5]

    def test_async_handler_hit(self, make_ctx):
        ctx = make_ctx(
            "from fastapi import FastAPI\n"
            "\n"
            "app = FastAPI()\n"
            "\n"
            '@app.delete("/cache")\n'
            "async def clear_cache():\n"
            "    await cache.clear()\n"
        )
        assert [h.line_start for h in self.rule.check(ctx)] == [5]

    def test_multiline_decorator_hit_at_first_line(self, make_ctx):
        ctx = make_ctx(
            "from fastapi import FastAPI\n"
            "\n"
            "app = FastAPI()\n"
            "\n"
            "@app.get(\n"
            '    "/items",\n'
            ")\n"
            "def list_items():\n"
            "    return db.all()\n"
        )
        hits = self.rule.check(ctx)
        assert [h.line_start for h in hits] == [5]
        assert hits[0].meta["handler"] == "list_items"

    def test_multiple_routes_each_hit(self, make_ctx):
        ctx = make_ctx(
            "from fastapi import FastAPI\n"
            "\n"
            "app = FastAPI()\n"
            "\n"
            '@app.get("/a")\n'
            "def a():\n"
            "    return 1\n"
            "\n"
            '@app.post("/b")\n'
            "def b():\n"
            "    return 2\n"
        )
        assert [h.line_start for h in self.rule.check(ctx)] == [5, 9]

    # ------------------------------------------------------------ 豁免（降噪）

    def test_decorator_dependencies_arg_exempt(self, make_ctx):
        ctx = make_ctx(
            "from fastapi import FastAPI, Depends\n"
            "\n"
            "app = FastAPI()\n"
            "\n"
            '@app.get("/me", dependencies=[Depends(get_current_user)])\n'
            "def me():\n"
            "    return current\n"
        )
        assert self.rule.check(ctx) == []

    def test_decorator_security_arg_exempt(self, make_ctx):
        ctx = make_ctx(
            "from fastapi import FastAPI, Security\n"
            "\n"
            "app = FastAPI()\n"
            "\n"
            '@app.get("/admin", dependencies=[Security(require_admin, scopes=["x"])])\n'
            "def admin():\n"
            "    return stats\n"
        )
        assert self.rule.check(ctx) == []

    def test_signature_depends_current_user_exempt(self, make_ctx):
        # FastAPI 最常见形态：鉴权依赖写在函数签名默认参数（经同文件线索豁免）
        ctx = make_ctx(
            "from fastapi import FastAPI, Depends\n"
            "\n"
            "app = FastAPI()\n"
            "\n"
            '@app.get("/me")\n'
            "def me(user=Depends(get_current_user)):\n"
            "    return user\n"
        )
        assert self.rule.check(ctx) == []

    def test_file_verify_token_clue_exempt(self, make_ctx):
        ctx = make_ctx(
            "from fastapi import FastAPI, Header\n"
            "\n"
            "app = FastAPI()\n"
            "\n"
            "def verify_token(token: str):\n"
            "    if token != EXPECTED:\n"
            "        raise denied\n"
            "\n"
            '@app.get("/reports")\n'
            "def reports():\n"
            "    return query()\n"
        )
        assert self.rule.check(ctx) == []

    def test_file_session_clue_exempt(self, make_ctx):
        ctx = make_ctx(
            "from flask import Flask, session\n"
            "\n"
            "app = Flask(__name__)\n"
            "\n"
            '@app.route("/profile")\n'
            "def profile():\n"
            "    uid = session.get(\"user_id\")\n"
            "    return render(uid)\n"
        )
        assert self.rule.check(ctx) == []

    def test_sibling_guard_below_route_exempt(self, make_ctx):
        # 守卫装饰器位于路由装饰器与 def 之间（下方）
        ctx = make_ctx(
            "from fastapi import FastAPI\n"
            "\n"
            "app = FastAPI()\n"
            "\n"
            '@app.get("/dash")\n'
            "@auth_guard\n"
            "def dashboard():\n"
            "    return render()\n"
        )
        assert self.rule.check(ctx) == []

    def test_sibling_guard_above_route_exempt(self, make_ctx):
        # 守卫装饰器位于路由装饰器上方（含 required 字样但不构成同文件线索，
        # 单独验证堆叠装饰器豁免路径）
        ctx = make_ctx(
            "from fastapi import FastAPI\n"
            "\n"
            "app = FastAPI()\n"
            "\n"
            "@required_consent\n"
            '@app.get("/dash")\n'
            "def dashboard():\n"
            "    return render()\n"
        )
        assert self.rule.check(ctx) == []

    # ------------------------------------------------------------ 红线（零命中）

    def test_no_framework_import_zero_hit(self, make_ctx):
        # 自研注册表/HTTP 客户端的普通装饰器：无框架 import，两规则全 0
        ctx = make_ctx(
            "import registry\n"
            "\n"
            "reg = registry.load()\n"
            "\n"
            '@reg.get("items")\n'
            "def lookup():\n"
            "    return reg.all()\n"
        )
        assert self.rule.check(ctx) == []

    def test_framework_import_in_string_or_comment_no_enable(self, make_ctx):
        # 字符串/注释里的 fastapi/flask 字样不构成启用前提（掩码行判定）
        ctx = make_ctx(
            'README = "pip install fastapi flask"\n'
            "# from flask import Flask\n"
            "\n"
            "reg = load()\n"
            "\n"
            '@reg.get("items")\n'
            "def lookup():\n"
            "    return reg.all()\n"
        )
        assert self.rule.check(ctx) == []

    def test_framework_import_without_routes_zero_hit(self, make_ctx):
        ctx = make_ctx(
            "import flask\n"
            "\n"
            "app = flask.Flask(__name__)\n"
            "\n"
            "def helper():\n"
            "    return 1\n"
        )
        assert self.rule.check(ctx) == []

    def test_route_without_def_conservative_no_hit(self, make_ctx):
        # 装饰器后面跟的不是同缩进 def（孤装饰器）：保守不报
        ctx = make_ctx(
            "from fastapi import FastAPI\n"
            "\n"
            "app = FastAPI()\n"
            "\n"
            '@app.get("/x")\n'
            "handlers = []\n"
        )
        assert self.rule.check(ctx) == []


class TestWebNoRateLimit:
    rule = WebNoRateLimitRule()

    def test_single_hit_per_file_anchored_at_first_route(self, make_ctx):
        ctx = make_ctx(
            "from fastapi import FastAPI\n"
            "\n"
            "app = FastAPI()\n"
            "\n"
            '@app.get("/a")\n'
            "def a():\n"
            "    return 1\n"
            "\n"
            '@app.post("/b")\n'
            "def b():\n"
            "    return 2\n"
        )
        hits = self.rule.check(ctx)
        assert [h.line_start for h in hits] == [5]  # 锚定首个路由装饰器，去重
        assert len(hits) == 1
        assert hits[0].severity.value == "low"
        assert hits[0].category.value == "security"
        assert hits[0].meta["suspect"] is True
        assert hits[0].meta["route_count"] == 2
        assert "标疑" in hits[0].message

    def test_flask_routes_hit_once(self, make_ctx):
        ctx = make_ctx(
            "from flask import Flask\n"
            "\n"
            "app = Flask(__name__)\n"
            "\n"
            '@app.route("/x")\n'
            "def x():\n"
            "    return 1\n"
        )
        assert [h.line_start for h in self.rule.check(ctx)] == [5]

    def test_slowapi_import_exempt(self, make_ctx):
        ctx = make_ctx(
            "from fastapi import FastAPI\n"
            "from slowapi import Limiter\n"
            "from slowapi.util import get_remote_address\n"
            "\n"
            "app = FastAPI()\n"
            "\n"
            '@app.post("/login")\n'
            "def login(form):\n"
            "    return None\n"
        )
        assert self.rule.check(ctx) == []

    def test_flask_limiter_import_exempt(self, make_ctx):
        ctx = make_ctx(
            "from flask import Flask\n"
            "from flask_limiter import Limiter\n"
            "\n"
            "app = Flask(__name__)\n"
            "\n"
            '@app.route("/x")\n'
            "def x():\n"
            "    return 1\n"
        )
        assert self.rule.check(ctx) == []

    def test_ratelimit_import_exempt(self, make_ctx):
        ctx = make_ctx(
            "from ratelimit import limits\n"
            "from fastapi import FastAPI\n"
            "\n"
            "app = FastAPI()\n"
            "\n"
            '@app.get("/x")\n'
            "def x():\n"
            "    return 1\n"
        )
        assert self.rule.check(ctx) == []

    def test_limit_decorator_usage_exempt(self, make_ctx):
        # star 导入等场景下无逐字 import 行，但存在 @x.limit( 限流用法
        ctx = make_ctx(
            "from fastapi import FastAPI\n"
            "from slowapi.extension import *\n"
            "\n"
            "app = FastAPI()\n"
            "\n"
            '@app.post("/login")\n'
            '@limiter.limit("5/minute")\n'
            "def login(form):\n"
            "    return None\n"
        )
        assert self.rule.check(ctx) == []

    def test_no_framework_import_zero_hit(self, make_ctx):
        ctx = make_ctx(
            "import registry\n"
            "\n"
            "reg = registry.load()\n"
            "\n"
            '@reg.get("items")\n'
            "def lookup():\n"
            "    return reg.all()\n"
        )
        assert self.rule.check(ctx) == []

    def test_framework_import_without_routes_zero_hit(self, make_ctx):
        ctx = make_ctx(
            "import flask\n"
            "\n"
            "app = flask.Flask(__name__)\n"
            "\n"
            "def helper():\n"
            "    return 1\n"
        )
        assert self.rule.check(ctx) == []


class TestCleanCorpusExcerpt:
    """clean 语料摘录：两规则全 0 命中（含鉴权齐备的 FastAPI 文件）。"""

    def test_clean_services_file(self, make_ctx):
        ctx = make_ctx(
            "import json\n"
            "import logging\n"
            "\n"
            "logger = logging.getLogger(__name__)\n"
            "\n"
            "def summarize(orders):\n"
            '    logger.info("summarized %s", len(orders))\n'
            "    return len(orders)\n"
        )
        assert all(r.check(ctx) == [] for r in build_py_web_routing_rules())

    def test_fully_guarded_fastapi_file(self, make_ctx):
        ctx = make_ctx(
            "from fastapi import FastAPI, Depends\n"
            "\n"
            "app = FastAPI(dependencies=[Depends(get_current_user)])\n"
            "\n"
            '@app.get("/items")\n'
            "def list_items():\n"
            "    return db.all()\n"
        )
        rules = {r.id: r for r in build_py_web_routing_rules()}
        # 鉴权齐备：无鉴权标疑经文件内 current_user 线索豁免（构造器级
        # dependencies 本身属已知限制，见模块 docstring）
        assert rules["PY-WEB-ROUTE-NO-AUTH"].check(ctx) == []
        # 限流标疑与鉴权无关：该文件确无限流特征，仍按其自身口径报告
        rl_hits = rules["PY-WEB-NO-RATE-LIMIT"].check(ctx)
        assert [h.line_start for h in rl_hits] == [5]


def test_build_py_web_routing_rules():
    rules = build_py_web_routing_rules()
    assert [r.id for r in rules] == ["PY-WEB-ROUTE-NO-AUTH", "PY-WEB-NO-RATE-LIMIT"]
    ids = {r.id: r for r in rules}
    for rule in rules:
        assert rule.languages == ("python",)
        assert rule.category.value == "security"
        assert rule.bad_example.strip() and rule.good_example.strip()
    assert ids["PY-WEB-ROUTE-NO-AUTH"].severity.value == "medium"
    assert ids["PY-WEB-NO-RATE-LIMIT"].severity.value == "low"
