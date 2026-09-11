"""CodeAudit Agent 离线全闭环演示：python demo/run_demo.py

不依赖网络与 GLM_API_KEY，一键跑通「检测 → verified Patch → 生成单测 → 三格式报告」。

原理（不修改 audit 包，全部在本脚本内完成）：
- 靶项目 demo/mini_app 每次运行前复制到 demo/.demo_work/（幂等，不动原件）；
- 审计阶段关闭 LLM 审查/复核（enable_llm_review=False、enable_verify=False），
  检测走纯静态规则通道；
- 通过替换 audit.orchestrator.pipeline._make_llm 这一装配缝隙，把流水线使用的
  LLM 客户端注入为 DemoScriptedLLM（脚本化 FakeLLM，按 System Prompt 分发）：
    * Fix Agent 调用（"修复工程师"）   → 返回 SQL 参数化修复的 unified diff；
    * TestGen Agent 调用（"测试工程师"）→ 返回针对 find_user 的 pytest 用例；
    * 其余调用                           → 返回空响应（与 FakeLLMClient 脚本耗尽语义一致）。
- Patch 的验证（git apply → tree-sitter 语法重解析 → 沙箱运行现有测试）与
  生成单测的沙箱运行都是真实执行，只有 LLM 的"思考"被脚本替代。
"""

from __future__ import annotations

import asyncio
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

DEMO_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DEMO_DIR.parent
MINI_APP_DIR = DEMO_DIR / "mini_app"
WORK_ROOT = DEMO_DIR / ".demo_work"

DEFECT_FILE = "store.py"
DEFECT_LINE = 6
TARGET_SYMBOL = "find_user"

# ---------------------------------------------------------------- 脚本化 LLM 的"答案"

# Fix Agent 脚本：与 demo/mini_app/store.py 逐字节对齐的 unified diff（4→4 行，保持切片区间不变）
FIX_DIFF = (
    "diff --git a/store.py b/store.py\n"
    "--- a/store.py\n"
    "+++ b/store.py\n"
    "@@ -4,4 +4,4 @@\n"
    " def find_user(conn, username):\n"
    '     """按用户名查询用户记录。"""\n'
    "-    query = \"SELECT * FROM users WHERE name = '\" + username + \"'\"\n"
    "-    return conn.execute(query).fetchall()\n"
    '+    query = "SELECT * FROM users WHERE name = ?"\n'
    "+    return conn.execute(query, (username,)).fetchall()\n"
)

FIX_RATIONALE = (
    "把用户名从 SQL 字符串拼接改为占位符 ? + 参数元组，由驱动层完成转义，"
    "彻底消除注入面；函数签名与返回值不变，现有调用方（main.py、tests/test_store.py）无需修改。"
)

# TestGen Agent 脚本：针对修复后的 find_user 的回归用例（含注入载荷用例，真跑必须全绿）
TESTGEN_CODE = '''import sqlite3

from store import find_user


def _memory_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE users (name TEXT PRIMARY KEY, role TEXT)")
    conn.execute("INSERT INTO users VALUES ('alice', 'admin')")
    conn.execute("INSERT INTO users VALUES ('bob', 'viewer')")
    conn.commit()
    return conn


# kind: normal
def test_find_user_returns_matching_row():
    rows = find_user(_memory_conn(), "alice")
    assert [tuple(r) for r in rows] == [("alice", "admin")]


# kind: normal
def test_find_user_matches_exact_name_only():
    rows = find_user(_memory_conn(), "bob")
    assert len(rows) == 1
    assert rows[0][1] == "viewer"


# kind: boundary
def test_find_user_unknown_name_returns_empty():
    assert find_user(_memory_conn(), "nobody") == []


# kind: boundary
def test_find_user_empty_name_returns_empty():
    assert find_user(_memory_conn(), "") == []


# kind: error
def test_find_user_treats_injection_payload_as_literal():
    # 修复前该载荷会让 WHERE 恒真并返回全表；修复后按字面值匹配，必须为空
    rows = find_user(_memory_conn(), "x' OR '1'='1")
    assert rows == []
'''


# ---------------------------------------------------------------- 输出工具

_WIDTH = 72


def _banner(title: str) -> None:
    print()
    print("=" * _WIDTH)
    print(f" {title}")
    print("=" * _WIDTH)


def _step(no: int, total: int, title: str, note: str = "") -> None:
    print()
    print(f"[步骤 {no}/{total}] {title}")
    if note:
        print(f"  {note}")
    print("-" * _WIDTH)


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.rstrip("\n").splitlines())


def _numbered(text: str, mark_line: int | None = None) -> str:
    lines = []
    for no, line in enumerate(text.rstrip("\n").splitlines(), 1):
        marker = "  <-- 缺陷行" if no == mark_line else ""
        lines.append(f"    {no:3d} | {line}{marker}")
    return "\n".join(lines)


def _rel(path: Path | str) -> str:
    """相对项目根的 posix 路径（打印用）。"""
    try:
        return Path(path).resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


# 事件去重：剥掉末尾的全角括号补充说明后再比对（如"检测完成：N 个问题（模式：rule）"）
_DUP_SUFFIX_RE = re.compile(r"（[^）]*）$")


# ---------------------------------------------------------------- 脚本化 LLM


def _make_scripted_llm() -> Any:
    """构造按 System Prompt 分发的脚本化 LLM（延迟导入 audit，保证本模块可被单测导入）。"""
    import json

    from audit.llm.base import LLMClient, LLMResponse

    class DemoScriptedLLM(LLMClient):
        """离线演示专用：不看顺序、只看调用方身份，避免脚本顺序依赖。"""

        def __init__(self) -> None:
            self.calls: dict[str, int] = {"fix": 0, "testgen": 0, "other": 0}

        async def chat(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None = None,
            json_mode: bool = False,
            temperature: float = 0.2,
        ) -> LLMResponse:
            system = next((str(m.get("content", "")) for m in messages if m.get("role") == "system"), "")
            if "修复工程师" in system:
                self.calls["fix"] += 1
                payload = {"diff": FIX_DIFF, "rationale": FIX_RATIONALE}
                return LLMResponse(content=json.dumps(payload, ensure_ascii=False), model="demo-scripted")
            if "测试工程师" in system:
                self.calls["testgen"] += 1
                return LLMResponse(content=f"```python\n{TESTGEN_CODE}```", model="demo-scripted")
            self.calls["other"] += 1
            return LLMResponse(content="", model="demo-scripted")

        def usage_totals(self) -> dict[str, int]:
            return {"llm_calls": sum(self.calls.values()), "prompt_tokens": 0, "completion_tokens": 0}

    return DemoScriptedLLM()


# ---------------------------------------------------------------- 演示步骤


def _prepare_workdir() -> Path:
    """幂等：清空 demo/.demo_work 并复制靶项目，返回副本路径。"""
    if WORK_ROOT.exists():
        shutil.rmtree(WORK_ROOT)
    project_copy = WORK_ROOT / "mini_app"
    shutil.copytree(MINI_APP_DIR, project_copy, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    return project_copy


def _show_target(project_copy: Path) -> None:
    files = sorted(p for p in project_copy.rglob("*.py") if "__pycache__" not in p.parts)
    print(f"  靶项目：{_rel(MINI_APP_DIR)}（本次运行副本：{_rel(project_copy)}）")
    for path in files:
        print(f"    - {path.relative_to(project_copy).as_posix()}")
    print()
    print(f"  缺陷代码 {DEFECT_FILE}（第 {DEFECT_LINE} 行以字符串拼接构造 SQL，可被注入）：")
    print(_numbered((project_copy / DEFECT_FILE).read_text(encoding="utf-8"), DEFECT_LINE))


async def _run_pipeline(project_copy: Path, llm: Any) -> Any:
    """跑完整七阶段流水线；LLM 通过替换 _make_llm 装配缝隙注入。"""
    import audit.orchestrator.pipeline as pipeline_module
    from audit.config import AuditConfig

    config = AuditConfig(
        source_path=str(project_copy),
        work_root=str(WORK_ROOT / "audit"),
        out_dir=str(WORK_ROOT / "reports"),
        api_key="offline-demo",  # 仅用于让 llm_available=True 走 fix/testgen；不会发起任何网络请求
        enable_llm_review=False,  # 审查通道关闭 → 纯规则检测
        enable_verify=False,
        do_fix=True,
        do_tests=True,
    )

    seen_event_keys: set[tuple[str, str]] = set()

    async def emitter(event: dict[str, Any]) -> None:
        if event.get("type") != "progress":
            return
        message = str(event.get("message", ""))
        if message.startswith("阶段 ") and message.endswith(("开始", "完成")):
            return  # 形式化的阶段起止事件不打印，保留有信息量的事件
        stage = str(event.get("stage", "?"))
        key = (stage, _DUP_SUFFIX_RE.sub("", message))  # 包内同一阶段会发两条近似事件，去重
        if key in seen_event_keys:
            return
        seen_event_keys.add(key)
        flag = " !!" if event.get("error") else (" (warning)" if event.get("warning") else "")
        print(f"  [{stage:<10}] {message}{flag}")

    original_make_llm = pipeline_module._make_llm
    pipeline_module._make_llm = lambda _config: (llm, None)  # 依赖注入：整条流水线使用脚本化 LLM
    try:
        return await pipeline_module.run_audit(config, emitter)
    finally:
        pipeline_module._make_llm = original_make_llm


def _show_detection(report: Any) -> None:
    summary = report.summary
    print(f"  项目：{report.project_name}   工作副本 {report.stats.files_total} 个文件 / {report.loc} 行"
          "（报告阶段现场统计，已含 tests/generated/ 下新生成的单测文件）")
    print(f"  健康分：{report.health_score}   （critical={summary.get('critical', 0)} "
          f"high={summary.get('high', 0)} medium={summary.get('medium', 0)} low={summary.get('low', 0)}）")
    print("  注：健康分按每千行加权问题密度计算，迷你项目基数极小，一条 critical 即可压到 0 分，属口径正常。")
    print()
    for issue in report.issues:
        print(f"  {issue.id}  [{issue.severity.value}/{issue.category.value}]  "
              f"{issue.file}:{issue.line_start}  置信度 {issue.confidence:.0%}  来源 {issue.source.value}")
        print(f"      {issue.title}")
        print(f"      证据：{'; '.join(issue.evidence)}")


def _show_patches(report: Any) -> bool:
    if not report.patches:
        print("  （未生成任何 Patch）")
        return False
    verified = False
    issues_by_id = {issue.id: issue for issue in report.issues}
    for patch in report.patches:
        issue = issues_by_id.get(patch.issue_id)
        fix_status = issue.fix_status.value if issue is not None else "-"
        print(f"  {patch.id}  修复 {patch.issue_id}   apply_status = {patch.apply_status}   "
              f"现有测试 {patch.tests_passed}/{patch.tests_run} 通过   Issue.fix_status = {fix_status}")
        print(f"  说明：{patch.rationale}")
        print()
        print(_indent(patch.diff))
        verified = verified or patch.apply_status == "verified"
    return verified


def _find_src_root() -> Path | None:
    """定位本次审计的工作副本（demo/.demo_work/audit/<audit_id>/src）。"""
    candidates = sorted((WORK_ROOT / "audit").glob("*/src"))
    return candidates[0] if candidates else None


async def _show_tests(report: Any) -> bool:
    from audit.sandbox import SandboxExecutor

    if not report.test_cases:
        print("  （未生成任何单测）")
        return False
    passed = [tc for tc in report.test_cases if tc.status == "passed"]
    for tc in report.test_cases:
        print(f"  {tc.id}  目标 {tc.target}   status = {tc.status}   kind = {tc.kind}   assert {tc.assert_count} 条   文件 {tc.file}")
    src_root = _find_src_root()
    if not passed or src_root is None:
        return False
    gen_file = passed[0].file
    gen_path = src_root / gen_file
    if gen_path.is_file():
        print()
        print(f"  生成文件 {_rel(gen_path)}：")
        print(_indent(gen_path.read_text(encoding="utf-8")))
    print()
    print("  在沙箱中重放该生成文件（与流水线内运行方式一致：python -m pytest <文件> -q）：")
    result = await SandboxExecutor().run_tests("pytest", src_root, gen_file)
    tail = (result.stdout_tail or result.stderr_tail).rstrip().splitlines()[-4:]
    print(_indent("\n".join(tail)))
    print(f"    exit_code = {result.exit_code}   timed_out = {result.timed_out}   耗时 {result.duration_sec}s")
    return result.exit_code == 0 and not result.timed_out


def _show_reports() -> None:
    out_dir = WORK_ROOT / "reports"
    for fmt in ("json", "md", "html"):
        path = out_dir / f"report.{fmt}"
        state = f"{path.stat().st_size} 字节" if path.is_file() else "缺失"
        print(f"  {fmt:<5} {_rel(path)}   ({state})")


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.path.insert(0, str(PROJECT_ROOT))

    total = 6
    started = time.monotonic()
    _banner("CodeAudit Agent 离线全闭环演示（无网络 · 无 API Key · 脚本化 LLM）")
    print("  流程：检测（纯规则）→ 修复 Patch（git apply + 语法重解析 + 现有测试）→ 生成单测（沙箱运行）→ 报告")

    _step(1, total, "靶项目与缺陷代码", "每次运行在 demo/.demo_work/ 下重建副本，demo/mini_app 原件不被修改。")
    project_copy = _prepare_workdir()
    _show_target(project_copy)

    _step(2, total, "运行七阶段审计流水线",
          "注入脚本化 LLM：审查/复核关闭走纯规则；Fix 返回参数化修复 diff；TestGen 返回 pytest 用例。")
    llm = _make_scripted_llm()
    report = await _run_pipeline(project_copy, llm)
    print(f"  脚本化 LLM 调用次数：fix={llm.calls['fix']}  testgen={llm.calls['testgen']}  other={llm.calls['other']}")

    _step(3, total, "检测摘要")
    _show_detection(report)

    _step(4, total, "修复 Patch 与验证结果", "验证链：validate_diff → git apply --check/apply → tree-sitter 重解析 → 沙箱运行现有测试。")
    patch_ok = _show_patches(report)

    _step(5, total, "生成的单测与运行结果", "目标函数取自 verified Patch 的 hunk 与索引切片的交集；失败会带报错重试（≤2 次），仍失败则剔除。")
    tests_ok = await _show_tests(report)

    _step(6, total, "审计报告（三种格式）")
    _show_reports()

    elapsed = time.monotonic() - started
    _banner("演示结束")
    print(f"  Patch 状态：{'verified' if patch_ok else '未达 verified'}   "
          f"生成单测：{'通过' if tests_ok else '未通过'}   总耗时 {elapsed:.1f}s")
    if patch_ok and tests_ok:
        print("  离线演示完成，配置 GLM_API_KEY 后可跑真实 LLM 全流程：")
        print("    python cli.py run demo/mini_app --fix --tests --review-mode tools")
        return 0
    print("  演示未达到预期结果，请检查上方阶段输出（git 是否可用、pytest 是否安装）。")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
