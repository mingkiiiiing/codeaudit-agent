"""全局规则注册表：import 即完成全部内置规则的注册。"""

from __future__ import annotations

from audit.detect.base import Rule, RuleRegistry
from audit.detect.rules import (
    build_arch_layer_rules,
    build_cpp_rules,
    build_go_rules,
    build_java_rules,
    build_javascript_rules,
    build_js_security_ext_rules,
    build_pii_rules,
    build_py_naming_rules,
    build_py_taint_rules,
    build_python_rules,
    build_typescript_rules,
)
from audit.detect.rules.js.js_ext import build_js_ext_rules
from audit.detect.rules.py_complexity import build_py_complexity_rules
from audit.detect.rules.py_concurrency import build_py_concurrency_rules
from audit.detect.rules.py_dynamic_exec import build_dynamic_exec_rules
from audit.detect.rules.py_none_deref import build_py_none_deref_rules
from audit.detect.rules.py_orm import build_py_orm_rules
from audit.detect.rules.py_security_ops import build_security_ops_rules
from audit.detect.rules.py_web_routing import build_py_web_routing_rules
from audit.detect.rules.python_ext import build_python_ext_rules

DEFAULT_REGISTRY: RuleRegistry = RuleRegistry()
DEFAULT_REGISTRY.register_all(build_python_rules())
DEFAULT_REGISTRY.register_all(build_javascript_rules() + build_typescript_rules())
# 扩充第二期（W6-A3）：新规则一律收敛在 *_ext.py，追加注册、不动上面既有 49 条
DEFAULT_REGISTRY.register_all(build_python_ext_rules())
DEFAULT_REGISTRY.register_all(build_js_ext_rules())
# 扩充第三期（W16，验收短板清偿）：命名规范 / PII / JS 命令注入 / 分层架构违规
DEFAULT_REGISTRY.register_all(build_py_naming_rules())
DEFAULT_REGISTRY.register_all(build_pii_rules())
DEFAULT_REGISTRY.register_all(build_js_security_ext_rules())
DEFAULT_REGISTRY.register_all(build_arch_layer_rules())
# 扩充第四期（W19，深度审计 P1 清偿）：圈复杂度 / 并发缺陷 / ORM N+1 / 动态执行
DEFAULT_REGISTRY.register_all(build_py_complexity_rules())
DEFAULT_REGISTRY.register_all(build_py_concurrency_rules())
DEFAULT_REGISTRY.register_all(build_py_orm_rules())
DEFAULT_REGISTRY.register_all(build_dynamic_exec_rules())
# 扩充第五期（W20，P2 清偿）：默认凭据字典 / 日志伪造
DEFAULT_REGISTRY.register_all(build_security_ops_rules())
# 扩充第六期（W21，门禁清白配套）：FastAPI/Flask 路由无鉴权 / 无限流标疑
DEFAULT_REGISTRY.register_all(build_py_web_routing_rules())
# P0-3（AST 断供修复配套）：PY-NONE-DEREF——同函数内空值流（AST-only，tree=None 不产命中）
DEFAULT_REGISTRY.register_all(build_py_none_deref_rules())
# W24-A（Java 语言包）：JAVA-SQL-INJECTION / JAVA-HARDCODED-SECRET / JAVA-LONG-FUNCTION
DEFAULT_REGISTRY.register_all(build_java_rules())
# W26-C（Go 语言包）：GO-SQL-INJECTION / GO-HARDCODED-SECRET / GO-LONG-FUNCTION
DEFAULT_REGISTRY.register_all(build_go_rules())
# W28-C（C++ 语言包）：CPP-SQL-INJECTION / CPP-HARDCODED-SECRET / CPP-LONG-FUNCTION
DEFAULT_REGISTRY.register_all(build_cpp_rules())
# W30 卡 A（污点传播）：PY-TAINT-UNSAFE-SINK——函数内污点传播（AST-only，tree=None 不产命中）
DEFAULT_REGISTRY.register_all(build_py_taint_rules())


def get_registry() -> RuleRegistry:
    """返回全局默认注册表；集成方可继续向其注册自定义/扩展规则。"""
    return DEFAULT_REGISTRY


def register_rule(rule: Rule) -> Rule:
    """向默认注册表追加单条规则（便于外部扩展）。"""
    return DEFAULT_REGISTRY.register(rule)
