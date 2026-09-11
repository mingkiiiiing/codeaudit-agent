"""Wave 3 消融开关接线测试：enable_symbol_context（契约 v1.3，docs/08 §3）。

False 时 review prompt 组装不附"依赖符号源码"上下文块（架构卡片与文件源码保留），
单文件 / 大文件切片 / 批量小切片三条路径一致生效；True（及 config=None 的直调
兼容路径）行为与现状一致。全部基于 FakeLLMClient 脚本驱动，零网络、零真实 LLM。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from audit.agents.review import (
    SYMBOL_CONTEXT_DISABLED_TEXT,
    make_tools_review_fn,
    review_file,
)
from audit.config import AuditConfig
from audit.indexer import create_index
from audit.llm.base import FakeLLMClient
from audit.models import Symbol
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


class FakeSymbolIndex:
    """返回带签名的固定符号表，供断言"符号上下文块是否出现"。"""

    def symbols_for_file(self, rel_path: str) -> list[Symbol]:
        return [
            Symbol(
                name="get_user",
                kind="function",
                file=rel_path,
                line_start=1,
                line_end=5,
                signature="def get_user(uid: int) -> dict | None",
            )
        ]

    def grouped_slices(self, rel_path: str, max_lines: int = 400) -> list[Any]:  # pragma: no cover
        raise NotImplementedError


def _write_files(src: Path, files: dict[str, str]) -> WorkspaceContext:
    for rel, content in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return WorkspaceContext(
        audit_id="w3sw001",
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


def _system_texts(llm: FakeLLMClient) -> list[str]:
    """收集全部调用中的 system 消息文本。"""
    return [
        m.get("content", "")
        for c in llm.calls
        for m in c["messages"]
        if m.get("role") == "system"
    ]


# ---------------------------------------------------------------- 直调路径（json mode）


class TestReviewFileConfigKwarg:
    async def test_symbols_included_by_default(self, sample_workspace):
        """config=None：向后兼容，行为不变（符号表照常附上）。"""
        llm = FakeLLMClient([{"content": json.dumps({"issues": [_payload()]})}])
        await review_file(sample_workspace, FakeSymbolIndex(), llm, FILE, hints=[], config=None)
        system_text = llm.calls[0]["messages"][0]["content"]
        assert "get_user(uid: int) -> dict | None" in system_text
        assert SYMBOL_CONTEXT_DISABLED_TEXT not in system_text

    async def test_symbols_included_when_enabled(self, sample_workspace):
        llm = FakeLLMClient([{"content": json.dumps({"issues": [_payload()]})}])
        config = AuditConfig(source_path=str(sample_workspace.src_root))
        await review_file(sample_workspace, FakeSymbolIndex(), llm, FILE, hints=[], config=config)
        system_text = llm.calls[0]["messages"][0]["content"]
        assert "get_user(uid: int) -> dict | None" in system_text

    async def test_symbols_omitted_when_disabled(self, sample_workspace):
        """False：不附符号源码块；架构卡片与文件源码保留。"""
        llm = FakeLLMClient([{"content": json.dumps({"issues": [_payload()]})}])
        config = AuditConfig(source_path=str(sample_workspace.src_root), enable_symbol_context=False)
        issues = await review_file(sample_workspace, FakeSymbolIndex(), llm, FILE, hints=[], config=config)
        assert len(issues) == 1  # 审查功能本身不受影响
        system_text = llm.calls[0]["messages"][0]["content"]
        assert "get_user(uid: int) -> dict | None" not in system_text
        assert SYMBOL_CONTEXT_DISABLED_TEXT in system_text
        # 架构卡片与带行号文件源码保留
        assert "<architecture_card>" in system_text
        assert "1: " in system_text

    async def test_batch_extra_files_symbols_omitted_when_disabled(self, sample_workspace):
        """批量小切片路径：False 时主文件与附加文件的符号块一并禁用。"""
        extras_rel = "app/services/users.py"
        llm = FakeLLMClient([{"content": json.dumps({"issues": [_payload()]})}])
        config = AuditConfig(source_path=str(sample_workspace.src_root), enable_symbol_context=False)
        await review_file(
            sample_workspace,
            FakeSymbolIndex(),
            llm,
            FILE,
            hints=[],
            extra_files=[(extras_rel, sample_workspace.read_file_text(extras_rel), [])],
            config=config,
        )
        system_text = llm.calls[0]["messages"][0]["content"]
        assert "get_user(uid: int) -> dict | None" not in system_text
        assert system_text.count(SYMBOL_CONTEXT_DISABLED_TEXT) == 2  # 主文件 + 附加文件
        # 文件源码块仍在（两个文件的分隔标记齐全）
        assert "===== 文件 1/2" in system_text and "===== 文件 2/2" in system_text


# ------------------------------------------------------------ 工具取证路径


class TestToolsReviewFnSymbolSwitch:
    async def test_tools_path_symbols_omitted_when_disabled(self, sample_workspace, fake_emitter):
        ctx = _make_ctx_dict_workspace(sample_workspace, fake_emitter, enable_symbol_context=False)
        llm = ctx.llm
        review_fn = make_tools_review_fn(ctx)
        await review_fn(sample_workspace, FILE, [])
        system_texts = _system_texts(llm)
        assert system_texts
        for text in system_texts:
            assert "get_user(uid: int) -> dict | None" not in text
            assert SYMBOL_CONTEXT_DISABLED_TEXT in text

    async def test_tools_path_symbols_kept_when_enabled(self, sample_workspace, fake_emitter):
        ctx = _make_ctx_dict_workspace(sample_workspace, fake_emitter)
        llm = ctx.llm
        await make_tools_review_fn(ctx)(sample_workspace, FILE, [])
        system_texts = _system_texts(llm)
        assert any("get_user(uid: int) -> dict | None" in t for t in system_texts)


def _make_ctx_dict_workspace(
    sample_workspace: WorkspaceContext, fake_emitter, **overrides: Any
) -> PipelineContext:
    """基于既有 sample_workspace 构造 PipelineContext（FakeSymbolIndex 注入 index）。"""
    config = AuditConfig(source_path=str(sample_workspace.src_root), **overrides)

    async def emit(event: dict) -> None:
        return None

    return PipelineContext(
        config=config,
        workspace=sample_workspace,
        llm=FakeLLMClient(
            [
                {"tool_calls": [{"name": "read_file", "arguments": {"path": FILE}}]},
                {"tool_calls": [{"name": "record_issues", "arguments": {"issues": [_payload()]}}]},
            ]
        ),
        emitter=emit,
        index=FakeSymbolIndex(),
    )


# ------------------------------------------------------------ 大文件切片路径


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


class TestSlicePathSymbolSwitch:
    async def test_slice_symbols_omitted_when_disabled(self, tmp_path: Path):
        """>400 行大文件分片审查时，每片的 system prompt 都不附符号上下文。"""
        files = {"big.py": _big_source(40)}
        llm = FakeLLMClient()
        ctx = _make_ctx(tmp_path, files, llm, enable_symbol_context=False)
        store = create_index(ctx.workspace)
        store.build()
        ctx.index = store

        slices = store.grouped_slices("big.py", 400)
        assert len(slices) >= 2  # 前置：确有多片
        script = [
            {
                "tool_calls": [
                    {
                        "name": "record_issues",
                        "arguments": {
                            "issues": [
                                {"file": "big.py", "line_start": sl.line_start + 2, "line_end": sl.line_start + 2,
                                 "title": f"切片问题{i}", "category": "bug", "severity": "medium",
                                 "description": "d", "evidence": [], "suggestion": "s", "confidence": 0.5}
                            ]
                        },
                    }
                ]
            }
            for i, sl in enumerate(slices)
        ]
        from audit.llm.base import _normalize_script_item

        llm._script = [_normalize_script_item(item, i) for i, item in enumerate(script)]  # type: ignore[attr-defined]
        llm._cursor = 0  # type: ignore[attr-defined]

        await make_tools_review_fn(ctx)(ctx.workspace, "big.py", [])
        system_texts = _system_texts(llm)
        assert len(system_texts) >= len(slices)
        for text in system_texts:
            assert SYMBOL_CONTEXT_DISABLED_TEXT in text
            # 真实索引下符号表本应有 func_* 条目，禁用后不得出现
            assert "function func_" not in text
