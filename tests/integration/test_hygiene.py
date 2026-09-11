"""联调卫生测试（R2 公共设施要求）：套件运行后仓库根不得新增 .codeaudit 目录。

- 本标记用例被 conftest.pytest_collection_modifyitems 移到整个会话最后执行，
  保证「套件运行后」语义；
- 用例自身先以显式 --work-root/--out 完成一次真实 CLI 审计（产物全部落 tmp_path），
  再对照会话起点快照断言仓库根的 .codeaudit 没有新增审计工作区——凡有用例漏传
  work-root（默认 .codeaudit 落到 CWD）即在此暴露；
- 仓库根若残留用户历史运行的 .codeaudit（如 dogfood 未带 --work-root），按
  「不新增」口径断言（快照条目之外不得出现新 audit_id 子目录）。

场景归属：R2 公共设施要求第 2 条（配套全部场景的工作区卫生约束）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

# 仓库根（与 conftest.py 的 ROOT 同一定位：tests/integration 的上两级）
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.hygiene_last
def test_repo_root_gains_no_codeaudit_after_suite(
    run_cli_subprocess, tmp_path: Path, repo_root_snapshot: dict
) -> None:
    repo_audit_dir = ROOT / ".codeaudit"

    # 一次显式沙箱化的真实审计（全部产物落 tmp_path）
    proc = run_cli_subprocess(
        [
            "run", "tests/samples/demo_proj",
            "--no-llm",
            "--work-root", str(tmp_path / "work"),
            "--out", str(tmp_path / "out"),
        ]
    )
    assert proc.returncode == 0, proc.stderr

    # 仓库根的 .codeaudit 不得出现本套件新增的审计工作区
    current = sorted(p.name for p in repo_audit_dir.iterdir()) if repo_audit_dir.is_dir() else []
    new_entries = [name for name in current if name not in repo_root_snapshot["entries"]]
    assert not new_entries, (
        f"仓库根 .codeaudit 出现了新审计工作区 {new_entries}："
        "某个用例未显式指定 --work-root / work_root（默认 .codeaudit 落到仓库根）"
    )

    if not repo_root_snapshot["existed"]:
        # 套件开始前仓库根本就没有 .codeaudit：套件运行后也不得出现
        assert not repo_audit_dir.exists(), (
            "仓库根出现了 .codeaudit 目录：某个用例未显式指定 --work-root / work_root"
        )
