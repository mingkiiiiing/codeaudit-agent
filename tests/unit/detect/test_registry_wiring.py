"""注册表接线完整性守卫（P0-13，第十轮审计 F10-R1 防再犯）。

W30 半收口教训：py_taint.py 已实现、``build_py_taint_rules`` 已被 registry.py
import，但漏了 ``register_all`` 调用——规则类直测全绿而 CLI/server/CI/前端
主链路零命中（92≠93），靠 ruff F401 + CI 看门测试事后才红。

本守卫把「接线」口径前移到接入期：**audit/detect/rules 包树内任何
``build_*_rules`` 工厂产出的规则，都必须已在 DEFAULT_REGISTRY 注册**。
工厂清单经 pkgutil 动态遍历（含 js 子包），新增规则文件只要定义了工厂函数
即自动落入覆盖，无需维护手工清单。
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re

import audit.detect.rules as rules_pkg
from audit.detect.base import RuleRegistry
from audit.detect.registry import DEFAULT_REGISTRY

_BUILDER_RE = re.compile(r"^build_\w+_rules$")


def _iter_builders():
    """包树内全部 build_*_rules 工厂：(定义模块名, 函数名, 函数)，按归属模块去重。"""
    seen: set[tuple[str, str]] = set()
    for mod_info in pkgutil.walk_packages(rules_pkg.__path__, prefix=rules_pkg.__name__ + "."):
        if mod_info.ispkg:
            continue
        module = importlib.import_module(mod_info.name)
        for name, obj in vars(module).items():
            if (
                _BUILDER_RE.match(name)
                and inspect.isfunction(obj)
                and obj.__module__ == mod_info.name
                and (mod_info.name, name) not in seen
            ):
                seen.add((mod_info.name, name))
                yield mod_info.name, name, obj


def test_rules_package_builders_all_registered():
    builders = list(_iter_builders())
    # 覆盖面自健康下限（当前 20 个工厂：rules/__init__ 11 + registry 直引子模块 9）；
    # 若未来拆分模块导致数量骤降，说明遍历口径失效，守卫自身先红。
    assert len(builders) >= 15, f"工厂遍历仅发现 {len(builders)} 个，遍历口径疑似失效"
    registered_ids = {r.id for r in DEFAULT_REGISTRY.all_rules}
    for mod_name, func_name, builder in builders:
        isolated = RuleRegistry()
        isolated.register_all(builder())
        assert isolated.all_rules, f"{mod_name}.{func_name} 未产出任何规则"
        for rule in isolated.all_rules:
            assert rule.id in registered_ids, (
                f"{mod_name}.{func_name} 产出的 {rule.id} 未在 DEFAULT_REGISTRY 注册"
                "（import 了但没接线——F10-R1 形态，见 docs/29）"
            )
