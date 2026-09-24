"""W30 safe-rename 类型推断增强自测：obj.X 直接构造 / super().X / 基类链传递闭包。

全部离线零 LLM、tmp_path 现写源码，不触碰仓库其他目录。三项放宽口径：
- obj.X（放宽①）：仅当 obj 引用点位于函数体内、且其之前存在
  ``obj = ClassName(...)`` 直接类名构造赋值、ClassName 对上某定义点类段
  才放行；模块级构造（W29 拒绝语料形态，语义回归锁定）、别名构造、
  工厂函数构造、引用先于构造一律仍歧义拒绝；
- super().X（放宽②）：仅认零参裸 ``super()``，沿所在类基类链（传递闭包）
  解析，链无定义 / 不可解析 / 所在类无 bases → 仍拒绝；
- 基类链（放宽③）：self.X 的 bases 解析从一层放宽为递归闭包，继承环按
  不可解析处理（不挂死、拒绝）；
安全边界不变：任一歧义点整体拒绝、拒绝后零落盘；单定义路径不做归属判定。
"""

from __future__ import annotations

import ast
from pathlib import Path

from audit.indexer.parsers import parse_source
from audit.refactor.rename import apply_rename, plan_rename


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


def _assert_all_files_parse(proj: Path) -> None:
    """全项目 AST 复检：所有 .py 必须 ast.parse 成功。"""
    for py in sorted(proj.rglob("*.py")):
        ast.parse(py.read_bytes())


def _rejection_message(plan) -> str:
    """提取「宁拒不改」主拒绝文案（多定义点归属判定产物）。"""
    assert not plan.ok and plan.patches == []
    return next(e for e in plan.errors if "宁拒不改" in e)


# ------------------------------------------------- 放宽①：obj.X 直接构造推断


def test_obj_direct_construction_in_function_resolves(tmp_path: Path) -> None:
    """函数体内 ``obj = Cart(); obj.add_item()`` 经直接构造推断归属 Cart → 放行。"""
    root = _write_proj(
        tmp_path,
        {
            "app.py": (
                "class Cart:\n"
                "    def add_item(self, sku):\n"
                "        return f\"cart:{sku}\"\n"
                "\n"
                "\n"
                "class Order:\n"
                "    def add_item(self, sku):\n"
                "        return f\"order:{sku}\"\n"
                "\n"
                "\n"
                "def checkout(sku):\n"
                "    obj = Cart()\n"
                "    return obj.add_item(sku)\n"
            )
        },
    )
    plan = plan_rename(root, "add_item", "append_item")
    assert plan.ok, plan.errors
    assert {d.qualified_name for d in plan.definitions} == {"Cart.add_item", "Order.add_item"}
    assert plan.total_replace_points == 3  # 两个定义名 + checkout 内 obj.add_item 属性位
    result = apply_rename(plan, dry_run=False)
    assert result.ok and result.applied
    _assert_all_files_parse(root)
    ns: dict[str, object] = {}
    exec((root / "app.py").read_text(encoding="utf-8"), ns)
    # 语义守恒：obj 经构造推断确实走 Cart 实现；Order 同步改名且行为不变
    assert ns["checkout"]("SKU9") == "cart:SKU9"  # type: ignore[attr-defined]
    assert ns["Order"]().append_item("K") == "order:K"  # type: ignore[attr-defined]
    counts = _ident_counts(root / "app.py")
    assert counts.get("add_item", 0) == 0 and counts.get("append_item", 0) == 3


def test_obj_construction_class_without_matching_def_rejected(tmp_path: Path) -> None:
    """函数体内构造的类（Ghost）无同名方法定义点 → 构造类名对不上定义点类段 → 拒绝。"""
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
                "class Ghost:\n"
                "    def walk(self):\n"
                "        return 0\n"
                "\n"
                "\n"
                "def main():\n"
                "    obj = Ghost()\n"
                "    return obj.run()\n"
            )
        },
    )
    plan = plan_rename(root, "run", "execute")
    message = _rejection_message(plan)
    assert "其中 1 个引用点作用域归属不明" in message
    assert "app.py:18(" in message
    assert "obj.run 形态" in message
    result = apply_rename(plan, dry_run=False)
    assert not result.ok and result.written_files == []
    assert _ident_counts(root / "app.py").get("run", 0) == 3  # 磁盘零改动（两 def 名 + 一处调用）


def test_obj_factory_construction_rejected(tmp_path: Path) -> None:
    """边界申报：工厂函数构造 ``obj = make_alpha(); obj.run()`` 不做返回值推断 → 拒绝。"""
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
                "def make_alpha():\n"
                "    return Alpha()\n"
                "\n"
                "\n"
                "def main():\n"
                "    obj = make_alpha()\n"
                "    return obj.run()\n"
            )
        },
    )
    plan = plan_rename(root, "run", "execute")
    message = _rejection_message(plan)
    assert "其中 1 个引用点作用域归属不明" in message
    assert "app.py:17(" in message
    assert "obj.run 形态（接收者类型不可静态判定）" in message


def test_obj_alias_class_construction_rejected(tmp_path: Path) -> None:
    """边界申报：别名构造 ``Alias = Alpha`` 后 ``obj = Alias(); obj.run()`` → 拒绝。"""
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
                "AlphaAlias = Alpha\n"
                "\n"
                "\n"
                "def main():\n"
                "    obj = AlphaAlias()\n"
                "    return obj.run()\n"
            )
        },
    )
    plan = plan_rename(root, "run", "execute")
    message = _rejection_message(plan)
    assert "其中 1 个引用点作用域归属不明" in message
    assert "app.py:16(" in message


def test_obj_reference_before_construction_rejected(tmp_path: Path) -> None:
    """「向上回溯」方向性：引用点先于构造赋值（同函数体内）→ 该引用不可归属 → 拒绝。

    同函数体内构造之后的引用点（第 15 行）可归属，但整体仍拒绝（宁拒不改）。
    """
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
                "def main(flag):\n"
                "    if flag:\n"
                "        return obj.run()\n"
                "    obj = Alpha()\n"
                "    return obj.run()\n"
            )
        },
    )
    plan = plan_rename(root, "run", "execute")
    message = _rejection_message(plan)
    assert "其中 1 个引用点作用域归属不明" in message  # 仅第 13 行（先于构造）歧义
    assert "app.py:13(" in message
    assert "app.py:15(" not in message


def test_w29_module_level_construction_semantics_locked(tmp_path: Path) -> None:
    """W29 语义回归锁：模块级 ``alpha = Alpha(); alpha.run()`` 直接构造**不回溯**，
    仍整体拒绝且文案与 W29 逐字一致（放宽①严格限定函数体内，不得翻绿）。"""
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
    message = _rejection_message(plan)
    assert "检测到 2 个同名定义点" in message
    assert "其中 2 个引用点作用域归属不明" in message
    assert "app.py:12(" in message and "app.py:14(" in message
    assert "alpha.run 形态" in message and "beta.run 形态" in message
    assert "接收者类型不可静态判定" in message
    result = apply_rename(plan, dry_run=False)
    assert not result.ok and not result.applied and result.written_files == []
    assert _ident_counts(root / "app.py").get("run", 0) == 4  # 磁盘零改动（token 级）


# ------------------------------------------------- 放宽②：super().X 基类链解析


def test_super_call_resolves_direct_base(tmp_path: Path) -> None:
    """``super().handler()`` 沿所在类 bases 命中基类定义点 → 放行（含语义守恒）。"""
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
                "    def handler(self):\n"
                "        return \"child+\" + super().handler()\n"
                "\n"
                "    def go(self):\n"
                "        return self.handler()\n"
            )
        },
    )
    plan = plan_rename(root, "handler", "process")
    assert plan.ok, plan.errors
    assert plan.total_replace_points == 5  # 三个定义名 + super().handler + self.handler
    result = apply_rename(plan, dry_run=False)
    assert result.ok and result.applied
    _assert_all_files_parse(root)
    ns: dict[str, object] = {}
    exec((root / "app.py").read_text(encoding="utf-8"), ns)
    # super().process() 解析到基类实现；self.process() 走 Child 自身实现
    assert ns["Child"]().go() == "child+base"  # type: ignore[attr-defined]
    assert ns["Other"]().process() == "other"  # type: ignore[attr-defined]
    counts = _ident_counts(root / "app.py")
    assert counts.get("handler", 0) == 0 and counts.get("process", 0) == 5


def test_super_call_base_without_definition_rejected(tmp_path: Path) -> None:
    """super().X 所在类基类链上无同名定义点（基类只有其他方法）→ 歧义拒绝。"""
    root = _write_proj(
        tmp_path,
        {
            "app.py": (
                "class Base:\n"
                "    def setup(self):\n"
                "        return 0\n"
                "\n"
                "\n"
                "class Unrelated:\n"
                "    def handler(self):\n"
                "        return 1\n"
                "\n"
                "\n"
                "class Other:\n"
                "    def handler(self):\n"
                "        return 2\n"
                "\n"
                "\n"
                "class Child(Base):\n"
                "    def go(self):\n"
                "        return super().handler()\n"
            )
        },
    )
    plan = plan_rename(root, "handler", "process")
    message = _rejection_message(plan)
    assert "其中 1 个引用点作用域归属不明" in message
    assert "app.py:18(" in message
    assert "super.handler 所在类 Child 的基类链均无同名定义点或不可解析" in message


def test_super_call_without_bases_rejected(tmp_path: Path) -> None:
    """所在类无任何 bases 时 super().X 解析不出基类名 → 歧义拒绝。"""
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
                "class Orphan:\n"
                "    def go(self):\n"
                "        return super().run()\n"
            )
        },
    )
    plan = plan_rename(root, "run", "execute")
    message = _rejection_message(plan)
    assert "app.py:13(" in message
    assert "super.run 所在类 Orphan 的基类链均无同名定义点或不可解析" in message


# ------------------------------------------------- 放宽③：基类链传递闭包


def test_self_attr_two_level_inheritance_chain(tmp_path: Path) -> None:
    """两层继承链 Child(Mid)→Mid(Base)：self.handle 经传递闭包命中 Base 定义 → 放行。"""
    root = _write_proj(
        tmp_path,
        {
            "app.py": (
                "class Base:\n"
                "    def handle(self):\n"
                "        return \"base\"\n"
                "\n"
                "\n"
                "class Mid(Base):\n"
                "    pass\n"
                "\n"
                "\n"
                "class Other:\n"
                "    def handle(self):\n"
                "        return \"other\"\n"
                "\n"
                "\n"
                "class Child(Mid):\n"
                "    def go(self):\n"
                "        return self.handle()\n"
            )
        },
    )
    plan = plan_rename(root, "handle", "process")
    assert plan.ok, plan.errors  # W29 一层口径会拒（Mid 无定义、Base 超出一层），W30 放行
    assert plan.total_replace_points == 3  # 两个定义名 + Child 的 self.handle
    result = apply_rename(plan, dry_run=False)
    assert result.ok and result.applied
    _assert_all_files_parse(root)
    ns: dict[str, object] = {}
    exec((root / "app.py").read_text(encoding="utf-8"), ns)
    assert ns["Child"]().go() == "base"  # type: ignore[attr-defined]
    assert ns["Other"]().process() == "other"  # type: ignore[attr-defined]
    assert _ident_counts(root / "app.py").get("handle", 0) == 0


def test_super_call_two_level_chain(tmp_path: Path) -> None:
    """super().handle 走两层基类链 Child(Mid)→Mid(Base) 命中 Base 定义 → 放行。"""
    root = _write_proj(
        tmp_path,
        {
            "app.py": (
                "class Base:\n"
                "    def handle(self):\n"
                "        return \"base\"\n"
                "\n"
                "\n"
                "class Mid(Base):\n"
                "    pass\n"
                "\n"
                "\n"
                "class Other:\n"
                "    def handle(self):\n"
                "        return \"other\"\n"
                "\n"
                "\n"
                "class Child(Mid):\n"
                "    def go(self):\n"
                "        return \"child:\" + super().handle()\n"
            )
        },
    )
    plan = plan_rename(root, "handle", "process")
    assert plan.ok, plan.errors
    assert plan.total_replace_points == 3
    assert apply_rename(plan, dry_run=False).ok
    _assert_all_files_parse(root)
    ns: dict[str, object] = {}
    exec((root / "app.py").read_text(encoding="utf-8"), ns)
    assert ns["Child"]().go() == "child:base"  # type: ignore[attr-defined]
    assert _ident_counts(root / "app.py").get("handle", 0) == 0


def test_inheritance_cycle_rejects_and_terminates(tmp_path: Path) -> None:
    """继承环 LoopA(LoopB) ↔ LoopB(LoopA)：链解析不挂死，按不可解析处理 → 拒绝。"""
    root = _write_proj(
        tmp_path,
        {
            "app.py": (
                "class Alpha:\n"
                "    def handle(self):\n"
                "        return 1\n"
                "\n"
                "\n"
                "class Other:\n"
                "    def handle(self):\n"
                "        return 2\n"
                "\n"
                "\n"
                "class LoopA(LoopB):\n"
                "    def go(self):\n"
                "        return self.handle()\n"
                "\n"
                "\n"
                "class LoopB(LoopA):\n"
                "    def stay(self):\n"
                "        return 0\n"
            )
        },
    )
    plan = plan_rename(root, "handle", "process")  # 若实现挂死此调用不返回
    message = _rejection_message(plan)
    assert "app.py:13(" in message
    assert "self.handle 所在类 LoopA 及其基类均无同名定义点" in message
    assert apply_rename(plan, dry_run=False).written_files == []
    assert _ident_counts(root / "app.py").get("handle", 0) == 3  # 磁盘零改动（两 def 名 + 一处调用）


# ------------------------------------------------- 单定义路径回归


def test_single_definition_with_construction_unchecked(tmp_path: Path) -> None:
    """单定义回归锁定：函数体内 ``obj = Repo(); obj.load()`` 不做归属判定，行为不变。"""
    root = _write_proj(
        tmp_path,
        {
            "app.py": (
                "class Repo:\n"
                "    def load(self):\n"
                "        return 1\n"
                "\n"
                "\n"
                "def boot():\n"
                "    obj = Repo()\n"
                "    return obj.load()\n"
            )
        },
    )
    plan = plan_rename(root, "load", "fetch")
    assert plan.ok, plan.errors
    assert len(plan.definitions) == 1
    assert plan.total_replace_points == 2  # 定义名 + obj.load 属性位（构造类名 Repo 不参与）
    result = apply_rename(plan, dry_run=False)
    assert result.ok and result.applied
    _assert_all_files_parse(root)
    ns: dict[str, object] = {}
    exec((root / "app.py").read_text(encoding="utf-8"), ns)
    assert ns["boot"]() == 1  # type: ignore[attr-defined]
    counts = _ident_counts(root / "app.py")
    assert counts.get("load", 0) == 0 and counts.get("fetch", 0) == 2
