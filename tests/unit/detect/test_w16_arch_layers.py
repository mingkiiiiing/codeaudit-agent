"""W16 分层架构违规规则自测（全离线，直接构造 RuleContext，不依赖索引）。

覆盖：高层目录 import 低层命中；api→service 等中间层不报；低层目录自身不报；
根目录/无分层结构文件不报；js 相对路径与 @/ 别名命中；注释掉的 import 不报；
model/models 不在低层表（防误报红线）。
"""

from __future__ import annotations

import pytest

from audit.detect.base import RuleContext
from audit.detect.rules.arch_layers import (
    JsArchLayerViolationRule,
    PyArchLayerViolationRule,
    build_arch_layer_rules,
)
from audit.models import Category, Severity


def _py_ctx(source: str, rel_path: str) -> RuleContext:
    lines = source.splitlines()
    return RuleContext(rel_path=rel_path, language="python", source=source, lines=lines)


def _js_ctx(source: str, rel_path: str, language: str = "javascript") -> RuleContext:
    lines = source.splitlines()
    return RuleContext(rel_path=rel_path, language=language, source=source, lines=lines)


# ---------------------------------------------------------------- python 规则


def test_api_import_dao_hits() -> None:
    """api/x.py 内 `from dao.user import ...` → PY-LAYER-VIOLATION 命中，行号正确。"""
    source = "from dao.user import get_user\n\n\ndef handle():\n    return get_user(1)\n"
    hits = PyArchLayerViolationRule().check(_py_ctx(source, "api/x.py"))
    assert len(hits) == 1
    hit = hits[0]
    assert hit.rule_id == "PY-LAYER-VIOLATION"
    assert hit.line_start == 1 and hit.line_end == 1
    assert hit.category == Category.STYLE and hit.severity == Severity.MEDIUM
    assert "dao.user" in hit.message
    assert "服务层" in hit.message  # description 的建议口径同步进 message


def test_py_package_prefixed_dao_hits() -> None:
    """`import app.dao.user as d`（次段 dao）与 `from app.repository import x` 均命中。"""
    rule = PyArchLayerViolationRule()
    assert len(rule.check(_py_ctx("import app.dao.user as d\n", "controller/c.py"))) == 1
    assert len(rule.check(_py_ctx("from app.repository import find\n", "handler/h.py"))) == 1


def test_api_import_service_no_hit() -> None:
    """api→services 属层之间依赖（services 不在低层表）→ 不报。"""
    source = "from services.orders import create_order\n\n\ndef handle():\n    return create_order({})\n"
    assert PyArchLayerViolationRule().check(_py_ctx(source, "api/orders.py")) == []


def test_dao_self_reference_no_hit() -> None:
    """文件在低层目录（dao 内部互引）→ 豁免不报。"""
    source = "from dao.role import role_of\n\n\ndef user_role(uid):\n    return role_of(uid)\n"
    assert PyArchLayerViolationRule().check(_py_ctx(source, "dao/user.py")) == []


def test_root_and_no_layer_files_no_hit() -> None:
    """根目录文件不报；无分层结构目录（app/）内的文件不报。"""
    rule = PyArchLayerViolationRule()
    assert rule.check(_py_ctx("from dao import x\n", "main.py")) == []
    assert rule.check(_py_ctx("from dao import x\n", "app/utils.py")) == []


def test_models_not_low_layer_no_hit() -> None:
    """防误报红线：api 层 import models（pydantic/sqlalchemy 模型常态）→ 不报。"""
    source = "from models.user import User\nfrom app.models import Order\n\n\nu = User\n"
    assert PyArchLayerViolationRule().check(_py_ctx(source, "api/users.py")) == []


def test_commented_import_no_hit() -> None:
    """注释掉的 import（行内 # 剥离）→ 不报。"""
    assert PyArchLayerViolationRule().check(_py_ctx("# from dao import x\n", "api/x.py")) == []


@pytest.mark.parametrize(
    "rel_path, import_line",
    [
        ("routes/r.py", "from persistence.session import session_scope"),
        ("views/v.py", "import infra.db"),
        ("presentation/p.py", "from dal.legacy import legacy_query"),
        ("api/a.py", "from ..dao import user_repo"),  # 相对导入剥离前导点后同判
    ],
)
def test_high_low_segment_matrix(rel_path: str, import_line: str) -> None:
    """高层段 × 低层段白名单矩阵：逐组合命中（含相对导入形态）。"""
    hits = PyArchLayerViolationRule().check(_py_ctx(import_line + "\n", rel_path))
    assert len(hits) == 1


# ---------------------------------------------------------------- js/ts 规则


def test_js_relative_db_hits() -> None:
    """views/list.js `import ... from '../db/client'` → JS-LAYER-VIOLATION 命中。"""
    source = "import { connect } from '../db/client';\n\nexport const list = () => connect();\n"
    hits = JsArchLayerViolationRule().check(_js_ctx(source, "views/list.js"))
    assert len(hits) == 1
    hit = hits[0]
    assert hit.rule_id == "JS-LAYER-VIOLATION"
    assert hit.line_start == 1
    assert "../db/client" in hit.message


def test_js_alias_dao_hits() -> None:
    """`@/dao/` 别名命中；`../repository/` 相对路径命中。"""
    rule = JsArchLayerViolationRule()
    assert len(rule.check(_js_ctx("import { findUser } from '@/dao/user';\n", "api/user.js"))) == 1
    assert (
        len(rule.check(_js_ctx("import repo from '../repository/order.js';\n", "views/o.js"))) == 1
    )


def test_js_service_and_comment_no_hit() -> None:
    """js→services 中间层不报；注释掉的 import 不报；同层相对导入不报。"""
    rule = JsArchLayerViolationRule()
    assert rule.check(_js_ctx("import { create } from '../services/orders.js';\n", "api/o.js")) == []
    assert rule.check(_js_ctx("// import x from '../dao/u';\n", "api/x.js")) == []
    assert rule.check(_js_ctx("import { y } from './helpers.js';\n", "api/y.js")) == []


def test_typescript_language_shared() -> None:
    """TS 文件共享同一条 JS 规则（languages 含 typescript），别名形态同样命中。"""
    source = "import { repo } from '@/infrastructure/db';\n\nexport const load = () => repo;\n"
    hits = JsArchLayerViolationRule().check(_js_ctx(source, "controller/user.ts", "typescript"))
    assert len(hits) == 1


# ---------------------------------------------------------------- 构建函数与契约


def test_build_arch_layer_rules() -> None:
    """build_arch_layer_rules 返回 python 与 js/ts 各一条，元信息完整合法。"""
    rules = build_arch_layer_rules()
    assert {type(r) for r in rules} == {PyArchLayerViolationRule, JsArchLayerViolationRule}
    py = next(r for r in rules if r.id == "PY-LAYER-VIOLATION")
    js = next(r for r in rules if r.id == "JS-LAYER-VIOLATION")
    assert py.languages == ("python",)
    assert set(js.languages) == {"javascript", "typescript"}
    for r in rules:
        assert r.category == Category.STYLE and r.severity == Severity.MEDIUM
        assert r.description  # 说明文案非空（含豁免/建议口径）
