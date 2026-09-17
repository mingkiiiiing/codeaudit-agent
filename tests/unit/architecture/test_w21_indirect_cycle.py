"""W21 卡3：三层间接环（a→b→c→a）语料实测既有循环依赖检测能力。

背景：既有循环依赖检测能力在 audit/refactor/heuristics.py——`_import_graph`
（基于 IndexStore.dependencies 构造项目内文件级导入图）+ `_find_cycles`
（迭代三色 DFS 找环，无环长限制），消费点是 `_cycle_proposal`（检出环才输出
「消除模块循环依赖」重构方案）。既有单测（test_heuristics.py）只用 FakeIndex
桩覆盖了两层直接环（a↔b），本卡在 pytest tmp_path 下构造真实三模块包语料：

    pkg/a.py ──from pkg.b import──▶ pkg/b.py
        ▲                              │
        └──────── from pkg.a import ── pkg/c.py

走「真实文件 → SqliteIndexStore.build() → 真实依赖图 → 环检测 → 重构方案」
全链路，断言 3 节点间接环被识别（图级 + generate_proposals 方案级），
并附无环对照（a→b 单向）断言不误报。

本测试只新增测试文件，不改任何共享模块。
"""

from __future__ import annotations

from pathlib import Path

from audit.config import AuditConfig
from audit.indexer import create_index
from audit.llm.base import FakeLLMClient
from audit.models import FileManifest
from audit.pipeline import PipelineContext
from audit.refactor.heuristics import _find_cycles, _import_graph, generate_proposals
from audit.workspace import WorkspaceContext

# 三层间接环语料：a → b → c → a（全部为真实 import）
_CORPUS = {
    "pkg/__init__.py": "",
    "pkg/a.py": (
        "from pkg.b import helper_b\n"
        "\n"
        "\n"
        "def run_a() -> int:\n"
        "    return helper_b() + 1\n"
    ),
    "pkg/b.py": (
        "from pkg.c import helper_c\n"
        "\n"
        "\n"
        "def helper_b() -> int:\n"
        "    return helper_c() + 1\n"
    ),
    "pkg/c.py": (
        "from pkg.a import run_a\n"
        "\n"
        "\n"
        "def helper_c() -> int:\n"
        "    return 1\n"
    ),
}

_FILES = ["pkg/a.py", "pkg/b.py", "pkg/c.py"]


def _make_workspace(tmp_path: Path, files: dict[str, str]) -> tuple[WorkspaceContext, Path]:
    """在 tmp_path/src 下落盘语料并构造 WorkspaceContext。"""
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    ws = WorkspaceContext(
        audit_id="w21-cycle", src_root=src, work_root=tmp_path / "work", db_path=tmp_path / "idx.db"
    )
    return ws, src


def _build_index(ws: WorkspaceContext):
    store = create_index(ws)
    store.build()
    return store


# ------------------------------------------------------------- 图级：环检测


def test_three_node_indirect_cycle_in_graph(tmp_path: Path) -> None:
    """真实语料 + 真实索引：依赖图检出 3 节点间接环 a→b→c→a。"""
    ws, _src = _make_workspace(tmp_path, _CORPUS)
    store = _build_index(ws)
    try:
        # 前置自检：三条边都被索引解析为项目内文件（排除"索引没建好"假阳性）
        assert store.dependencies("pkg/a.py") == ["pkg/b.py"]
        assert store.dependencies("pkg/b.py") == ["pkg/c.py"]
        assert store.dependencies("pkg/c.py") == ["pkg/a.py"]

        graph = _import_graph(store.dependencies, _FILES)
        cycles = _find_cycles(graph, limit=10)
        assert cycles, "三层间接环（a→b→c→a）未被识别：环检测能力缺口"
        cycle = cycles[0]
        assert cycle[0] == cycle[-1], f"环路径首尾应闭合：{cycle}"
        assert set(cycle[:-1]) == {"pkg/a.py", "pkg/b.py", "pkg/c.py"}, f"环应覆盖全部 3 节点：{cycle}"
        assert len(cycle) == 4, f"3 节点环路径应为 3 节点 + 闭合点：{cycle}"
    finally:
        store.close()


def test_acyclic_control_finds_no_cycle(tmp_path: Path) -> None:
    """无环对照：a→b 单向（b 无回边）不产出任何环。"""
    corpus = dict(_CORPUS)
    corpus["pkg/b.py"] = "def helper_b() -> int:\n    return 1\n"  # 剪断 b→c
    ws, _src = _make_workspace(tmp_path, corpus)
    store = _build_index(ws)
    try:
        graph = _import_graph(store.dependencies, _FILES)
        # 剪断 b→c 后剩 a→b、c→a 两条边：链式无环（c→a→b 死路）
        assert graph == {"pkg/a.py": {"pkg/b.py"}, "pkg/b.py": set(), "pkg/c.py": {"pkg/a.py"}}
        assert _find_cycles(graph, limit=10) == []
    finally:
        store.close()


# ------------------------------------------------------- 方案级：重构建议


def _loc_of(rel: str, src: Path) -> int:
    """语料行数（manifests 的 loc 字段，仅用于方案层文件清单非空）。"""
    return len((src / rel).read_text(encoding="utf-8").splitlines())


def test_three_node_cycle_produces_refactor_proposal(tmp_path: Path) -> None:
    """generate_proposals 全链路：真实语料检出 3 节点环 → 循环依赖重构方案。"""
    ws, src = _make_workspace(tmp_path, _CORPUS)
    store = _build_index(ws)
    try:
        events: list[dict] = []

        async def emit(event: dict) -> None:
            events.append(event)

        config = AuditConfig(source_path=str(src), enable_llm_review=False)
        ctx = PipelineContext(config=config, workspace=ws, llm=FakeLLMClient(), emitter=emit)
        ctx.index = store
        ctx.workspace.manifests = [
            FileManifest(path=rel, language="python", loc=_loc_of(rel, src)) for rel in _FILES
        ]
        proposals = [p for p in generate_proposals(ctx) if p.kind == "other"]
        assert len(proposals) == 1, "3 节点环应产出且仅产出 1 条循环依赖方案"
        p = proposals[0]
        assert "循环依赖" in p.title
        # 环路径完整呈现三层间接环：pkg/a.py -> pkg/b.py -> pkg/c.py -> pkg/a.py
        assert "pkg/a.py -> pkg/b.py -> pkg/c.py -> pkg/a.py" in p.rationale
    finally:
        store.close()
