"""联调场景⑥（R2 必测场景 6）：zip 输入 + --diff 警告回退。

- 流水线级：zip 是解压产物、无从对 git ref 求差 → 发「diff 增量仅支持本地 git
  目录输入，本次跳过增量」warning 事件并回退全量（两个缺陷文件的问题都在）；
- CLI 级：同一项目分别以目录与 zip 输入审计，两者检出的问题指纹集合完全一致
  （解压 + 单层顶层目录提升后的检出等价性）。

无 LLM，全程离线。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import cli
from audit.config import AuditConfig
from audit.models import Issue
from audit.orchestrator.pipeline import run_audit
from audit.utils import issue_fingerprint

_A_PY = """import os


def remove_quietly(path):
    try:
        os.remove(path)
    except:
        pass
    return True
"""

_B_PY = """def collect(items, bucket=[]):
    bucket.extend(items)
    return bucket
"""

_FILES = {"a.py": _A_PY, "b.py": _B_PY}


async def test_zip_with_diff_ref_warns_and_falls_back_to_full(
    tmp_path: Path, make_git_project, events_collector, stage_order
) -> None:
    project = make_git_project(_FILES)
    zip_path = shutil.make_archive(str(tmp_path / "proj"), "zip", root_dir=project)

    config = AuditConfig(
        source_path=zip_path,
        work_root=str(tmp_path / "work"),
        out_dir=str(tmp_path / "out"),
        enable_llm_review=False,
        diff_ref="HEAD",  # zip 输入：无法对 ref 求差 → 必须警告并回退全量
    )
    report = await run_audit(config, events_collector)
    events = events_collector.events

    warnings = [e for e in events if e.get("warning")]
    assert any(
        "diff 增量仅支持本地 git 目录输入" in str(e.get("message", "")) and "跳过增量" in str(e.get("message", ""))
        for e in warnings
    ), warnings
    # 未发"增量模式"事件（没有真的剪枝）
    assert not any("增量模式" in str(e.get("message", "")) for e in events)

    # 回退全量：两个文件的缺陷都在
    issue_files = {i.file for i in report.issues}
    assert {"a.py", "b.py"} <= issue_files
    assert stage_order(events)[-1] == "done"


def test_zip_input_detection_equivalent_to_dir_input(tmp_path: Path, make_project, capsys) -> None:
    """同一项目：目录输入与 zip 输入的检出问题指纹集合一致（CLI 端到端）。"""
    project = make_project(_FILES, name="equiv")

    def _run(source: Path, out_name: str) -> set[str]:
        out_dir = tmp_path / out_name
        rc = cli.main(
            [
                "run", str(source), "--no-llm",
                "--work-root", str(tmp_path / "work"),
                "--out", str(out_dir),
                "--json",
            ]
        )
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)  # 无门禁时 --json 的 stdout 是纯 JSON
        report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
        assert report == data  # stdout 报告与落盘报告一致
        return {issue_fingerprint(Issue.from_dict(raw)) for raw in report["issues"]}

    dir_prints = _run(project, "out_dir_input")

    zip_path = shutil.make_archive(str(tmp_path / "equiv"), "zip", root_dir=project)
    zip_prints = _run(Path(zip_path), "out_zip_input")

    assert zip_prints == dir_prints
    assert len(zip_prints) >= 2  # a.py 裸 except + b.py 可变默认参数
