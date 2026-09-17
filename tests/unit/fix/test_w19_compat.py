"""W19 单测：Patch 接口兼容性比对（extract/diff/collect + stage 接线，全部离线）。

覆盖路径：
- extract_python_signatures：模块级/类方法/默认值与 *args/**kwargs/裸 */类型注解
  忽略/_private 排除/嵌套函数不收/装饰器函数可收/坏源码不抛（正则回退）；
- diff_signatures：移除函数/移除方法/签名变更/新增带默认值参数不算破坏/
  参数改名与减参算破坏/无变化与新增函数空列表；
- collect_compat_notes：JS 文件不比对（已知限制）；
- stage 接线：syntax-ok 路径写入"变更签名 add"、verified 无破坏时空列表
  （既有断言零回归）、回滚路径空列表、比对异常不影响主流程。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from audit.fix.compat import collect_compat_notes, diff_signatures, extract_python_signatures
from audit.fix.stage import run_fix_stage
from audit.llm.base import FakeLLMClient
from audit.models import FixStatus, Patch

import _fix_helpers as H


# ---------------------------------------------------------------- extract


def test_extract_module_function():
    """模块级函数：限定名 = 函数名，参数按序规范化。"""
    sigs = extract_python_signatures("def add(a, b):\n    return a + b\n")
    assert sigs == {"add": "a, b"}


def test_extract_class_methods():
    """一级类方法：限定名 = Class.method（self 计入参数）。"""
    sigs = extract_python_signatures(
        "class Foo:\n"
        "    def bar(self, x):\n"
        "        return x\n"
        "\n"
        "    def baz(self):\n"
        "        return 1\n"
    )
    assert sigs == {"Foo.bar": "self, x", "Foo.baz": "self"}


def test_extract_defaults_and_splats():
    """默认值参数记 name=，*args/**kwargs/裸 * 均保留且保序。"""
    sigs = extract_python_signatures(
        "def f(a, b=1, *args, **kw):\n"
        "    pass\n"
        "\n"
        "\n"
        "def g(a, *, b, c=2):\n"
        "    pass\n"
    )
    assert sigs == {"f": "a, b=, *args, **kw", "g": "a, *, b, c="}


def test_extract_ignores_type_annotations():
    """类型注解不算破坏性变更：规范化时只留参数名与默认值有无。"""
    sigs = extract_python_signatures("def f(x: int, y: str = 'a', *rest: bytes):\n    pass\n")
    assert sigs == {"f": "x, y=, *rest"}


def test_extract_excludes_private():
    """下划线开头的函数/方法/类整体排除（_Foo.bar 也不算公开 API）。"""
    sigs = extract_python_signatures(
        "def _hidden(a):\n"
        "    pass\n"
        "\n"
        "\n"
        "def pub():\n"
        "    pass\n"
        "\n"
        "\n"
        "class Foo:\n"
        "    def _secret(self):\n"
        "        pass\n"
        "\n"
        "    def open(self):\n"
        "        pass\n"
        "\n"
        "\n"
        "class _Inner:\n"
        "    def bar(self):\n"
        "        pass\n"
    )
    assert sigs == {"pub": "", "Foo.open": "self"}


def test_extract_skips_nested_functions():
    """嵌套函数/闭包不属于公开 API 面：inner 不收，只收 outer。"""
    sigs = extract_python_signatures(
        "def outer():\n"
        "    def inner():\n"
        "        pass\n"
        "    return inner\n"
    )
    assert sigs == {"outer": ""}


def test_extract_decorated_function():
    """装饰器包裹的函数（decorated_definition）同样可提取。"""
    sigs = extract_python_signatures("import functools\n\n\n@functools.lru_cache\ndef cached(x):\n    return x\n")
    assert sigs == {"cached": "x"}


def test_extract_bad_source_no_raise():
    """坏 Python 源码不抛异常：tree-sitter 解析失败回退正则，返回 dict。"""
    bad = "def broken(:\n    @@@ $$$\n\nclass @@\ndef still_ok(a, b):\n    pass\n"
    sigs = extract_python_signatures(bad)
    assert isinstance(sigs, dict)
    # 正则回退仍能从残损文本中抓到格式完整的 def 行
    assert sigs.get("still_ok") == "a, b"


# ---------------------------------------------------------------- diff


def test_diff_removed_function():
    """before 有 after 无 → 移除函数。"""
    assert diff_signatures({"foo": "a"}, {}) == ["移除函数 foo"]


def test_diff_removed_method():
    """方法移除按限定名含点区分措辞。"""
    assert diff_signatures({"Foo.bar": "self"}, {}) == ["移除方法 Foo.bar"]


def test_diff_signature_changed():
    """参数增加（无默认值）→ 变更签名，含前后参数串。"""
    before = {"add": "a, b"}
    after = {"add": "a, b, c"}
    assert diff_signatures(before, after) == ["变更签名 add(a, b) -> add(a, b, c)"]


def test_diff_added_param_with_default_not_breaking():
    """新增带默认值的参数不算破坏：不计入清单。"""
    before = {"add": "a, b"}
    after = {"add": "a, b, c="}
    assert diff_signatures(before, after) == []
    # *args / **kwargs 追加同样向后兼容
    assert diff_signatures({"f": "a"}, {"f": "a, *args, **kw"}) == []


def test_diff_param_rename_breaking():
    """参数改名 → 前缀不再逐位相等 → 变更签名。"""
    assert diff_signatures({"f": "a, b"}, {"f": "a, c"}) == ["变更签名 f(a, b) -> f(a, c)"]


def test_diff_removed_param_breaking():
    """减参（新参列表比旧参短）→ 变更签名。"""
    assert diff_signatures({"f": "a, b"}, {"f": "a"}) == ["变更签名 f(a, b) -> f(a)"]


def test_diff_no_change_and_added_function():
    """签名无变化 / after 新增公开函数：均不算破坏，返回空列表。"""
    assert diff_signatures({"foo": "a", "Bar.m": "self"}, {"foo": "a", "Bar.m": "self", "new_fn": "x"}) == []
    assert diff_signatures({}, {"anything": ""}) == []


# ---------------------------------------------------------------- collect


class _RaisingWorkspace:
    """abs_path 必抛 OSError 的工作副本替身（删除文件场景）。"""

    def abs_path(self, rel):
        raise OSError("gone")


def test_collect_compat_notes_skips_non_python(tmp_path: Path):
    """已知限制：JS 文件不参与比对，compat_notes 恒为空。"""
    (tmp_path / "app.js").write_text("function add(a, b) { return a + b; }\n", encoding="utf-8", newline="\n")

    class _Ws:
        def abs_path(self, rel):
            return tmp_path / str(rel).replace("\\", "/")

    assert collect_compat_notes({"app.js": b"function add(a) { return a; }\n"}, _Ws()) == []


def test_collect_compat_notes_deleted_file_reports_removal():
    """文件被删除（after 缺失）不抛异常：before 的公开函数逐个报移除（真实破坏）。"""
    assert collect_compat_notes({"gone.py": b"def f(a):\n    pass\n"}, _RaisingWorkspace()) == ["移除函数 f"]


# ---------------------------------------------------------------- stage 接线（集成级）

# 签名破坏型样例：def add(a, b) → def add(a, b, c)（无默认值，破坏）
SIGNATURE_APP_PY = "def add(a, b):\n    return a + b\n"
SIGNATURE_BREAK_DIFF = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-def add(a, b):\n"
    "+def add(a, b, c):\n"
    "     return a + b\n"
)


def _issue(issue_id: str, file: str, line: int = 1):
    from audit.models import Category, Issue, Severity

    return Issue(
        id=issue_id,
        category=Category.BUG,
        severity=Severity.CRITICAL,
        title="示例缺陷",
        file=file,
        line_start=line,
        line_end=line,
        code_snippet="",
        description="示例缺陷",
        evidence=["rule_hit: demo"],
        confidence=0.9,
    )


async def test_stage_syntax_ok_path_writes_compat_notes(tmp_path: Path, fake_emitter):
    """syntax-ok 路径：签名破坏补丁应用后 compat_notes 含"变更签名 add"，事件带"接口变更 1 处"。"""
    ws = H.make_workspace(tmp_path, {"app.py": SIGNATURE_APP_PY})  # 无测试 → syntax-ok
    fake = FakeLLMClient([H.llm_json(SIGNATURE_BREAK_DIFF, "增加参数示意")])
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.issues = [_issue("ISS-0001", "app.py")]

    await run_fix_stage(ctx)

    assert len(ctx.patches) == 1
    patch = ctx.patches[0]
    assert patch.apply_status == "syntax-ok"
    assert patch.compat_notes == ["变更签名 add(a, b) -> add(a, b, c)"]
    stages = [e for e in fake_emitter.events if e.get("stage") == "fix"]
    assert any("接口变更 1 处" in str(e.get("message", "")) for e in stages)


async def test_stage_verified_without_breaking_keeps_notes_empty(tmp_path: Path, fake_emitter):
    """verified 路径：不改签名的常规修复 compat_notes 为空（既有断言天然兼容，零回归）。"""
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY, "tests/test_app.py": H.TEST_APP_PY})
    fake = FakeLLMClient([H.llm_json(H.BARE_EXCEPT_DIFF, "裸 except 改为精确捕获")])
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.issues = [_issue("ISS-0001", "app.py", line=9)]

    await run_fix_stage(ctx)

    patch = ctx.patches[0]
    assert patch.apply_status == "verified"
    assert patch.tests_passed == patch.tests_run == 2
    assert patch.compat_notes == []


async def test_stage_rollback_path_notes_empty(tmp_path: Path, fake_emitter):
    """needs-review 回滚路径：after==before，compat_notes 为空且文件已还原。"""
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY, "tests/test_app.py": H.TEST_APP_PY})
    fake = FakeLLMClient([H.llm_json(H.SYNTAX_BREAK_DIFF, "语法破坏示意")])
    issue = _issue("ISS-0001", "app.py", line=9)
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.issues = [issue]

    await run_fix_stage(ctx)

    patch = ctx.patches[0]
    assert patch.apply_status == "needs-review"
    assert issue.fix_status == FixStatus.NEEDS_REVIEW
    assert patch.compat_notes == []
    assert ws.abs_path("app.py").read_bytes() == H.APP_PY.encode("utf-8")


async def test_stage_compat_error_does_not_break_flow(tmp_path: Path, fake_emitter, monkeypatch):
    """比对异常兜底：collect 抛错时 patch 主流程不受影响，错误记入 compat_errors。"""
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY, "tests/test_app.py": H.TEST_APP_PY})
    fake = FakeLLMClient([H.llm_json(H.BARE_EXCEPT_DIFF, "常规修复")])
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.issues = [_issue("ISS-0001", "app.py", line=9)]

    def _boom(backups, workspace):
        raise RuntimeError("compat exploded")

    monkeypatch.setattr("audit.fix.stage.collect_compat_notes", _boom)

    await run_fix_stage(ctx)

    patch = ctx.patches[0]
    assert patch.apply_status == "verified"  # 主流程照常
    assert patch.compat_notes == []  # 兜底置空，不抛
    errors = ctx.extra.get("compat_errors")
    assert errors and "ISS-0001" in errors[0] and "RuntimeError" in errors[0]
    # Patch 模型契约不受影响：compat_notes 字段存在且为 list
    assert isinstance(Patch().compat_notes, list)


def test_regex_fallback_class_indentation(tmp_path: Path):
    """正则回退：类栈按缩进维护，方法随类名限定（防御 tree-sitter 不可用场景）。"""
    from audit.fix.compat import _extract_by_regex

    sigs = _extract_by_regex(
        "class Outer:\n"
        "    def m(self, a):\n"
        "        pass\n"
        "\n"
        "\n"
        "def top(b=1):\n"
        "    pass\n"
    )
    assert sigs == {"Outer.m": "self, a", "top": "b="}


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("a", "a, b=", True),  # 尾部追加带默认值参数
        ("a", "b, a", False),  # 顺序/名称不匹配
        ("a, b", "a", False),  # 减参
        ("", "x=", True),  # 无参 → 带默认值参数
        ("a", "a", True),  # 完全一致按兼容处理（调用方不报）
        ("a", "a, *args", True),  # 追加 *args
    ],
)
def test_nonbreaking_extension_matrix(old: str, new: str, expected: bool):
    """向后兼容判定矩阵（_is_nonbreaking_extension 行为锚定）。"""
    from audit.fix.compat import _is_nonbreaking_extension

    assert _is_nonbreaking_extension(old, new) is expected
