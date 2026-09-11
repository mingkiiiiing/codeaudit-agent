"""pytest 全局 fixtures：保证 audit 可导入、提供样例工程工作区。"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from audit.config import AuditConfig  # noqa: E402
from audit.llm.base import FakeLLMClient  # noqa: E402
from audit.pipeline import PipelineContext  # noqa: E402
from audit.workspace import WorkspaceContext  # noqa: E402

SAMPLES = ROOT / "tests" / "samples"


@pytest.fixture
def demo_proj_path() -> Path:
    return SAMPLES / "demo_proj"


@pytest.fixture
def sample_workspace(tmp_path: Path, demo_proj_path: Path) -> WorkspaceContext:
    """把 demo_proj 复制到临时目录，返回指向副本的 WorkspaceContext。"""
    src = tmp_path / "src"
    shutil.copytree(demo_proj_path, src)
    return WorkspaceContext(
        audit_id="test0001",
        src_root=src,
        work_root=tmp_path,
        db_path=tmp_path / "index.db",
    )


@pytest.fixture
def fake_emitter():
    """收集事件的发射器，便于断言进度事件。"""
    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)

    emit.events = events  # type: ignore[attr-defined]
    return emit


@pytest.fixture
def pipeline_ctx(sample_workspace: WorkspaceContext, fake_emitter) -> PipelineContext:
    config = AuditConfig(source_path=str(sample_workspace.src_root))
    return PipelineContext(
        config=config,
        workspace=sample_workspace,
        llm=FakeLLMClient(),
        emitter=fake_emitter,
    )
