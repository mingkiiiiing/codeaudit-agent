"""W2-A2 单测：run_testgen_stage 编排闭环（FakeLLM 脚本回放，沙箱真跑 pytest）。

覆盖路径：
- happy：verified Patch（指向 app/utils/mathx.py 的 compare）→ 生成通过 → passed 记录
  + assert_count≥2 + tests/generated/ 落盘 + 无污染；
- 重试：脚本 [失败代码, 失败代码, 好代码] → 最终 passed 且 retries=2；
- 校验失败回喂：[无 assert 代码, 好代码] → 非法记一次失败后通过；
- 剔除：三次全坏 → dropped 记录 + 生成文件被清理；
- UNTESTABLE：跳过、无记录、无污染；
- 多目标：TC 编号递增、同文件多块追加；
- 无目标：直接完成。
"""

from __future__ import annotations

from pathlib import Path

from audit.indexer import create_index
from audit.llm.base import FakeLLMClient

import _testgen_helpers as H
from audit.testgen.stage import run_testgen_stage

MATHX = "app/utils/mathx.py"

# 自造最小工程的目标函数（与 GOOD/FAILING 测试代码语义匹配）
MATHX_SOURCE = """def compare(a, b):
    if a == None:
        return False
    return a == b
"""


def _small_workspace(tmp_path: Path):
    """最小工程：app/utils/mathx.py（compare），无需真实索引（extra 指定目标）。"""
    return H.make_workspace(
        tmp_path,
        {"app/__init__.py": "", "app/utils/__init__.py": "", MATHX: MATHX_SOURCE},
    )


def _assert_no_pollution(workspace) -> None:
    """无污染断言：src 的 tests/ 下除 generated/ 外无新文件。"""
    tests_root = workspace.src_root / "tests"
    if not tests_root.is_dir():
        return
    for path in tests_root.rglob("*"):
        if path.is_file():
            rel_parts = path.relative_to(tests_root).parts
            assert "generated" in rel_parts, f"污染用户测试目录：{path}"


# ---------------------------------------------------------------- happy path


async def test_happy_path_verified_patch_generates_passing_tests(sample_workspace, fake_emitter):
    """verified Patch → hunk 命中 compare → FakeLLM 好代码 → passed + 落盘 + 无污染。"""
    index = create_index(sample_workspace)
    index.build()
    try:
        fake = FakeLLMClient([{"content": H.fenced(H.GOOD_TEST_CODE)}])
        ctx = H.make_ctx(sample_workspace, fake, fake_emitter)
        ctx.index = index
        ctx.patches = [H.verified_patch()]

        await run_testgen_stage(ctx)
    finally:
        index.close()

    # TestCase 产物
    assert len(ctx.test_cases) == 1
    tc = ctx.test_cases[0]
    assert tc.id == "TC-0001"
    assert tc.target == f"{MATHX}:compare"
    assert tc.file == "tests/generated/test_gen_app_utils_mathx.py"
    assert tc.status == "passed"
    assert tc.kind == "normal"  # 首个 kind 注释为 normal
    assert tc.assert_count >= 2

    # 生成文件落盘且带头注释
    gen_file = H.generated_file(sample_workspace, MATHX)
    assert gen_file.exists()
    text = gen_file.read_text(encoding="utf-8")
    assert "由 CodeAudit 自动生成" in text

    # 统计
    assert ctx.extra["testgen_stats"] == {
        "targets": 1, "generated": 1, "passed": 1, "dropped": 0, "retries": 0,
    }
    # 事件
    stages = [e for e in fake_emitter.events if e.get("stage") == "testgen"]
    assert any("单测生成阶段完成" in e.get("message", "") for e in stages)
    assert any(e.get("status") == "passed" for e in stages)

    _assert_no_pollution(sample_workspace)


# ---------------------------------------------------------------- 重试路径


async def test_retry_path_failing_twice_then_pass(tmp_path, fake_emitter):
    """脚本 [失败代码, 失败代码, 好代码] → passed 且 retries=2。"""
    ws = _small_workspace(tmp_path)
    fake = FakeLLMClient(
        [
            {"content": H.fenced(H.FAILING_TEST_CODE)},
            {"content": H.fenced(H.FAILING_TEST_CODE)},
            {"content": H.fenced(H.GOOD_TEST_CODE)},
        ]
    )
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.extra["testgen_targets"] = [(MATHX, "compare")]

    await run_testgen_stage(ctx)

    assert [tc.status for tc in ctx.test_cases] == ["passed"]
    assert ctx.test_cases[0].target == f"{MATHX}:compare"
    # 报错回喂：第 2/3 次生成的 prompt 带 [previous_failure]
    assert len(fake.calls) == 3
    assert "[previous_failure]" in fake.calls[1]["messages"][1]["content"]
    assert "[previous_failure]" in fake.calls[2]["messages"][1]["content"]
    assert ctx.extra["testgen_stats"] == {
        "targets": 1, "generated": 3, "passed": 1, "dropped": 0, "retries": 2,
    }
    # 最终文件里只保留当前版本（旧失败块已被剥离）
    text = H.generated_file(ws, MATHX).read_text(encoding="utf-8")
    assert text.count("test_compare_wrong_expectation") == 0
    assert "test_compare_equal_values" in text
    _assert_no_pollution(ws)


async def test_validate_failure_counts_one_and_retries(tmp_path, fake_emitter):
    """[无 assert 代码, 好代码]：非法记一次失败（不落盘不跑沙箱）→ 重试后通过。"""
    ws = _small_workspace(tmp_path)
    fake = FakeLLMClient(
        [
            {"content": H.fenced(H.NO_ASSERT_CODE)},
            {"content": H.fenced(H.GOOD_TEST_CODE)},
        ]
    )
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.extra["testgen_targets"] = [(MATHX, "compare")]

    await run_testgen_stage(ctx)

    assert [tc.status for tc in ctx.test_cases] == ["passed"]
    assert ctx.extra["testgen_stats"] == {
        "targets": 1, "generated": 1, "passed": 1, "dropped": 0, "retries": 1,
    }
    assert "缺少显式 assert" in fake.calls[1]["messages"][1]["content"]
    _assert_no_pollution(ws)


# ---------------------------------------------------------------- 剔除路径


async def test_all_attempts_fail_drops_and_cleans_file(tmp_path, fake_emitter):
    """三次全坏 → dropped 记录 + 生成文件被清理。"""
    ws = _small_workspace(tmp_path)
    fake = FakeLLMClient([{"content": H.fenced(H.FAILING_TEST_CODE)}] * 3)
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.extra["testgen_targets"] = [(MATHX, "compare")]

    await run_testgen_stage(ctx)

    assert len(ctx.test_cases) == 1
    tc = ctx.test_cases[0]
    assert tc.id == "TC-0001"
    assert tc.status == "dropped"
    assert tc.target == f"{MATHX}:compare"
    assert tc.file == "tests/generated/test_gen_app_utils_mathx.py"
    assert tc.assert_count == 1  # 失败代码里有 1 条 assert
    # 生成文件被清理（.py 全部移除；目录可能残留 __pycache__）
    gen_dir = ws.src_root / "tests" / "generated"
    assert not H.generated_file(ws, MATHX).exists()
    if gen_dir.is_dir():
        assert list(gen_dir.rglob("*.py")) == []
    assert ctx.extra["testgen_stats"] == {
        "targets": 1, "generated": 3, "passed": 0, "dropped": 1, "retries": 2,
    }
    assert any(e.get("status") == "dropped" for e in fake_emitter.events)
    _assert_no_pollution(ws)


# ---------------------------------------------------------------- UNTESTABLE / 边界


async def test_untestable_skips_without_pollution(tmp_path, fake_emitter):
    """LLM 输出 '# UNTESTABLE:' → 跳过：无记录、无文件、不计重试。"""
    ws = _small_workspace(tmp_path)
    fake = FakeLLMClient([{"content": H.UNTESTABLE_TEXT}])
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.extra["testgen_targets"] = [(MATHX, "compare")]

    await run_testgen_stage(ctx)

    assert ctx.test_cases == []
    assert ctx.extra["testgen_stats"] == {
        "targets": 1, "generated": 0, "passed": 0, "dropped": 0, "retries": 0,
    }
    assert not (ws.src_root / "tests" / "generated").exists()
    _assert_no_pollution(ws)


async def test_multi_targets_share_generated_file_with_sequential_ids(tmp_path, fake_emitter):
    """同文件两个目标共用生成文件：块追加、TC 编号递增。"""
    ws = _small_workspace(tmp_path)
    build_page_source = MATHX_SOURCE + """

def build_page(rows):
    html = ""
    for r in rows:
        html = html + "<li>" + str(r) + "</li>"
    return html
"""
    build_page_test = """from app.utils.mathx import build_page


# kind: boundary
def test_build_page_empty_rows():
    assert build_page([]) == ""
"""
    ws.abs_path(MATHX).write_text(build_page_source, encoding="utf-8")
    fake = FakeLLMClient(
        [
            {"content": H.fenced(build_page_test)},
            {"content": H.fenced(H.GOOD_TEST_CODE)},
        ]
    )
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.extra["testgen_targets"] = [(MATHX, "compare"), (MATHX, "build_page")]

    await run_testgen_stage(ctx)

    assert [tc.id for tc in ctx.test_cases] == ["TC-0001", "TC-0002"]
    assert [tc.status for tc in ctx.test_cases] == ["passed", "passed"]
    # 排序后 build_page 在前（字母序）
    assert ctx.test_cases[0].kind == "boundary"
    assert ctx.test_cases[1].kind == "normal"
    text = H.generated_file(ws, MATHX).read_text(encoding="utf-8")
    assert text.count("# ---- CodeAudit block: ") == 2
    assert ctx.extra["testgen_stats"]["passed"] == 2
    _assert_no_pollution(ws)


async def test_no_targets_completes_immediately(sample_workspace, fake_emitter):
    """无 patches/extra/issues → 零目标直接完成，不触碰文件系统。"""
    fake = FakeLLMClient([])
    ctx = H.make_ctx(sample_workspace, fake, fake_emitter)

    await run_testgen_stage(ctx)

    assert ctx.test_cases == []
    assert ctx.extra["testgen_stats"] == {
        "targets": 0, "generated": 0, "passed": 0, "dropped": 0, "retries": 0,
    }
    assert fake.calls == []
    assert any("无目标" in e.get("message", "") for e in fake_emitter.events)
