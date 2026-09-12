"""Stage6 单测生成闭环编排（W2-A2；W7-A3 扩展 JS/TS）：run_testgen_stage。

流程（docs/02 §2 Stage6、docs/07 §3.2 契约 v1.2）：
  select_targets 选目标（verified Patch hunk / extra 指定 / critical-high Issue）
  → 按目标语言分派（Python→pytest 路径不变；JS/TS→node-test 路径；
    node 不可用时该语言目标 emit 警告并跳过——诚实降级）
  → 逐目标 generate_tests → validate_code / validate_code_js
    （非法记一次失败，带校验错误回喂）
  → write_generated 落盘（Python .py / JS .test.mjs）→ run_generated 沙箱运行
  → 失败把 stderr_tail 末尾 40 行截断作为上下文回喂重生成（≤2 次重试）
  → 仍失败 remove_generated 剔除 + TestCase(status="dropped")
  → 通过 → ctx.test_cases 追加 TestCase(status="passed")。

失败语义：
- 单目标失败不阻断后续（整体 try/except 兜底，沙箱超时按失败处理）；
- LLM 首轮即判 UNTESTABLE / 输出不可解析 → 跳过该目标（不落任何文件、不计入
  passed/dropped，避免无意义重试）；
- 同一目标重试时先剥离上一版失败代码块再追加新块，保证生成文件中每个目标
  只保留当前版本。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from audit.models import TestCase
from audit.pipeline import PipelineContext
from audit.sandbox import SandboxExecutor, test_runner_available
from audit.testgen.generator import (
    count_asserts,
    generate_tests,
    select_targets,
    validate_code,
    validate_code_js,
)
from audit.testgen.runner import remove_generated, run_generated, write_generated
from audit.utils import guess_language, make_id, truncate

__all__ = ["run_testgen_stage"]

_MAX_RETRIES = 2  # 失败后最多重试 2 次（共 3 次尝试）
_STDERR_TAIL_LINES = 40  # 回喂上下文保留 stderr 末尾行数
_STATS_KEYS = ("targets", "generated", "passed", "dropped", "retries")

_KIND_RE = re.compile(r"(?:#|//)\s*kind:\s*(normal|boundary|error)")


def _target_language(file: str) -> str:
    """目标文件语言：python / javascript / typescript（未知后缀按 python 处理，行为不变）。"""
    return guess_language(file) or "python"


def _parse_kind(code: str) -> str:
    """从代码注释解析用例类别（首个 # kind: / // kind: 标注）；缺省 normal。"""
    match = _KIND_RE.search(code or "")
    return match.group(1) if match else "normal"


def _tail_lines(text: str, max_lines: int) -> str:
    """保留末尾 max_lines 行。"""
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[-max_lines:])


async def run_testgen_stage(ctx: PipelineContext) -> None:
    """Stage6 入口：编排层 config.do_tests 时调用（契约 v1.2 §3.2）。

    产出：
    - ctx.test_cases 追加 TestCase（passed / dropped）；
    - src_root/tests/generated/ 下的生成测试文件（通过者保留，失败者剔除）；
    - ctx.extra["testgen_stats"] = {"targets","generated","passed","dropped","retries"}；
    - 每步 ctx.emit("testgen", ...) 进度事件，结束时发汇总事件。
    """
    stats: dict[str, int] = dict.fromkeys(_STATS_KEYS, 0)
    ctx.extra["testgen_stats"] = stats

    try:
        targets = select_targets(ctx)
    except Exception as exc:  # noqa: BLE001 —— 目标选取异常不阻断阶段
        targets = []
        await ctx.emit("testgen", f"目标选取异常：{type(exc).__name__}: {exc}", error=True)
    stats["targets"] = len(targets)

    await ctx.emit(
        "testgen",
        f"单测生成阶段开始：目标 {len(targets)} 个（上限 {ctx.config.testgen_max_functions}）",
        total=len(targets),
    )
    if not targets:
        await ctx.emit("testgen", f"单测生成阶段完成：无目标 {stats}", **stats)
        return

    sandbox = SandboxExecutor()
    tc_seq = 0
    for index, (file, symbol) in enumerate(targets, 1):
        await ctx.emit(
            "testgen",
            f"[{index}/{len(targets)}] 生成单测：{file}:{symbol}",
            current=index,
            total=len(targets),
        )
        # W7-A3：按语言分派运行框架；node 不可用时 JS/TS 目标 emit 警告并跳过（诚实降级）
        language = _target_language(file)
        if language != "python" and not test_runner_available("node-test"):
            await ctx.emit(
                "testgen",
                f"{file}:{symbol} 跳过（node 不可用，无法运行 node --test 验证 JS/TS 单测）",
                status="skipped",
            )
            continue
        try:
            tc_seq = await _testgen_one(ctx, sandbox, file, symbol, stats, tc_seq)
        except Exception as exc:  # noqa: BLE001 —— 单目标失败不阻断后续
            await ctx.emit(
                "testgen",
                f"{file}:{symbol} 单测生成异常：{type(exc).__name__}: {exc}",
                error=True,
            )

    ctx.extra["testgen_stats"] = stats
    await ctx.emit(
        "testgen",
        f"单测生成阶段完成：targets={stats['targets']} generated={stats['generated']} "
        f"passed={stats['passed']} dropped={stats['dropped']} retries={stats['retries']}",
        **stats,
    )


async def _testgen_one(
    ctx: PipelineContext,
    sandbox: SandboxExecutor,
    file: str,
    symbol: str,
    stats: dict[str, int],
    tc_seq: int,
) -> int:
    """单个目标的生成闭环：generate → validate → write → run（失败回喂 ≤2 次）。

    Returns:
        递增后的 TestCase 序号（追加 passed/dropped 记录时消耗）。
    """
    workspace = ctx.workspace
    language = _target_language(file)
    is_js = language in ("javascript", "typescript")
    framework = "node-test" if is_js else "pytest"
    runner_label = "node --test" if is_js else "pytest"
    fence_name = "```javascript" if is_js else "```python"
    error_context = ""
    gen_path: Path | None = None
    last_code = ""

    for attempt in range(_MAX_RETRIES + 1):
        generated = await generate_tests(
            ctx.llm, workspace, ctx.index, file, symbol, error_context=error_context, language=language
        )
        failure = ""
        if generated is None:
            if attempt == 0:
                # LLM 判定不可测（或两次解析失败）：跳过且不落任何文件（无污染）
                await ctx.emit(
                    "testgen",
                    f"{file}:{symbol} 跳过（LLM 判定不可单测或输出不可解析）",
                )
                return tc_seq
            failure = f"LLM 输出不可解析（无 {fence_name} 围栏）"
        else:
            code, _ = generated
            last_code = code
            syntax_checked = True
            if is_js:
                errors, syntax_checked = validate_code_js(code)
            else:
                errors = validate_code(code)
            if errors:
                failure = "生成代码校验失败：" + "；".join(errors)
            else:
                if is_js and not syntax_checked:
                    await ctx.emit(
                        "testgen",
                        f"{file}:{symbol} node 不可用，已跳过 node --check 语法校验（仅静态校验）",
                    )
                stats["generated"] += 1
                if gen_path is not None:
                    # 重试场景：先剥离上一版失败代码块，再追加新块
                    remove_generated(workspace, gen_path, file, symbol)
                gen_path = write_generated(workspace, file, symbol, code, language)
                result = await run_generated(sandbox, workspace, gen_path, framework)
                if result.exit_code == 0 and not result.timed_out:
                    tc_seq += 1
                    assert_count = count_asserts(code, language)
                    ctx.test_cases.append(
                        TestCase(
                            id=make_id("TC", tc_seq),
                            target=f"{file}:{symbol}",
                            file=workspace.rel(gen_path),
                            status="passed",
                            kind=_parse_kind(code),
                            assert_count=assert_count,
                        )
                    )
                    stats["passed"] += 1
                    await ctx.emit(
                        "testgen",
                        f"{file}:{symbol} 单测通过（assert {assert_count} 条）",
                        status="passed",
                    )
                    return tc_seq
                detail = _tail_lines(result.stderr_tail, _STDERR_TAIL_LINES) or _tail_lines(
                    result.stdout_tail, _STDERR_TAIL_LINES
                )
                failure = (
                    f"{runner_label} 未通过（exit={result.exit_code}, timed_out={result.timed_out}）：\n{detail}"
                )

        if attempt < _MAX_RETRIES:
            stats["retries"] += 1
            error_context = failure
            await ctx.emit(
                "testgen",
                f"{file}:{symbol} 第 {attempt + 1} 次尝试失败，带报错重试：{truncate(failure, 200)}",
            )

    # 重试耗尽仍失败 → 剔除生成文件 + dropped 记录
    if gen_path is not None:
        remove_generated(workspace, gen_path, file, symbol)
    tc_seq += 1
    ctx.test_cases.append(
        TestCase(
            id=make_id("TC", tc_seq),
            target=f"{file}:{symbol}",
            file=workspace.rel(gen_path) if gen_path is not None else "",
            status="dropped",
            kind=_parse_kind(last_code) if last_code else "normal",
            assert_count=count_asserts(last_code, language),
        )
    )
    stats["dropped"] += 1
    await ctx.emit(
        "testgen",
        f"{file}:{symbol} 单测剔除（重试 {_MAX_RETRIES} 次仍失败，已清理生成文件）",
        status="dropped",
    )
    return tc_seq
