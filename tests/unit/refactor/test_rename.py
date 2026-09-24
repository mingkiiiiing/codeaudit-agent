"""W27-C safe-rename 纯模块自测：plan/apply 两段式、误替换防护、all-or-nothing、dogfood 门。

全部离线零 LLM：
- 小型用例在 tmp_path 现写源码（单文件/跨目录），不触碰仓库其他目录；
- dogfood 语料在 tests/unit/refactor/fixtures/rename_dogfood/（4 文件小型
  python 项目，20 个重命名目标 + 字符串/注释/子串陷阱 + 跨文件同名类
  歧义语料（Validator ×2 + models.Validator 属性链引用 → 拒绝含位置明细）），
  每个目标复制到独立临时副本上执行 dry_run→apply→再 plan 幂等链路。
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

from audit.indexer.parsers import parse_source
from audit.refactor.rename import RenamePlan, apply_rename, plan_rename

DOGFOOD = Path(__file__).parent / "fixtures" / "rename_dogfood"

# 20 个重命名目标：函数/方法/类/模块级常量 × 单文件/跨文件引用
TARGETS: list[tuple[str, str]] = [
    ("calc_total", "calc_sum"),
    ("format_label", "render_label"),
    ("MAX_RETRY", "MAX_ATTEMPTS"),
    ("DEFAULT_TIMEOUT", "DEFAULT_TTL"),
    ("build_query", "make_query"),
    ("parse_int", "to_int"),
    ("clamp_value", "bound_value"),
    ("join_names", "merge_names"),
    ("UserRecord", "AccountRecord"),
    ("OrderLine", "CartLine"),
    ("to_display", "render_inline"),
    ("net_amount", "final_amount"),
    ("TAX_RATE", "VAT_RATE"),
    ("OrderService", "CheckoutService"),
    ("checkout", "place_order"),
    ("audit_log", "write_audit"),
    ("notify_user", "send_notice"),
    ("SERVICE_NAME", "SERVICE_LABEL"),
    ("resolve_customer", "lookup_customer"),
    ("apply_discount", "apply_markdown"),
]

# 文本残留陷阱：重命名后旧名必须仍留在字符串/注释里（证明 token 级不越界）
TEXT_TRAPS: dict[str, str] = {
    "calc_total": "util.py",  # docstring
    "MAX_RETRY": "util.py",  # 注释 + docstring
    "UserRecord": "models.py",  # docstring
    "OrderService": "service.py",  # docstring
    "checkout": "service.py",  # docstring + 字面量 "checkout"
    "audit_log": "service.py",  # docstring
    "SERVICE_NAME": "service.py",  # 注释
    "notify_user": "service.py",  # docstring
    "apply_discount": "validation.py",  # docstring
}

# 标识符陷阱：相邻不同符号重命名后必须原样保留（名 -> 期望出现次数）
IDENT_TRAPS: dict[str, tuple[str, int]] = {
    "MAX_RETRY": ("MAX_RETRY_LIMIT", 1),
    "notify_user": ("username", 2),
}


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
    """①每文件 AST 复检：项目内全部 .py 必须 ast.parse 成功。"""
    for py in sorted(proj.rglob("*.py")):
        ast.parse(py.read_bytes())  # 失败即抛 SyntaxError 使断言失败


# ---------------------------------------------------------------- 单文件基础


def test_plan_single_file_function_rename(tmp_path: Path) -> None:
    root = _write_proj(tmp_path, {"app.py": 'def greet(name):\n    return "hi " + name\n\nmsg = greet("bob")\nother = greet\n'})
    plan = plan_rename(root, "greet", "welcome")
    assert plan.ok, plan.errors
    assert len(plan.definitions) == 1
    assert plan.definitions[0].kind == "function"
    assert plan.total_replace_points == 3  # 定义 + 调用 + 赋值引用
    patch = plan.patches[0]
    assert patch.file == "app.py"
    assert len(patch.replace_points) == 3
    assert "+def welcome" in patch.unified_diff and "-def greet" in patch.unified_diff
    counts = _ident_counts(root / "app.py")
    assert counts["greet"] == 3 and "welcome" not in counts  # plan 不落盘


def test_apply_dry_run_does_not_write(tmp_path: Path) -> None:
    source = "def greet(name):\n    return name\n\nmsg = greet('bob')\n"
    root = _write_proj(tmp_path, {"app.py": source})
    plan = plan_rename(root, "greet", "welcome")
    result = apply_rename(plan, dry_run=True)
    assert result.ok and not result.applied
    assert result.written_files == []
    assert set(result.diffs) == {"app.py"}
    assert (root / "app.py").read_text(encoding="utf-8") == source  # 磁盘原样


def test_apply_writes_files_and_ast_ok(tmp_path: Path) -> None:
    root = _write_proj(tmp_path, {"app.py": "def greet(name):\n    return name\n\nmsg = greet('bob')\n"})
    plan = plan_rename(root, "greet", "welcome")
    result = apply_rename(plan, dry_run=False)
    assert result.ok and result.applied
    assert result.written_files == ["app.py"]
    new_text = (root / "app.py").read_text(encoding="utf-8")
    ast.parse(new_text)  # ①AST 复检
    assert "greet" not in new_text and new_text.count("welcome") == 2  # 定义 + 调用


def test_constant_rename(tmp_path: Path) -> None:
    root = _write_proj(tmp_path, {"conf.py": "MAX_JOBS = 4\nworkers = MAX_JOBS * 2\n"})
    plan = plan_rename(root, "MAX_JOBS", "MAX_WORKERS")
    assert plan.ok, plan.errors
    assert plan.definitions[0].kind == "constant"
    assert plan.total_replace_points == 2
    result = apply_rename(plan, dry_run=False)
    assert result.ok
    assert (root / "conf.py").read_text(encoding="utf-8") == "MAX_WORKERS = 4\nworkers = MAX_WORKERS * 2\n"


def test_second_plan_after_apply_finds_nothing(tmp_path: Path) -> None:
    root = _write_proj(tmp_path, {"app.py": "def solo():\n    return solo()\n"})
    plan = plan_rename(root, "solo", "unique")
    assert apply_rename(plan, dry_run=False).ok
    plan2 = plan_rename(root, "solo", "unique")  # ④幂等：第二次 plan 找不到旧名
    assert not plan2.ok
    assert plan2.patches == []
    assert any("未找到" in e for e in plan2.errors)


# ---------------------------------------------------------------- 跨文件 / 方法


def test_cross_file_rename_updates_import_sites(tmp_path: Path) -> None:
    root = _write_proj(
        tmp_path,
        {
            "util.py": "def calc_total(items):\n    return sum(items)\n",
            "app.py": "from util import calc_total\n\ntotal = calc_total([1, 2])\n",
        },
    )
    plan = plan_rename(root, "calc_total", "calc_sum")
    assert plan.ok, plan.errors
    assert {p.file for p in plan.patches} == {"util.py", "app.py"}
    result = apply_rename(plan, dry_run=False)
    assert result.ok and set(result.written_files) == {"util.py", "app.py"}
    app_text = (root / "app.py").read_text(encoding="utf-8")
    assert "from util import calc_sum" in app_text  # 导入项已更新
    assert "calc_sum([1, 2])" in app_text
    assert "calc_total" not in app_text
    assert "import util" not in app_text.replace("from util import", "")  # 模块路径 util 不动
    _assert_all_files_parse(root)


def test_method_rename_updates_attribute_calls(tmp_path: Path) -> None:
    root = _write_proj(
        tmp_path,
        {"app.py": "class Repo:\n    def load(self):\n        return 1\n\nr = Repo()\nr.load()\nx = r.load\n"},
    )
    plan = plan_rename(root, "load", "fetch")
    assert plan.ok, plan.errors
    assert plan.definitions[0].qualified_name == "Repo.load"
    assert plan.definitions[0].kind == "method"
    assert plan.total_replace_points == 3  # 定义 + 调用 + 属性引用
    result = apply_rename(plan, dry_run=False)
    assert result.ok
    counts = _ident_counts(root / "app.py")
    assert counts["fetch"] == 3 and "load" not in counts


# ---------------------------------------------------------------- 安全防护


def test_substring_protection_user_vs_username(tmp_path: Path) -> None:
    root = _write_proj(
        tmp_path,
        {"app.py": "def user(uid):\n    return uid\n\nusername = 'bob'\nuser_id = user(1)\nprint(username, user_id)\n"},
    )
    before = _ident_counts(root / "app.py")
    assert before["user"] == 2 and before["username"] == 2 and before["user_id"] == 2  # username 出现两次：赋值 + print
    plan = plan_rename(root, "user", "member")
    assert plan.ok and plan.total_replace_points == 2  # 只有 user 本体，绝不吃掉 username/user_id
    result = apply_rename(plan, dry_run=False)
    assert result.ok
    after = _ident_counts(root / "app.py")
    assert after["member"] == 2 and "user" not in after
    assert after["username"] == 2 and after["user_id"] == 2  # 子串邻居原样


def test_strings_and_comments_keep_old_name(tmp_path: Path) -> None:
    root = _write_proj(
        tmp_path,
        {
            "app.py": (
                'def ping(host):\n'
                '    """ping 节点，docstring 陷阱。"""\n'
                "    # comment: ping again\n"
                '    label = "ping in string"\n'
                "    return ping(host)  # trailing ping\n"
            )
        },
    )
    plan = plan_rename(root, "ping", "pong")
    assert plan.ok and plan.total_replace_points == 2  # 仅定义 + 调用
    result = apply_rename(plan, dry_run=False)
    assert result.ok
    new_text = (root / "app.py").read_text(encoding="utf-8")
    assert "pong" in new_text
    # 字符串/注释里的旧名物理保留
    assert '"""ping 节点' in new_text
    assert "# comment: ping again" in new_text
    assert '"ping in string"' in new_text
    assert "# trailing ping" in new_text
    assert _ident_counts(root / "app.py")["pong"] == 2  # token 级零残留


def test_invalid_new_name_rejected(tmp_path: Path) -> None:
    root = _write_proj(tmp_path, {"app.py": "def ok_fn():\n    return 1\n"})
    source = (root / "app.py").read_text(encoding="utf-8")
    for bad in ("not-an-ident", "class", "1abc", ""):
        plan = plan_rename(root, "ok_fn", bad)
        assert not plan.ok and plan.patches == [], bad
        result = apply_rename(plan, dry_run=False)
        assert not result.ok and not result.applied
    assert (root / "app.py").read_text(encoding="utf-8") == source  # 磁盘原样


def test_symbol_not_found_rejected(tmp_path: Path) -> None:
    root = _write_proj(tmp_path, {"app.py": "x = 1\n"})
    plan = plan_rename(root, "ghost", "phantom")
    assert not plan.ok and plan.patches == []
    assert any("未找到" in e for e in plan.errors)


def test_multiple_definitions_ambiguous_refused_with_detail(tmp_path: Path) -> None:
    """dogfood 跨文件同名类（Validator ×2）：cross_check 的 models.Validator 引用
    归属不明 → 整体拒绝，报错文案升级为含定义点清单与歧义点 file:line 明细。

    在独立副本上执行；语料本体零改动。正向「全部可归属 → 放行」场景由
    test_rename_scope.py 的 tmp_path 用例锁定。
    """
    proj = tmp_path / "dogfood"
    shutil.copytree(DOGFOOD, proj)
    plan = plan_rename(proj, "Validator", "Checker")
    assert not plan.ok
    assert plan.patches == []
    assert len(plan.definitions) == 2
    message = next(e for e in plan.errors if "宁拒不改" in e)
    assert "检测到 2 个同名定义点" in message
    assert "models.py:Validator" in message and "validation.py:Validator" in message  # 定义点清单
    assert "其中 1 个引用点作用域归属不明" in message
    assert "validation.py:34(" in message  # 歧义点位置（cross_check 内 models.Validator）
    assert "models.Validator 形态" in message  # 歧义形态进文案
    result = apply_rename(plan, dry_run=False)
    assert not result.ok and not result.applied and result.written_files == []
    # 副本零落盘；原始语料目录本体零改动
    for py in proj.glob("*.py"):
        assert "Checker" not in py.read_text(encoding="utf-8")
    for py in DOGFOOD.glob("*.py"):
        assert "Checker" not in py.read_text(encoding="utf-8")


def test_ast_recheck_failure_is_all_or_nothing(tmp_path: Path) -> None:
    root = _write_proj(
        tmp_path,
        {"main.py": "from mod import helper\n\nvalue = helper(1)\n", "mod.py": "def helper(x):\n    return x\n"},
    )
    plan: RenamePlan = plan_rename(root, "helper", "assistant")
    assert plan.ok and len(plan.patches) == 2
    # 人为破坏其中一个补丁的替换后内容（模拟复检失败）
    broken = next(p for p in plan.patches if p.file == "mod.py")
    broken.new_bytes = b"def broken(:\n"
    snapshots = {p.file: (root / p.file).read_bytes() for p in plan.patches}
    result = apply_rename(plan, dry_run=False)
    assert not result.ok and not result.applied and result.written_files == []
    assert any("mod.py" in e and "AST 复检失败" in e for e in result.errors)
    for rel, snap in snapshots.items():  # all-or-nothing：全部文件未被改写
        assert (root / rel).read_bytes() == snap
    _assert_all_files_parse(root)


def test_stale_file_refused_before_write(tmp_path: Path) -> None:
    root = _write_proj(tmp_path, {"app.py": "def solo():\n    return 1\n"})
    plan = plan_rename(root, "solo", "unique")
    assert plan.ok
    (root / "app.py").write_text("def solo():\n    return 1\n\n# 外部追加\n", encoding="utf-8")  # 计划后文件被外部修改
    result = apply_rename(plan, dry_run=False)
    assert not result.ok and not result.applied
    assert any("并发修改" in e for e in result.errors)
    assert "# 外部追加" in (root / "app.py").read_text(encoding="utf-8")  # 外部修改未被覆盖
    assert "unique" not in (root / "app.py").read_text(encoding="utf-8")


def test_unsupported_language_rejected(tmp_path: Path) -> None:
    root = _write_proj(tmp_path, {"app.py": "def solo():\n    return 1\n"})
    plan = plan_rename(root, "solo", "unique", language="go")
    assert not plan.ok and plan.patches == []
    assert any("python" in e for e in plan.errors)


# ---------------------------------------------------------------- dogfood 门（≥18/20）


def test_dogfood_rename_gate_20_symbols(tmp_path: Path) -> None:
    """20 目标逐一在独立副本上走 dry_run→apply→再 plan 全链路，X/20 记入验收。"""
    verified = 0
    failures: list[str] = []
    for old, new in TARGETS:
        proj = tmp_path / old  # 每个符号独立副本，互不污染
        shutil.copytree(DOGFOOD, proj)
        try:
            _run_one_rename(proj, old, new)
            verified += 1
        except AssertionError as exc:
            failures.append(f"{old}: {exc}")
    assert verified >= 18, f"dogfood 门未过：{verified}/20，失败={failures}"


def _run_one_rename(proj: Path, old: str, new: str) -> None:
    """单目标全链路断言：①AST ②旧名 token 零残留 ③新名引用数守恒 ④幂等。"""
    plan = plan_rename(proj, old, new)
    assert plan.ok, f"plan 失败: {plan.errors}"
    assert len(plan.definitions) == 1, plan.definitions

    before = _proj_ident_counts(proj)
    assert before.get(old, 0) == plan.total_replace_points, (before.get(old), plan.total_replace_points)

    # dry_run：不落盘、diff 齐全
    snapshots = {py: py.read_bytes() for py in sorted(proj.rglob("*.py"))}
    dry = apply_rename(plan, dry_run=True)
    assert dry.ok and not dry.applied
    assert set(dry.diffs) == {p.file for p in plan.patches}
    for py, snap in snapshots.items():
        assert py.read_bytes() == snap, f"dry_run 改写了 {py.name}"

    # 真写
    result = apply_rename(plan, dry_run=False)
    assert result.ok and result.applied, result.errors
    assert set(result.written_files) == {p.file for p in plan.patches}

    # ① 每文件 AST parse 成功
    _assert_all_files_parse(proj)
    # ② 旧名在代码区（identifier）零残留；字符串/注释陷阱保留旧名
    after = _proj_ident_counts(proj)
    assert after.get(old, 0) == 0, f"旧名 {old} 仍有 {after.get(old)} 个 token 残留"
    trap_file = TEXT_TRAPS.get(old)
    if trap_file:
        assert old in (proj / trap_file).read_text(encoding="utf-8"), f"文本陷阱丢失: {trap_file}"
    # ③ 新名引用数 = 原引用数（跨文件全部更新），逐文件对账
    assert after.get(new, 0) == before.get(old, 0), (after.get(new), before.get(old))
    patch_points = {p.file: len(p.replace_points) for p in plan.patches}
    for rel, pts in patch_points.items():
        per_file_new = _ident_counts(proj / rel).get(new, 0)
        assert per_file_new == pts, (rel, per_file_new, pts)
    # 标识符陷阱：相邻不同符号原样保留
    if old in IDENT_TRAPS:
        trap_name, want = IDENT_TRAPS[old]
        assert before.get(trap_name, 0) == want and after.get(trap_name, 0) == want, (trap_name, before.get(trap_name), after.get(trap_name))
    # ④ 幂等：再 plan 找不到旧名
    plan2 = plan_rename(proj, old, new)
    assert not plan2.ok and plan2.patches == []
    assert any("未找到" in e for e in plan2.errors)
