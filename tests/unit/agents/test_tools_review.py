"""W2-A3 单测：工具取证式审查（make_tools_review_fn）、大文件切片、批量小切片与文件级并发。

全部基于 FakeLLMClient 脚本驱动，零网络、零真实 LLM。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from audit.agents.prompts import PROMPT_VERSION
from audit.agents.review import (
    TOOLS_REVIEW_TOOL_NAMES,
    make_tools_review_fn,
    review_file,
    review_files_parallel,
)
from audit.config import AuditConfig
from audit.indexer import create_index
from audit.llm.base import FakeLLMClient, _normalize_script_item
from audit.models import IssueSource
from audit.pipeline import PipelineContext
from audit.workspace import WorkspaceContext

FILE = "app/services/orders.py"


def _payload(file: str = FILE, line: int = 2, **overrides: Any) -> dict:
    data = {
        "category": "bug",
        "severity": "high",
        "title": "get_user 返回值未判空即访问属性",
        "file": file,
        "line_start": line,
        "line_end": line,
        "description": "uid 不存在时 get_user 返回 None，后续访问属性会崩",
        "evidence": [f"{file}:{line} 调用 get_user"],
        "suggestion": "增加判空分支",
        "confidence": 0.9,
    }
    data.update(overrides)
    return data


def _write_files(src: Path, files: dict[str, str]) -> WorkspaceContext:
    for rel, content in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return WorkspaceContext(
        audit_id="w2a3001",
        src_root=src,
        work_root=src.parent,
        db_path=src.parent / "index.db",
    )


def _make_ctx(
    tmp_path: Path,
    files: dict[str, str],
    llm: FakeLLMClient,
    index: Any = None,
    **config_overrides: Any,
) -> PipelineContext:
    workspace = _write_files(tmp_path / "src", files)
    config = AuditConfig(source_path=str(workspace.src_root))
    for k, v in config_overrides.items():
        setattr(config, k, v)

    async def emit(event: dict) -> None:
        return None

    return PipelineContext(config=config, workspace=workspace, llm=llm, emitter=emit, index=index)


def _small_file(name: str) -> str:
    return f'"""{name}：小文件。"""\n\n\ndef {name.replace(".", "_")}_run(x):\n    return x + 1\n'


# ---------------------------------------------------------------- 工具取证路径


class TestToolsReviewFn:
    async def test_read_file_then_record_issues(self, sample_workspace, fake_emitter):
        """脚本：先 read_file 取证，再 record_issues 提交 1 条合法 issue。"""
        llm = FakeLLMClient(
            [
                {"tool_calls": [{"name": "read_file", "arguments": {"path": FILE}}]},
                {"tool_calls": [{"name": "record_issues", "arguments": {"issues": [_payload()]}}]},
            ]
        )
        ctx = PipelineContext(
            config=AuditConfig(source_path=str(sample_workspace.src_root)),
            workspace=sample_workspace,
            llm=llm,
            emitter=fake_emitter,
        )
        review_fn = make_tools_review_fn(ctx)
        issues = await review_fn(sample_workspace, FILE, [])
        assert len(issues) == 1
        assert issues[0].source == IssueSource.LLM
        assert issues[0].file == FILE and issues[0].line_start == 2
        # prompt 版本记录
        assert ctx.extra["prompt_version"] == PROMPT_VERSION == "v2"
        # 工具集：只读 7 个 + record_issues（恰好一次收口）
        tool_names = {t["function"]["name"] for t in llm.calls[0]["tools"]}
        assert tool_names == set(TOOLS_REVIEW_TOOL_NAMES) | {"record_issues"}
        # read_file 真实读取了工作区文件（工具结果回填可见）
        tool_msgs = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
        assert any("orders.py" in m["content"] for m in tool_msgs)

    async def test_out_of_range_line_rejected_then_retry(self, sample_workspace, fake_emitter):
        """record_issues 提交越界行号被工具层拒绝（返回 error），脚本补一次合法提交。"""
        llm = FakeLLMClient(
            [
                {"tool_calls": [{"name": "record_issues", "arguments": {"issues": [_payload(line=99999)]}}]},
                {"tool_calls": [{"name": "record_issues", "arguments": {"issues": [_payload()]}}]},
            ]
        )
        ctx = PipelineContext(
            config=AuditConfig(source_path=str(sample_workspace.src_root)),
            workspace=sample_workspace,
            llm=llm,
            emitter=fake_emitter,
        )
        issues = await make_tools_review_fn(ctx)(sample_workspace, FILE, [])
        assert len(issues) == 1  # 只有补交的合法条目落库
        # 越界批次被拒的错误以 role=tool 消息回填，Agent 可读到失败原因
        tool_msgs = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
        assert "校验失败" in tool_msgs[0]["content"]
        assert "99999" in tool_msgs[0]["content"]

    async def test_token_budget_formula(self, sample_workspace, fake_emitter, monkeypatch):
        """AgentLimits.token_budget = min(200_000, ctx.config.token_budget // 10)。"""
        captured: dict[str, Any] = {}

        async def fake_run(self, system_prompt, messages, limits=None, on_event=None):
            captured["limits"] = limits
            # 模拟正常收口（R1-26：不调用 record_issues 的 runtime 结束会被判非正常）
            await self.get_tool("record_issues").handler(issues=[])
            from audit.agent.base import AgentResult

            return AgentResult()

        monkeypatch.setattr("audit.agent.runtime.SimpleAgentRuntime.run", fake_run)
        config = AuditConfig(source_path=str(sample_workspace.src_root), token_budget=1_000_000)
        ctx = PipelineContext(config=config, workspace=sample_workspace, llm=FakeLLMClient(), emitter=fake_emitter)
        await make_tools_review_fn(ctx)(sample_workspace, FILE, [])
        assert captured["limits"].max_iterations == 12
        assert captured["limits"].token_budget == 100_000

        config2 = AuditConfig(source_path=str(sample_workspace.src_root), token_budget=20_000_000)
        ctx2 = PipelineContext(config=config2, workspace=sample_workspace, llm=FakeLLMClient(), emitter=fake_emitter)
        await make_tools_review_fn(ctx2)(sample_workspace, FILE, [])
        assert captured["limits"].token_budget == 200_000  # 封顶


# ---------------------------------------------------------------- 大文件切片


def _big_source(n_funcs: int = 40) -> str:
    lines: list[str] = []
    for i in range(n_funcs):
        lines.append(f"def func_{i}(x):")
        lines.append(f"    total = {i}")
        for j in range(10):
            lines.append(f"    total += x * {j}")
        lines.append("    return total")
        lines.append("")
    return "\n".join(lines)


class TestBigFileSlicing:
    async def test_slices_reviewed_and_merged(self, tmp_path: Path):
        """>400 行大文件走 grouped_slices 多片审查并合并去重。"""
        files = {"big.py": _big_source(40)}  # 40 × 15 行 ≈ 600 行
        llm = FakeLLMClient()
        ctx = _make_ctx(tmp_path, files, llm)
        store = create_index(ctx.workspace)
        store.build()
        ctx.index = store

        slices = store.grouped_slices("big.py", 400)
        assert len(slices) >= 2  # 前置：确有多片

        # 每片提交 1 条位于该片内的合法 issue（行号取片首 +2，彼此相距远不会合并）
        script = [
            {
                "tool_calls": [
                    {
                        "name": "record_issues",
                        "arguments": {
                            "issues": [
                                _payload(file="big.py", line=sl.line_start + 2, title=f"切片问题{i}")
                            ]
                        },
                    }
                ]
            }
            for i, sl in enumerate(slices)
        ]
        llm._script = [_normalize_script_item(item, i) for i, item in enumerate(script)]  # type: ignore[attr-defined]
        llm._cursor = 0  # type: ignore[attr-defined]

        issues = await make_tools_review_fn(ctx)(ctx.workspace, "big.py", [])
        assert len(llm.calls) == 2 * len(slices)  # 每片：record_issues 调用 + 收尾
        assert len(issues) == len(slices)
        assert all(i.source == IssueSource.LLM for i in issues)
        # 每个切片的 System Prompt 都带切片标注（[切片 i/N] + 绝对行号说明）
        slice_markers = sum(
            1
            for c in llm.calls
            if any("[切片 " in m.get("content", "") for m in c["messages"] if m.get("role") == "system")
        )
        assert slice_markers >= len(slices)

    async def test_index_none_falls_back_to_truncated_whole_file(self, tmp_path: Path):
        """index 为 None 时整文件截断审查（前 400 行 + 截断标记）。"""
        files = {"big.py": _big_source(40)}
        llm = FakeLLMClient(
            [
                {"tool_calls": [{"name": "record_issues", "arguments": {"issues": [_payload(file="big.py", line=3)]}}]},
            ]
        )
        ctx = _make_ctx(tmp_path, files, llm, index=None)
        issues = await make_tools_review_fn(ctx)(ctx.workspace, "big.py", [])
        assert len(issues) == 1
        system_text = llm.calls[0]["messages"][0]["content"]
        assert "截断" in system_text and "400" in system_text
        assert len(llm.calls) == 2  # record_issues + 收尾，未发生切片


# ---------------------------------------------------------------- 批量小切片


class TestBatchSmallSlices:
    async def test_three_small_files_batch_into_one_call(self, tmp_path: Path):
        """3 个无 hint 小文件 batch 成 1 次 LLM 调用（json 合并审查）。"""
        files = {f"m{i}.py": _small_file(f"mod{i}") for i in range(3)}
        llm = FakeLLMClient(
            [{"content": json.dumps({"issues": [_payload(file="m0.py", line=4)]})}]
        )
        ctx = _make_ctx(tmp_path, files, llm)
        review_fn = make_tools_review_fn(ctx)
        results = await review_files_parallel(ctx, [(rel, []) for rel in files], review_fn=review_fn)
        assert len(llm.calls) == 1  # 3 文件 → 1 次调用
        assert llm.calls[0]["json_mode"] is True
        assert len(results["m0.py"]) == 1
        assert results["m0.py"][0].file == "m0.py"
        assert results["m1.py"] == [] and results["m2.py"] == []
        # 组合源码带显式文件分隔标记
        system_text = llm.calls[0]["messages"][0]["content"]
        for rel in files:
            assert f"{rel}（" in system_text
        assert "===== 文件 1/3" in system_text and "===== 文件 3/3" in system_text

    async def test_group_size_of_five(self, tmp_path: Path):
        """7 个小文件按 5 个一组 → 2 次调用；输出按文件分组回填。"""
        files = {f"s{i}.py": _small_file(f"part{i}") for i in range(7)}
        rels = list(files)
        llm = FakeLLMClient(
            [
                {"content": json.dumps({"issues": [_payload(file=rels[0], line=4)]})},
                {"content": json.dumps({"issues": [_payload(file=rels[5], line=4)]})},
            ]
        )
        ctx = _make_ctx(tmp_path, files, llm)
        results = await review_files_parallel(ctx, [(rel, []) for rel in rels], review_fn=make_tools_review_fn(ctx))
        assert len(llm.calls) == 2
        assert len(results[rels[0]]) == 1 and len(results[rels[5]]) == 1
        assert all(results[r] == [] for r in (rels[1], rels[2], rels[3], rels[4], rels[6]))

    async def test_hint_files_and_big_files_not_batched(self, tmp_path: Path):
        """有规则 hint 或 ≥80 行的文件不进批量组，走独立审查。"""
        big = "x = 0\n" * 90  # 90 行 ≥ 80
        files = {"a.py": _small_file("a"), "b.py": big}
        llm = FakeLLMClient()
        ctx = _make_ctx(tmp_path, files, llm)
        review_fn = make_tools_review_fn(ctx)
        await review_files_parallel(ctx, [("a.py", []), ("b.py", ["R-1|low|1-1|hint"])], review_fn=review_fn)
        assert len(llm.calls) == 2  # a.py 单独成组（1 次调用）+ b.py 独立审查（至少 1 次调用）

    async def test_batch_uses_review_file_extra_files(self, sample_workspace):
        """review_file 的 extra_files 参数：组合源码 + 分隔标记 + 按文件分组产出。"""
        llm = FakeLLMClient(
            [{"content": json.dumps({"issues": [_payload(file=FILE, line=2), _payload(file="app/config.py", line=3)]})}]
        )
        extra_source = sample_workspace.read_file_text("app/config.py")
        issues = await review_file(
            sample_workspace,
            None,
            llm,
            FILE,
            hints=[],
            extra_files=[("app/config.py", extra_source, [])],
        )
        assert len(llm.calls) == 1
        assert {i.file for i in issues} == {FILE, "app/config.py"}
        system_text = llm.calls[0]["messages"][0]["content"]
        assert "===== 文件 2/2: app/config.py" in system_text
        assert "批量小文件合并审查" in system_text


# ---------------------------------------------------------------- 文件级并发


class TestReviewFilesParallel:
    async def test_concurrency_peak_bounded(self, tmp_path: Path):
        """Semaphore(concurrency) 限流：在飞峰值 ≤ limit，且全部文件有结果。"""
        files = {f"p{i}.py": _small_file(f"p{i}") for i in range(8)}
        llm = FakeLLMClient()
        ctx = _make_ctx(tmp_path, files, llm, concurrency=3)
        state = {"inflight": 0, "peak": 0}

        async def review_fn(workspace, rel, hints):
            state["inflight"] += 1
            state["peak"] = max(state["peak"], state["inflight"])
            await asyncio.sleep(0.01)
            state["inflight"] -= 1
            return []

        results = await review_files_parallel(ctx, [(rel, []) for rel in files], review_fn=review_fn)
        assert state["peak"] <= 3
        assert state["peak"] > 1  # 确实发生了并发
        assert len(results) == 8

    async def test_single_file_error_degrades_and_records(self, tmp_path: Path):
        """单文件异常降级为空结果并记 ctx.extra['review_errors']。"""
        files = {f"e{i}.py": _small_file(f"e{i}") for i in range(3)}
        llm = FakeLLMClient()
        ctx = _make_ctx(tmp_path, files, llm)

        def review_fn(workspace, rel, hints):
            if rel == "e1.py":
                raise RuntimeError("llm down")
            return []

        results = await review_files_parallel(ctx, [(rel, []) for rel in files], review_fn=review_fn)
        assert "e1.py" not in results
        assert "e1.py" in ctx.extra["review_errors"]
        assert "llm down" in ctx.extra["review_errors"]["e1.py"]
        assert set(results) == {"e0.py", "e2.py"}

    async def test_group_error_records_all_members(self, tmp_path: Path):
        """批量组内审查异常时，组内每个文件都记录错误。"""
        files = {f"g{i}.py": _small_file(f"g{i}") for i in range(3)}
        llm = FakeLLMClient()
        ctx = _make_ctx(tmp_path, files, llm)
        review_fn = make_tools_review_fn(ctx)

        async def boom(*args, **kwargs):
            if kwargs.get("extra_files"):
                raise RuntimeError("disk gone")
            return []

        import audit.agents.review as review_mod

        original = review_mod.review_file
        review_mod.review_file = boom  # type: ignore[assignment]
        try:
            results = await review_files_parallel(ctx, [(rel, []) for rel in files], review_fn=review_fn)
        finally:
            review_mod.review_file = original  # type: ignore[assignment]
        assert results == {}
        assert set(ctx.extra["review_errors"]) == set(files)

    async def test_batch_disabled_without_marker(self, tmp_path: Path):
        """review_fn 非工具路径（无 _supports_extra_files 标记）时不做批量合并。"""
        files = {f"n{i}.py": _small_file(f"n{i}") for i in range(3)}
        llm = FakeLLMClient()
        ctx = _make_ctx(tmp_path, files, llm)
        calls: list[str] = []

        async def review_fn(workspace, rel, hints):
            calls.append(rel)
            return []

        await review_files_parallel(ctx, [(rel, []) for rel in files], review_fn=review_fn)
        assert len(llm.calls) == 0  # 未绕过注入的 review_fn 去走批量 json 路径
        assert sorted(calls) == sorted(files)


# ---------------------------------------------------------------- R1-26 回归


async def test_runtime_abnormal_end_raises_and_lands_in_review_errors(tmp_path: Path, fake_emitter):
    """R1-26：runtime 从未调用 record_issues 即结束 → 显式失败并记 review_errors。"""
    llm = FakeLLMClient([{"content": "我自己看完了，没问题。"}])  # 无任何工具调用
    ws = _write_files(tmp_path / "src", {"e9.py": "x = 1\n"})
    ctx = PipelineContext(
        config=AuditConfig(source_path=str(ws.src_root)),
        workspace=ws,
        llm=llm,
        emitter=fake_emitter,
    )
    results = await review_files_parallel(ctx, [("e9.py", [])], review_fn=make_tools_review_fn(ctx))
    assert results == {}  # 该文件无产出
    assert "e9.py" in ctx.extra["review_errors"]
    assert "record_issues" in ctx.extra["review_errors"]["e9.py"]
