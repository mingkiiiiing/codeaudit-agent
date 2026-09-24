"""W29 P0-2 safe-rename 作用域增强自测：多定义点「作用域可分则分，分不清则拒」。

全部离线零 LLM、tmp_path 现写源码，不触碰仓库其他目录。覆盖口径：
- self.X：归属所在类（词法最近 class）同名 method 定义点；无则解析 bases
  一层，基类名对上某定义点类段即归属（继承场景，含跨文件基类）；
- 裸名（from m import X 导入项、装饰器、赋值右值、X.foo 的 X 本体）：
  归属模块级 function/class/constant 定义点；
- obj.X / cls.X / self.a.b 等接收者不可静态判定 → 歧义点 → 整体拒绝，
  errors 含 file:line 位置明细与数量；
- 任一歧义 → 全部不替换、apply 不落盘（宁拒不改）；
- 单定义点路径不做归属判定（行为与 W27-C 逐字节一致，含 obj.X 引用点）。
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

from audit.indexer.parsers import parse_source
from audit.refactor.rename import apply_rename, plan_rename

DOGFOOD = Path(__file__).parent / "fixtures" / "rename_dogfood"


# ---------------------------------------------------------------- 夹具工具


def _write_proj(tmp_path: Path, files: dict[str, str]) -> Path:
    """在 tmp_path 下现写一个小型源码项目，返回根目录。"""
    root = tmp_path / "proj"
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def _ident_counts(path: Path) -> dict[str, int]:
    """token 级标识符计数（tree-sitter identifier 节点，天然不含字符串/注释）。"""
    data = path.read_bytes()
    tree, ok = parse_source("python", data)
    assert ok, path
    counts: dict[str, int] = {}
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "identifier":
            text = data[node.start_byte : node.end_byte].decode("utf-8")
            counts[text] = counts.get(text, 0) + 1
        stack.extend(node.children)
    return counts


def _proj_ident_counts(proj: Path) -> dict[str, int]:
    """整个项目所有 .py 的标识符计数总和。"""
    totals: dict[str, int] = {}
    for py in sorted(proj.rglob("*.py")):
        for name, cnt in _ident_counts(py).items():
            totals[name] = totals.get(name, 0) + cnt
    return totals


def _assert_all_files_parse(proj: Path) -> None:
    """全项目 AST 复检：所有 .py 必须 ast.parse 成功。"""
    for py in sorted(proj.rglob("*.py")):
        ast.parse(py.read_bytes())


# ------------------------------------------------- 可归属放行（self.X 形态）


def test_two_classes_same_method_self_calls_succeed(tmp_path: Path) -> None:
    """双类同名方法，各自方法体内 self 调用分别归属本类定义点 → 放行。"""
    root = _write_proj(
        tmp_path,
        {
            "app.py": (
                "class Cat:\n"
                "    def speak(self):\n"
                "        return \"meow\"\n"
                "\n"
                "    def demo(self):\n"
                "        return self.speak()\n"
                "\n"
                "\n"
                "class Dog:\n"
                "    def speak(self):\n"
                "        return \"woof\"\n"
                "\n"
                "    def demo(self):\n"
                "        return self.speak()\n"
            )
        },
    )
    plan = plan_rename(root, "speak", "say")
    assert plan.ok, plan.errors
    assert {d.qualified_name for d in plan.definitions} == {"Cat.speak", "Dog.speak"}
    assert plan.total_replace_points == 4  # 两个定义名 + 两个 self.speak 调用
    result = apply_rename(plan, dry_run=False)
    assert result.ok and result.applied
    _assert_all_files_parse(root)
    # 语义守恒：各自类的 self 调用仍走各自实现
    ns: dict[str, object] = {}
    exec((root / "app.py").read_text(encoding="utf-8"), ns)
    assert ns["Cat"]().demo() == "meow" and ns["Dog"]().demo() == "woof"  # type: ignore[attr-defined]
    counts = _ident_counts(root / "app.py")
    assert counts.get("speak", 0) == 0 and counts.get("say", 0) == 4


def test_inherited_self_call_resolves_to_base(tmp_path: Path) -> None:
    """继承链：Child 无同名方法定义，self 调用经 bases 解析归属基类 → 放行。"""
    root = _write_proj(
        tmp_path,
        {
            "app.py": (
                "class Base:\n"
                "    def handler(self):\n"
                "        return \"base\"\n"
                "\n"
                "\n"
                "class Other:\n"
                "    def handler(self):\n"
                "        return \"other\"\n"
                "\n"
                "\n"
                "class Child(Base):\n"
                "    def go(self):\n"
                "        return self.handler()\n"
            )
        },
    )
    plan = plan_rename(root, "handler", "process")
    assert plan.ok, plan.errors
    assert plan.total_replace_points == 3  # 两个定义名 + Child 的 self.handler
    result = apply_rename(plan, dry_run=False)
    assert result.ok and result.applied
    ns: dict[str, object] = {}
    exec((root / "app.py").read_text(encoding="utf-8"), ns)
    # 归属正确性实证：Child 经继承链仍解析到 Base（改名后）的实现；Other 同步改名且行为不变
    assert ns["Child"]().go() == "base" and ns["Other"]().process() == "other"  # type: ignore[attr-defined]
    assert _ident_counts(root / "app.py").get("handler", 0) == 0


def test_inherited_base_across_files(tmp_path: Path) -> None:
    """跨文件基类：base.Base.handle 定义、child.Dummy.handle 同名构成多定义点，
    Child(Base) 方法内 self.handle 经基类名跨文件解析 → 放行。"""
    root = _write_proj(
        tmp_path,
        {
            "base.py": "class Base:\n    def handle(self):\n        return 1\n",
            "child.py": (
                "from base import Base\n"
                "\n"
                "\n"
                "class Dummy:\n"
                "    def handle(self):\n"
                "        return 2\n"
                "\n"
                "\n"
                "class Child(Base):\n"
                "    def go(self):\n"
                "        return self.handle()\n"
            ),
        },
    )
    plan = plan_rename(root, "handle", "process")
    assert plan.ok, plan.errors
    assert {d.qualified_name for d in plan.definitions} == {"Base.handle", "Dummy.handle"}
    assert plan.total_replace_points == 3
    assert {p.file for p in plan.patches} == {"base.py", "child.py"}
    assert apply_rename(plan, dry_run=False).ok
    _assert_all_files_parse(root)
    child_text = (root / "child.py").read_text(encoding="utf-8")
    assert "return self.process()" in child_text
    assert "from base import Base" in child_text  # 模块路径 base 不动
    assert _proj_ident_counts(root).get("handle", 0) == 0


# ------------------------------------------------- 可归属放行（裸名形态）


def test_bare_call_in_method_body_resolves_module_function(tmp_path: Path) -> None:
    """类方法体内的裸名调用归属模块级函数定义点（不可能是方法调用）→ 放行。"""
    root = _write_proj(
        tmp_path,
        {
            "app.py": (
                "def helper():\n"
                "    return 0\n"
                "\n"
                "\n"
                "class A:\n"
                "    def helper(self):\n"
                "        return 1\n"
                "\n"
                "\n"
                "class B:\n"
                "    def go(self):\n"
                "        return helper()\n"
                "\n"
                "\n"
                "result = helper()\n"
            )
        },
    )
    plan = plan_rename(root, "helper", "util_fn")
    assert plan.ok, plan.errors
    assert {d.kind for d in plan.definitions} == {"function", "method"}
    assert plan.total_replace_points == 4  # 模块级 def + A 的 def + 方法体内裸名 + 模块级调用
    assert apply_rename(plan, dry_run=False).ok
    _assert_all_files_parse(root)
    ns: dict[str, object] = {}
    exec((root / "app.py").read_text(encoding="utf-8"), ns)
    assert ns["result"] == 0  # type: ignore[attr-defined]  # 方法体内裸名仍走模块级实现
    assert _ident_counts(root / "app.py").get("helper", 0) == 0


def test_from_import_item_resolves_module_level(tmp_path: Path) -> None:
    """``from m1 import payload`` 导入项与调用点均为裸名 → 归属模块级定义 → 放行。"""
    root = _write_proj(
        tmp_path,
        {
            "m1.py": "def payload():\n    return 1\n",
            "m2.py": "class Box:\n    def payload(self):\n        return 2\n",
            "app.py": "from m1 import payload\n\nvalue = payload()\n",
        },
    )
    plan = plan_rename(root, "payload", "cargo")
    assert plan.ok, plan.errors
    assert {d.qualified_name for d in plan.definitions} == {"payload", "Box.payload"}
    assert plan.total_replace_points == 4  # 两个 def 名 + app.py 导入项 + 调用
    assert apply_rename(plan, dry_run=False).ok
    _assert_all_files_parse(root)
    app_text = (root / "app.py").read_text(encoding="utf-8")
    assert "from m1 import cargo" in app_text and "value = cargo()" in app_text
    assert "import m1" not in app_text.replace("from m1 import", "")  # 模块路径 m1 不动
    ns: dict[str, object] = {}
    exec((root / "m1.py").read_text(encoding="utf-8"), ns)  # m1 无模块依赖，单文件实证语义
    assert ns["cargo"]() == 1  # type: ignore[attr-defined]


def test_decorator_reference_resolves_module_level(tmp_path: Path) -> None:
    """装饰器引用 ``@deco`` 是裸名 → 归属模块级函数定义点 → 放行。"""
    root = _write_proj(
        tmp_path,
        {
            "app.py": (
                "def deco(fn):\n"
                "    return fn\n"
                "\n"
                "\n"
                "class A:\n"
                "    def deco(self):\n"
                "        return None\n"
                "\n"
                "\n"
                "@deco\n"
                "def target():\n"
                "    return 1\n"
            )
        },
    )
    plan = plan_rename(root, "deco", "wrap_with")
    assert plan.ok, plan.errors
    assert plan.total_replace_points == 3  # 两个 def 名 + 装饰器引用
    assert apply_rename(plan, dry_run=False).ok
    assert "@wrap_with" in (root / "app.py").read_text(encoding="utf-8")
    _assert_all_files_parse(root)


def test_attribute_object_position_is_bare_name(tmp_path: Path) -> None:
    """``thing.marker`` 中处于 object 位的 thing 本体是裸名（非属性引用）→ 放行。"""
    root = _write_proj(
        tmp_path,
        {
            "app.py": (
                "def thing():\n"
                "    return 0\n"
                "\n"
                "\n"
                "class A:\n"
                "    def thing(self):\n"
                "        return 1\n"
                "\n"
                "\n"
                "thing.marker = True\n"
            )
        },
    )
    plan = plan_rename(root, "thing", "widget")
    assert plan.ok, plan.errors
    assert plan.total_replace_points == 3  # 两个 def 名 + thing.marker 的 thing 本体
    assert apply_rename(plan, dry_run=False).ok
    assert "widget.marker = True" in (root / "app.py").read_text(encoding="utf-8")
    assert _ident_counts(root / "app.py").get("thing", 0) == 0


# ------------------------------------------------- 歧义整体拒绝


def test_obj_prefix_rejected_with_positions(tmp_path: Path) -> None:
    """obj.X 形态（接收者类型不可静态判定）→ 整体拒绝，文案含数量与 file:line。"""
    root = _write_proj(
        tmp_path,
        {
            "app.py": (
                "class Alpha:\n"
                "    def run(self):\n"
                "        return 1\n"
                "\n"
                "\n"
                "class Beta:\n"
                "    def run(self):\n"
                "        return 2\n"
                "\n"
                "\n"
                "alpha = Alpha()\n"
                "alpha.run()\n"
                "beta = Beta()\n"
                "beta.run()\n"
            )
        },
    )
    plan = plan_rename(root, "run", "execute")
    assert not plan.ok and plan.patches == []
    assert len(plan.definitions) == 2
    message = next(e for e in plan.errors if "宁拒不改" in e)
    assert "检测到 2 个同名定义点" in message
    assert "其中 2 个引用点作用域归属不明" in message
    assert "app.py:12(" in message and "app.py:14(" in message  # 歧义点位置明细
    assert "alpha.run 形态" in message and "beta.run 形态" in message
    assert "接收者类型不可静态判定" in message
    result = apply_rename(plan, dry_run=False)
    assert not result.ok and not result.applied and result.written_files == []
    assert _ident_counts(root / "app.py").get("run", 0) == 4  # 磁盘零改动（token 级）


def test_self_call_without_class_def_rejected(tmp_path: Path) -> None:
    """self 调用但所在类（无基类）无同名定义 → 歧义拒绝，模块级定义不救 self.X。"""
    root = _write_proj(
        tmp_path,
        {
            "app.py": (
                "def run():\n"
                "    return 0\n"
                "\n"
                "\n"
                "class Other:\n"
                "    def run(self):\n"
                "        return 1\n"
                "\n"
                "\n"
                "class Ghost:\n"
                "    def go(self):\n"
                "        return self.run()\n"
            )
        },
    )
    plan = plan_rename(root, "run", "execute")
    assert not plan.ok and plan.patches == []
    message = next(e for e in plan.errors if "宁拒不改" in e)
    assert "其中 1 个引用点作用域归属不明" in message
    assert "app.py:12(" in message
    assert "self.run 所在类 Ghost 及其基类均无同名定义点" in message


def test_self_call_base_not_resolvable_rejected(tmp_path: Path) -> None:
    """有基类但基类名对不上任何定义点类段 → 歧义拒绝。"""
    root = _write_proj(
        tmp_path,
        {
            "app.py": (
                "class A:\n"
                "    def handle(self):\n"
                "        return 1\n"
                "\n"
                "\n"
                "class B:\n"
                "    def handle(self):\n"
                "        return 2\n"
                "\n"
                "\n"
                "class Unrelated:\n"
                "    def keep(self):\n"
                "        return 0\n"
                "\n"
                "\n"
                "class Child(Unrelated):\n"
                "    def go(self):\n"
                "        return self.handle()\n"
            )
        },
    )
    plan = plan_rename(root, "handle", "process")
    assert not plan.ok and plan.patches == []
    message = next(e for e in plan.errors if "宁拒不改" in e)
    assert "app.py:18(" in message
    assert "self.handle 所在类 Child 及其基类均无同名定义点" in message


def test_mixed_attributable_and_ambiguous_rejected(tmp_path: Path) -> None:
    """混合场景：可归属的 self 调用 + 一个 obj.X 歧义点 → 整体拒绝（部分可分不放行）。"""
    root = _write_proj(
        tmp_path,
        {
            "app.py": (
                "class A:\n"
                "    def handle(self):\n"
                "        return 1\n"
                "\n"
                "\n"
                "class B:\n"
                "    def handle(self):\n"
                "        return 2\n"
                "\n"
                "\n"
                "class WithSelf:\n"
                "    def handle(self):\n"
                "        return 3\n"
                "\n"
                "    def go(self):\n"
                "        return self.handle()\n"
                "\n"
                "\n"
                "obj = A()\n"
                "obj.handle()\n"
            )
        },
    )
    plan = plan_rename(root, "handle", "process")
    assert not plan.ok and plan.patches == []
    assert len(plan.definitions) == 3
    message = next(e for e in plan.errors if "宁拒不改" in e)
    assert "检测到 3 个同名定义点" in message
    assert "其中 1 个引用点作用域归属不明" in message  # 仅 obj.handle 歧义
    assert "app.py:20(" in message
    # self.handle（第 16 行）可归属，但整体仍拒绝
    assert "app.py:16(" not in message


def test_chained_attribute_object_rejected(tmp_path: Path) -> None:
    """``self.a.b()`` 的 b：接收者是 self.a（属性链，非 self identifier）→ 歧义拒绝。"""
    root = _write_proj(
        tmp_path,
        {
            "app.py": (
                "class Part:\n"
                "    def b(self):\n"
                "        return 1\n"
                "\n"
                "\n"
                "class Other:\n"
                "    def b(self):\n"
                "        return 2\n"
                "\n"
                "\n"
                "class Holder:\n"
                "    def __init__(self):\n"
                "        self.a = Part()\n"
                "\n"
                "    def go(self):\n"
                "        return self.a.b()\n"
            )
        },
    )
    plan = plan_rename(root, "b", "compute")
    assert not plan.ok and plan.patches == []
    message = next(e for e in plan.errors if "宁拒不改" in e)
    assert "app.py:16(" in message
    assert "self.a.b 形态" in message  # 接收者原文进入文案


# ------------------------------------------------- 回归与链路


def test_single_definition_obj_prefix_still_succeeds(tmp_path: Path) -> None:
    """单定义点回归锁定：即便引用点含 obj.X 形态也不做归属判定，行为不变。"""
    root = _write_proj(
        tmp_path,
        {
            "app.py": (
                "class Repo:\n"
                "    def load(self):\n"
                "        return 1\n"
                "\n"
                "\n"
                "r = Repo()\n"
                "r.load()\n"
            )
        },
    )
    plan = plan_rename(root, "load", "fetch")
    assert plan.ok, plan.errors  # 单定义路径：无归属判定，r.load 不构成歧义
    assert len(plan.definitions) == 1
    assert plan.total_replace_points == 2  # 定义名 + r.load 属性位
    assert apply_rename(plan, dry_run=False).ok
    assert _ident_counts(root / "app.py").get("load", 0) == 0


def test_multi_def_attributable_full_pipeline(tmp_path: Path) -> None:
    """多定义可归属全链路：plan → dry_run 不落盘 → apply → 再 plan 幂等。"""
    root = _write_proj(
        tmp_path,
        {
            "app.py": (
                "class Cat:\n"
                "    def speak(self):\n"
                "        return \"meow\"\n"
                "\n"
                "    def demo(self):\n"
                "        return self.speak()\n"
                "\n"
                "\n"
                "class Dog:\n"
                "    def speak(self):\n"
                "        return \"woof\"\n"
                "\n"
                "    def demo(self):\n"
                "        return self.speak()\n"
            )
        },
    )
    snapshots = (root / "app.py").read_bytes()
    plan = plan_rename(root, "speak", "say")
    assert plan.ok
    dry = apply_rename(plan, dry_run=True)
    assert dry.ok and not dry.applied
    assert (root / "app.py").read_bytes() == snapshots  # dry_run 零落盘
    result = apply_rename(plan, dry_run=False)
    assert result.ok and result.applied and result.written_files == ["app.py"]
    plan2 = plan_rename(root, "speak", "say")
    assert not plan2.ok and plan2.patches == []
    assert any("未找到" in e for e in plan2.errors)


def test_scope_rejection_writes_nothing(tmp_path: Path) -> None:
    """歧义拒绝后 apply_rename(dry_run=False) 不落盘，磁盘字节原样。"""
    root = _write_proj(
        tmp_path,
        {
            "app.py": (
                "class Alpha:\n"
                "    def run(self):\n"
                "        return 1\n"
                "\n"
                "\n"
                "class Beta:\n"
                "    def run(self):\n"
                "        return 2\n"
                "\n"
                "\n"
                "alpha = Alpha()\n"
                "alpha.run()\n"
            )
        },
    )
    snapshot = (root / "app.py").read_bytes()
    plan = plan_rename(root, "run", "execute")
    assert not plan.ok
    result = apply_rename(plan, dry_run=False)
    assert not result.ok and not result.applied and result.written_files == []
    assert (root / "app.py").read_bytes() == snapshot
    _assert_all_files_parse(root)


def test_same_name_parameter_is_bare_name_conservative_reject(tmp_path: Path) -> None:
    """边界锁定：与目标同名的函数参数按裸名口径处理（绑定不做区分），
    无模块级定义时保守误拒（宁拒不改）。"""
    root = _write_proj(
        tmp_path,
        {
            "app.py": (
                "class Cat:\n"
                "    def speak(self):\n"
                "        return \"meow\"\n"
                "\n"
                "\n"
                "class Dog:\n"
                "    def speak(self):\n"
                "        return \"woof\"\n"
                "\n"
                "\n"
                "def noise(speak):\n"
                "    return speak\n"
            )
        },
    )
    plan = plan_rename(root, "speak", "say")
    assert not plan.ok and plan.patches == []
    message = next(e for e in plan.errors if "宁拒不改" in e)
    assert "其中 2 个引用点作用域归属不明" in message  # 参数名 + 函数体内引用
    assert "裸名引用但定义点集合中无模块级 function/class/constant 定义点" in message


def test_dogfood_validator_ambiguous_rejected_end_to_end(tmp_path: Path) -> None:
    """dogfood 跨文件同名类（Validator ×2）端到端：models.Validator 引用歧义 →
    拒绝且 apply(dry_run=False) 零落盘，与 W28 拒绝行为等价、文案升级含位置。"""
    proj = tmp_path / "dogfood"
    shutil.copytree(DOGFOOD, proj)
    snapshots = {py: py.read_bytes() for py in sorted(proj.rglob("*.py"))}
    plan = plan_rename(proj, "Validator", "Checker")
    assert not plan.ok
    assert plan.patches == []
    assert {f"{d.file}:{d.qualified_name}" for d in plan.definitions} == {
        "models.py:Validator",
        "validation.py:Validator",
    }
    result = apply_rename(plan, dry_run=False)
    assert not result.ok and not result.applied and result.written_files == []
    for py, snap in snapshots.items():  # 副本零落盘
        assert py.read_bytes() == snap
    _assert_all_files_parse(proj)
