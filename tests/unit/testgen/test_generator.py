"""W2-A2 单测：select_targets / generate_tests / count_asserts / validate_code（全离线）。

覆盖路径：
- select_targets 三来源优先级（verified Patch hunk → extra 指定 → critical/high Issue）
  与 index 缺失误容、去重排序、testgen_max_functions 上限；
- generate_tests：围栏抽取、UNTESTABLE 判定、解析失败回喂重试 1 次、prompt 内容；
- count_asserts：Assert 节点统计与语法错误容错；
- validate_code：无 assert / assert True / 语法错误 / 危险调用 / 越界写入 拒绝。
"""

from __future__ import annotations

from audit.llm.base import FakeLLMClient
from audit.models import Category, FunctionSlice, Issue, Patch, Severity

import _testgen_helpers as H
from audit.testgen.generator import (
    count_asserts,
    generate_tests,
    parse_hunks,
    select_targets,
    validate_code,
)

MATHX = "app/utils/mathx.py"


def _slice(file: str, symbol: str, start: int, end: int, code: str | None = None) -> FunctionSlice:
    body = code if code is not None else f"def {symbol}():\n    ...\n"
    return FunctionSlice(
        file=file, symbol=symbol, line_start=start, line_end=end,
        code=body, signature=body.splitlines()[0] if body else "",
    )


def _issue(severity: Severity, file: str, issue_id: str = "ISS-9001") -> Issue:
    return Issue(
        id=issue_id, category=Category.BUG, severity=severity, title="示例", file=file,
        line_start=1, line_end=1,
    )


# ---------------------------------------------------------------- parse_hunks


def test_parse_hunks_extracts_new_side_ranges():
    ranges = parse_hunks(H.MATHX_COMPARE_DIFF)
    assert ranges == {MATHX: [(10, 14)]}


def test_parse_hunks_handles_dev_null_and_empty_count():
    diff = (
        "diff --git a/new.py b/new.py\n"
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+x\n"
    )
    assert parse_hunks(diff) == {"new.py": [(1, 3)]}
    # 纯删除 hunk（新侧 count=0）不产出区间
    diff_del = (
        "diff --git a/old.py b/old.py\n"
        "--- a/old.py\n"
        "+++ b/old.py\n"
        "@@ -1,3 +0,0 @@\n"
        "-x\n"
    )
    assert parse_hunks(diff_del) == {}


# ---------------------------------------------------------------- select_targets


async def test_select_targets_from_verified_patch_hunks(tmp_path, fake_emitter):
    """来源①：verified Patch hunk 区间与切片相交的 symbol 被选中；非 verified 忽略。"""
    ws = H.make_workspace(tmp_path, {f"{MATHX}": "x = 1\n"})
    ctx = H.make_ctx(ws, FakeLLMClient(), fake_emitter)
    ctx.index = H.FakeIndex(
        {
            MATHX: [_slice(MATHX, "accumulate", 4, 7), _slice(MATHX, "compare", 10, 13)],
        }
    )
    ctx.patches = [
        H.verified_patch(),  # hunk 10-14 → 命中 compare
        Patch(id="PATCH-0002", diff=H.MATHX_COMPARE_DIFF, apply_status="needs-review"),
    ]

    assert select_targets(ctx) == [(MATHX, "compare")]


async def test_select_targets_multi_patches_dedup_sorted(tmp_path, fake_emitter):
    """多 verified Patch 跨文件目标去重排序。"""
    other = "app/services/orders.py"
    ws = H.make_workspace(tmp_path, {MATHX: "x = 1\n", other: "y = 2\n"})
    ctx = H.make_ctx(ws, FakeLLMClient(), fake_emitter)
    ctx.index = H.FakeIndex(
        {
            MATHX: [_slice(MATHX, "compare", 10, 13)],
            other: [_slice(other, "get_order_summary", 8, 13), _slice(other, "get_user", 1, 6)],
        }
    )
    other_diff = (
        f"diff --git a/{other} b/{other}\n--- a/{other}\n+++ b/{other}\n"
        "@@ -8,6 +8,6 @@\n context\n"
    )
    ctx.patches = [H.verified_patch(), H.verified_patch(other_diff, "PATCH-0002")]

    assert select_targets(ctx) == [(other, "get_order_summary"), (MATHX, "compare")]


async def test_select_targets_extra_used_when_no_verified_patch(tmp_path, fake_emitter):
    """来源②：无 verified Patch 时取 ctx.extra['testgen_targets']。"""
    ws = H.make_workspace(tmp_path, {MATHX: "x = 1\n"})
    ctx = H.make_ctx(ws, FakeLLMClient(), fake_emitter)
    ctx.index = H.FakeIndex({MATHX: [_slice(MATHX, "compare", 10, 13)]})
    ctx.patches = [Patch(id="PATCH-0001", diff=H.MATHX_COMPARE_DIFF, apply_status="failed")]
    ctx.extra["testgen_targets"] = [(MATHX, "build_page"), ("bad-item",)]  # 坏项容错跳过

    assert select_targets(ctx) == [(MATHX, "build_page")]


async def test_select_targets_issues_fallback_first_slice(tmp_path, fake_emitter):
    """来源③：无 Patch/extra 时 critical/high Issue 文件去重，每文件取首个切片。"""
    ws = H.make_workspace(tmp_path, {MATHX: "x = 1\n", "app/services/users.py": "y = 2\n"})
    ctx = H.make_ctx(ws, FakeLLMClient(), fake_emitter)
    ctx.index = H.FakeIndex(
        {
            MATHX: [_slice(MATHX, "accumulate", 4, 7), _slice(MATHX, "compare", 10, 13)],
            "app/services/users.py": [],
        }
    )
    ctx.issues = [
        _issue(Severity.MEDIUM, MATHX),  # 非 critical/high：忽略
        _issue(Severity.HIGH, MATHX),
        _issue(Severity.CRITICAL, MATHX, "ISS-9002"),  # 同文件去重
        _issue(Severity.HIGH, "app/services/users.py"),  # 无切片文件：跳过
    ]

    assert select_targets(ctx) == [(MATHX, "accumulate")]


async def test_select_targets_priority_patch_beats_extra_beats_issues(tmp_path, fake_emitter):
    """优先级：verified Patch > extra 指定 > Issue 兜底。"""
    ws = H.make_workspace(tmp_path, {MATHX: "x = 1\n"})
    ctx = H.make_ctx(ws, FakeLLMClient(), fake_emitter)
    ctx.index = H.FakeIndex(
        {MATHX: [_slice(MATHX, "compare", 10, 13), _slice(MATHX, "build_page", 16, 20)]}
    )
    ctx.issues = [_issue(Severity.CRITICAL, MATHX)]

    # ① 生效
    ctx.patches = [H.verified_patch()]
    ctx.extra["testgen_targets"] = [(MATHX, "build_page")]
    assert select_targets(ctx) == [(MATHX, "compare")]

    # ① 空 → ② 生效
    ctx.patches = []
    assert select_targets(ctx) == [(MATHX, "build_page")]

    # ①② 空 → ③ 生效
    ctx.extra.clear()
    assert select_targets(ctx) == [(MATHX, "compare")]


async def test_select_targets_index_none_skips_patch_and_issue_sources(tmp_path, fake_emitter):
    """index 为 None：来源①③跳过；extra 仍可用；全空返回 []。"""
    ws = H.make_workspace(tmp_path, {MATHX: "x = 1\n"})
    ctx = H.make_ctx(ws, FakeLLMClient(), fake_emitter)
    ctx.index = None
    ctx.patches = [H.verified_patch()]
    ctx.issues = [_issue(Severity.CRITICAL, MATHX)]
    assert select_targets(ctx) == []

    ctx.extra["testgen_targets"] = [(MATHX, "compare")]
    assert select_targets(ctx) == [(MATHX, "compare")]


async def test_select_targets_respects_max_functions(tmp_path, fake_emitter):
    """testgen_max_functions 上限截断（排序后取前 N）。"""
    ws = H.make_workspace(tmp_path, {MATHX: "x = 1\n"})
    ctx = H.make_ctx(ws, FakeLLMClient(), fake_emitter, testgen_max_functions=1)
    ctx.extra["testgen_targets"] = [(MATHX, "zeta"), (MATHX, "alpha"), ("b.py", "m")]

    assert select_targets(ctx) == [(MATHX, "alpha")]


# ---------------------------------------------------------------- generate_tests


async def test_generate_tests_extracts_fenced_code(tmp_path, fake_emitter):
    """围栏代码正常抽取；prompt 注入目标源码/签名与关键约束。"""
    ws = H.make_workspace(tmp_path, {MATHX: "def compare(a, b):\n    return a == b\n"})
    fake = FakeLLMClient([{"content": H.fenced(H.GOOD_TEST_CODE)}])
    index = H.FakeIndex(
        {MATHX: [_slice(MATHX, "compare", 1, 2, code="def compare(a, b):\n    return a == b\n")]}
    )

    result = await generate_tests(fake, ws, index, MATHX, "compare")

    assert result == (H.GOOD_TEST_CODE.strip(), "compare")
    assert len(fake.calls) == 1
    user_text = fake.calls[0]["messages"][1]["content"]
    assert "[target]" in user_text and "compare" in user_text
    assert "def compare(a, b):" in user_text  # 切片源码注入
    system_text = fake.calls[0]["messages"][0]["content"]
    assert "# kind:" in system_text and "assert True" in system_text


async def test_generate_tests_untestable_returns_none_without_retry(tmp_path, fake_emitter):
    """LLM 判定 UNTESTABLE → None，且不触发重试调用。"""
    ws = H.make_workspace(tmp_path, {MATHX: "x = 1\n"})
    fake = FakeLLMClient([{"content": H.UNTESTABLE_TEXT}])

    assert await generate_tests(fake, ws, None, MATHX, "compare") is None
    assert len(fake.calls) == 1


async def test_generate_tests_no_fence_retries_once_then_none(tmp_path, fake_emitter):
    """两次都无围栏 → None；第二次调用带上一轮失败回喂。"""
    ws = H.make_workspace(tmp_path, {MATHX: "x = 1\n"})
    fake = FakeLLMClient([{"content": "我觉得应该这样测试……"}, {"content": "还是没有围栏"}])

    assert await generate_tests(fake, ws, None, MATHX, "compare") is None
    assert len(fake.calls) == 2
    retry_user = fake.calls[1]["messages"][-1]
    assert retry_user["role"] == "user"
    assert "无法解析" in retry_user["content"]


async def test_generate_tests_error_context_feeded_into_prompt(tmp_path, fake_emitter):
    """stage 重试场景：error_context 注入 [previous_failure] 段。"""
    ws = H.make_workspace(tmp_path, {MATHX: "x = 1\n"})
    fake = FakeLLMClient([{"content": H.fenced(H.GOOD_TEST_CODE)}])

    await generate_tests(fake, ws, None, MATHX, "compare", error_context="pytest 未通过：E AssertionError")

    user_text = fake.calls[0]["messages"][1]["content"]
    assert "[previous_failure]" in user_text
    assert "pytest 未通过" in user_text


async def test_generate_tests_target_block_falls_back_to_file_read(tmp_path, fake_emitter):
    """索引缺失/无切片：回退整文件注入（截断保护由常量保证）。"""
    ws = H.make_workspace(tmp_path, {MATHX: "def compare(a, b):\n    return a == b\n"})
    fake = FakeLLMClient([{"content": H.fenced(H.GOOD_TEST_CODE)}])

    result = await generate_tests(fake, ws, None, MATHX, "compare")

    assert result is not None
    user_text = fake.calls[0]["messages"][1]["content"]
    assert "def compare(a, b):" in user_text


# ---------------------------------------------------------------- count_asserts / validate_code


def test_count_asserts_counts_and_tolerates_syntax_error():
    code = "def t():\n    assert 1 == 1\n    assert 2 == 2\n    assert 3 == 3\n"
    assert count_asserts(code) == 3
    assert count_asserts("def broken(:\n") == 0
    assert count_asserts("") == 0


def test_validate_code_accepts_good_code():
    assert validate_code(H.GOOD_TEST_CODE) == []


def test_validate_code_rejects_missing_assert():
    errors = validate_code(H.NO_ASSERT_CODE)
    assert any("assert" in e for e in errors)


def test_validate_code_rejects_assert_true():
    errors = validate_code("def t():\n    assert True\n")
    assert any("assert True" in e for e in errors)


def test_validate_code_rejects_syntax_error():
    errors = validate_code("def broken(:\n    pass\n")
    assert any("语法解析失败" in e for e in errors)


def test_validate_code_rejects_dangerous_calls():
    cases = [
        "import os\nos.system('rm -rf /')\nassert 1\n",
        "from subprocess import run\nassert 1\nrun(['ls'])\n",
        "import shutil\nassert 1\nshutil.rmtree('/x')\n",
        "assert eval('1 + 1') == 2\n",
        "exec('x = 1')\nassert 1\n",
        "__import__('os').getcwd()\nassert 1\n",
    ]
    for code in cases:
        errors = validate_code(code)
        assert errors, f"应拒绝危险代码：{code!r}"
        assert any("禁止" in e for e in errors)


def test_validate_code_rejects_out_of_project_write():
    errors = validate_code("with open('/etc/passwd', 'w') as f:\n    f.write('x')\nassert 1\n")
    assert any("写入" in e for e in errors)
    # 项目内相对路径写入 / 纯读不拦
    assert validate_code("open('local.txt', 'w').close()\nassert 1\n") == []
    assert validate_code("open('local.txt').read()\nassert 1\n") == []


def test_validate_code_rejects_empty():
    assert validate_code("   \n") == ["生成代码为空"]
