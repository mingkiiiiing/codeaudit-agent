"""Stage5 修复闭环编排（W2-A1）：run_fix_stage。

流程（docs/02 §2 Stage5、docs/07 §3.2 契约 v1.2）：
  选 Issue（critical/high，按 严重度→置信度 排序，上限 config.fix_max_patches）
  → Fix Agent 生成 unified diff（FakeLLM/真实 LLM 皆可）
  → validate_diff 路径安全校验（非法 → needs-review，跳过应用）
  → 备份原文件 → git apply --check → git apply
  → tree-sitter 语法重解析（失败 → 回滚 + needs-review）
  → 探测现有测试：无 → syntax-ok；全绿 → verified；失败/超时 → 回滚 + needs-review。

W7-A3：闭环对 JS/TS Issue 直接可用——选 Issue 只按 severity 不按语言；语法重解析
由 tree-sitter 原生支持 js/ts；现有测试探测新增 node-test（node --test，node 不可用
时 find_existing_tests 返回 None，诚实降级 syntax-ok）。

失败语义：
- 单 Issue 失败不阻断后续（整体 try/except 兜底，沙箱超时按失败处理）；
- 同文件多个 Issue 顺序处理，后续 Patch 基于已改内容重新读取/备份；
- fix_status 一律用 audit.models.FixStatus 枚举，Patch.apply_status 用字符串
  （"verified" / "needs-review" / "syntax-ok" / "failed"）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from audit.fix.patcher import _diff_targets, apply_diff, generate_patch, validate_diff
from audit.fix.verifier import find_existing_tests, run_existing_tests, syntax_ok
from audit.models import FixStatus, Issue, Patch, Severity
from audit.pipeline import PipelineContext
from audit.sandbox import SandboxExecutor, SandboxResult
from audit.utils import make_id, truncate

__all__ = ["run_fix_stage"]

# stats 终态桶：每个被尝试的 Issue 恰好落入一个（verified/syntax_ok/needs_review/failed）
_STATS_KEYS = ("attempted", "patch_generated", "applied", "verified", "syntax_ok", "needs_review", "failed")
_SEVERITY_RANK = {"critical": 0, "high": 1}

_PYTEST_NO_TESTS_EXIT = 5  # pytest "no tests ran"
_TEST_PASSED_RE = re.compile(r"(\d+) passed")
_TEST_TOTAL_RE = re.compile(r"(\d+) total")
_TEST_EXTRA_RES = tuple((w, re.compile(rf"(\d+) {w}")) for w in ("failed", "errors?", "skipped", "xfailed", "xpassed"))

# node --test 摘要（spec/TAP 两种 reporter 均兼容）："tests 2" / "pass 2" / "fail 0"
_NODE_TOTAL_RE = re.compile(r"tests\s+(\d+)")
_NODE_PASS_RE = re.compile(r"pass\s+(\d+)")


def _severity_value(issue: Issue) -> str:
    return issue.severity.value if isinstance(issue.severity, Severity) else str(issue.severity)


def _select_issues(issues: list[Issue], max_patches: int) -> list[Issue]:
    """选出待修复 Issue：severity ∈ {critical, high}，按 (severity, -confidence) 排序截断。"""
    candidates = [i for i in issues if _severity_value(i) in _SEVERITY_RANK]
    candidates.sort(key=lambda i: (_SEVERITY_RANK[_severity_value(i)], -float(i.confidence or 0.0)))
    limit = max(0, int(max_patches))
    return candidates[:limit]


def _parse_test_counts(stdout: str, stderr: str, framework: str = "") -> tuple[int, int]:
    """从 pytest/jest/node --test 输出解析 (运行数, 通过数)；解析不出返回 (0, 0)。

    W7-A3：node-test（node --test）摘要形如 "ℹ tests 2 / ℹ pass 2 / ℹ fail 0"
    （spec reporter）或 "# tests 2 / # pass 2"（TAP reporter），两种均按
    "tests N" / "pass N" 宽松匹配。
    """
    text = f"{stdout}\n{stderr}"
    if str(framework or "").strip().lower() == "node-test":
        total_match = _NODE_TOTAL_RE.search(text)
        pass_match = _NODE_PASS_RE.search(text)
        total = int(total_match.group(1)) if total_match else 0
        passed = int(pass_match.group(1)) if pass_match else 0
        return total, passed
    passed_match = _TEST_PASSED_RE.search(text)
    passed = int(passed_match.group(1)) if passed_match else 0
    total_match = _TEST_TOTAL_RE.search(text)  # jest 风格："N passed, M total"
    if total_match:
        return int(total_match.group(1)), passed
    run = passed
    for _, pattern in _TEST_EXTRA_RES:
        m = pattern.search(text)
        if m:
            run += int(m.group(1))
    return run, passed


def _read_backup(workspace: Any, rel_path: str) -> bytes | None:
    """备份补丁应用前的原文件字节；文件不存在（新增文件场景）返回 None。"""
    try:
        return workspace.abs_path(rel_path).read_bytes()
    except OSError:
        return None


def _restore_backups(workspace: Any, backups: dict[str, bytes | None]) -> None:
    """回滚 = 还原全部 diff 目标文件的备份内容（R1-5：消除部分文件残留）。

    尽力而为（单个文件回滚失败不抛，Patch 终态已反映问题）；
    备份为 None 表示补丁新建的文件，回滚即删除。
    """
    for rel_path, backup in backups.items():
        target = workspace.abs_path(rel_path)
        try:
            if backup is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(backup)
        except OSError:
            continue


@dataclass
class _IssueRun:
    """单个 Issue 的修复运行状态（供异常兜底回滚/补录 Patch 使用）。"""

    issue: Issue
    terminal: bool = False  # 是否已落入终态桶（防统计重复计数）
    backups: dict[str, bytes | None] = field(default_factory=dict)  # diff 全部目标文件 → 原字节
    diff: str | None = None
    rationale: str = ""
    patch: Patch | None = None
    applied_ok: bool = False  # diff 是否已成功落盘（决定异常兜底是否需要回滚）
    events: list = field(default_factory=list)


async def run_fix_stage(ctx: PipelineContext) -> None:
    """Stage5 入口：编排层 config.do_fix 时调用（契约 v1.2 §3.2）。

    产出：
    - ctx.patches 追加 Patch（含 diff/rationale/apply_status/tests_run/tests_passed）；
    - 对应 Issue 更新 fix_status（FixStatus 枚举）与 patch_id；
    - ctx.extra["fix_stats"] = {"attempted","patch_generated","applied","verified",
      "syntax_ok","needs_review","failed"}（applied 只统计最终保留的应用成功数）；
    - 每步 ctx.emit("fix", ...) 进度事件，结束时发汇总事件。
    """
    stats: dict[str, int] = dict.fromkeys(_STATS_KEYS, 0)
    issues = _select_issues(ctx.issues, ctx.config.fix_max_patches)
    ctx.extra["fix_stats"] = stats
    await ctx.emit(
        "fix",
        f"修复阶段开始：候选 {len(issues)} 个（critical/high，上限 {ctx.config.fix_max_patches}）",
        total=len(issues),
    )
    if not issues:
        await ctx.emit("fix", f"修复阶段完成：无可修复问题 {stats}", **stats)
        return

    sandbox = SandboxExecutor()
    patch_seq = 0

    def _new_patch(
        state: _IssueRun,
        status: str,
        tests_run: int = 0,
        tests_passed: int = 0,
    ) -> Patch:
        nonlocal patch_seq
        patch_seq += 1
        patch = Patch(
            id=make_id("PATCH", patch_seq),
            issue_id=state.issue.id,
            diff=state.diff or "",
            rationale=state.rationale,
            apply_status=status,
            tests_run=tests_run,
            tests_passed=tests_passed,
        )
        ctx.patches.append(patch)
        state.patch = patch
        state.issue.patch_id = patch.id
        return patch

    async def _finalize(
        state: _IssueRun,
        *,
        bucket: str,
        status: str,
        fix_status: FixStatus,
        keep: bool,
        tests: tuple[int, int] = (0, 0),
        detail: str = "",
    ) -> None:
        """统一终态：记账 + 更新 Issue/Patch + 发事件（每个 Issue 恰好一次）。"""
        state.terminal = True
        stats[bucket] += 1
        if keep:
            stats["applied"] += 1
        else:
            _restore_backups(ctx.workspace, state.backups)
        patch = _new_patch(state, status, tests[0], tests[1])
        state.issue.fix_status = fix_status
        await ctx.emit(
            "fix",
            f"{state.issue.id} → {status}{('：' + detail) if detail else ''}",
            issue_id=state.issue.id,
            patch_id=patch.id,
        )

    for index, issue in enumerate(issues, 1):
        stats["attempted"] += 1
        await ctx.emit(
            "fix",
            f"[{index}/{len(issues)}] 尝试修复 {issue.id}（{issue.file}:{issue.line_start}）",
            current=index,
            total=len(issues),
            issue_id=issue.id,
        )
        state = _IssueRun(issue=issue)
        try:
            await _fix_one(ctx, state, sandbox, _finalize)
        except Exception as exc:  # noqa: BLE001 —— 单 Issue 失败不阻断后续
            if not state.terminal:
                stats["failed"] += 1
                state.terminal = True
            if state.applied_ok:  # 已落盘的补丁必须回滚，保证工作副本不被半成品污染
                _restore_backups(ctx.workspace, state.backups)
            if state.patch is None and state.diff is not None:
                _new_patch(state, "failed")
            if state.patch is not None:
                issue.fix_status = FixStatus.NEEDS_REVIEW
                issue.patch_id = state.patch.id
                if state.applied_ok:
                    # R1-18：回滚事实同步到 Patch 状态与事件文案
                    state.patch.apply_status = "needs-review"
            rolled = "（已回滚）" if state.applied_ok else ""
            await ctx.emit(
                "fix",
                f"{issue.id} 修复流程异常{rolled}：{type(exc).__name__}: {exc}",
                issue_id=issue.id,
                error=True,
            )

    ctx.extra["fix_stats"] = stats
    await ctx.emit(
        "fix",
        f"修复阶段完成：attempted={stats['attempted']} verified={stats['verified']} "
        f"syntax_ok={stats['syntax_ok']} needs_review={stats['needs_review']} "
        f"failed={stats['failed']} patches={len(ctx.patches)}",
        **stats,
    )


async def _fix_one(ctx: PipelineContext, state: _IssueRun, sandbox: SandboxExecutor, finalize: Any) -> None:
    """单个 Issue 的修复闭环：generate → validate → check/apply → syntax → tests。"""
    issue = state.issue

    # 1) 生成补丁（失败 → failed，无 Patch 产物）
    generated = await generate_patch(ctx.llm, issue, ctx.workspace, ctx.index)
    if generated is None:
        stats = ctx.extra["fix_stats"]
        stats["failed"] += 1
        state.terminal = True
        await ctx.emit("fix", f"{issue.id} 未能生成补丁（LLM 输出不可解析）", issue_id=issue.id)
        return
    state.diff, state.rationale = generated
    ctx.extra["fix_stats"]["patch_generated"] += 1
    await ctx.emit(
        "fix",
        f"{issue.id} 补丁已生成（diff {len(state.diff.splitlines())} 行）",
        issue_id=issue.id,
    )

    # 2) 路径安全校验（非法 → needs-review，跳过应用）
    errors = validate_diff(state.diff, ctx.workspace)
    if errors:
        await finalize(
            state,
            bucket="needs_review",
            status="needs-review",
            fix_status=FixStatus.NEEDS_REVIEW,
            keep=False,
            detail="diff 校验失败：" + "；".join(errors),
        )
        return

    # 3) 备份 + git apply --check + git apply
    # R1-5：按 diff 全部目标文件备份（复用 patcher._diff_targets），
    # 回滚时才能完整还原多文件补丁，消除"部分文件残留"。
    state.backups = {t: _read_backup(ctx.workspace, t) for t in _diff_targets(state.diff)}
    ok, reason = apply_diff(ctx.workspace, state.diff, check_only=True)
    if not ok:
        await finalize(
            state,
            bucket="needs_review",
            status="needs-review",
            fix_status=FixStatus.NEEDS_REVIEW,
            keep=False,
            detail=truncate(reason, 200),
        )
        return
    ok, reason = apply_diff(ctx.workspace, state.diff, check_only=False)
    if not ok:
        await finalize(
            state,
            bucket="needs_review",
            status="needs-review",
            fix_status=FixStatus.NEEDS_REVIEW,
            keep=False,
            detail=truncate(reason, 200),
        )
        return
    state.applied_ok = True
    await ctx.emit("fix", f"{issue.id} 补丁已应用", issue_id=issue.id)

    # 4) 语法重解析（失败 → 回滚 + needs-review）
    if not syntax_ok(ctx.workspace, issue.file):
        await finalize(
            state,
            bucket="needs_review",
            status="needs-review",
            fix_status=FixStatus.NEEDS_REVIEW,
            keep=False,
            detail="补丁后语法解析失败，已回滚",
        )
        return
    await ctx.emit("fix", f"{issue.id} 语法校验通过", issue_id=issue.id)

    # 5) 现有测试：无 → syntax-ok；全绿 → verified；失败/超时 → 回滚 + needs-review
    framework = find_existing_tests(ctx.workspace)
    if framework is None:
        await finalize(
            state,
            bucket="syntax_ok",
            status="syntax-ok",
            fix_status=FixStatus.SYNTAX_OK,
            keep=True,
            detail="无现有测试",
        )
        return
    result: SandboxResult = await run_existing_tests(sandbox, ctx.workspace, framework)
    tests_run, tests_passed = _parse_test_counts(result.stdout_tail, result.stderr_tail, framework)
    all_green = result.exit_code == 0 and not result.timed_out
    no_tests_collected = framework == "pytest" and result.exit_code == _PYTEST_NO_TESTS_EXIT
    if framework == "node-test":
        # node --test 无匹配测试文件时 exit 0 且 tests 0：无法证伪，降级 syntax-ok
        no_tests_collected = no_tests_collected or (
            result.exit_code == 0 and not result.timed_out and tests_run == 0
        )
    await ctx.emit(
        "fix",
        f"{issue.id} 现有测试（{framework}）运行完成：exit={result.exit_code} "
        f"passed={tests_passed}/{tests_run} timed_out={result.timed_out}",
        issue_id=issue.id,
    )
    if all_green:
        await finalize(
            state,
            bucket="verified",
            status="verified",
            fix_status=FixStatus.VERIFIED,
            keep=True,
            tests=(tests_run, tests_passed),
            detail=f"{framework} 全绿",
        )
        return
    if no_tests_collected:  # 测试目录存在但未收集到用例：无法证伪，降级 syntax-ok
        await finalize(
            state,
            bucket="syntax_ok",
            status="syntax-ok",
            fix_status=FixStatus.SYNTAX_OK,
            keep=True,
            detail=f"{framework} 未收集到用例",
        )
        return
    await finalize(
        state,
        bucket="needs_review",
        status="needs-review",
        fix_status=FixStatus.NEEDS_REVIEW,
        keep=False,
        tests=(tests_run, tests_passed),
        detail=truncate(f"{framework} 未通过（已回滚）：{result.stdout_tail or result.stderr_tail}", 300),
    )
