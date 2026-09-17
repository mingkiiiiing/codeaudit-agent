"""P0-2 CLI apply 子命令自测（全部离线，报告经磁盘布局注入，自包含样例常量）。

覆盖：dry-run 不落盘 / --yes 落盘 / 序号越界 / --all-verified 闸门 /
单补丁状态提示 / 无指纹老补丁 / 无补丁审计 / 报告未找到 / all-or-nothing。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cli
from audit.models import AuditReport, Patch

AID = "cliapp001"

# 自包含样例（与 tests/unit/fix/_fix_helpers 同源的 app.py + 裸 except 修复 diff，LF）
APP_PY = """import logging

logger = logging.getLogger(__name__)


def divide(a, b):
    try:
        return a / b
    except:
        return None
"""

BARE_EXCEPT_DIFF = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -6,5 +6,6 @@\n"
    " def divide(a, b):\n"
    "     try:\n"
    "         return a / b\n"
    "-    except:\n"
    "+    except Exception:\n"
    '+        logger.exception("divide failed")\n'
    "         return None\n"
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_lf(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _patch(
    diff: str, sha_map: dict[str, str], *, status: str = "verified", pid: str = "PATCH-0001"
) -> Patch:
    return Patch(
        id=pid,
        issue_id="ISS-0001",
        diff=diff,
        rationale="CLI 测试补丁",
        apply_status=status,
        target_sha256=sha_map,
    )


def _seed_audit(tmp_path: Path, patches: list[Patch]) -> tuple[Path, Path]:
    """注入 <work-root>/<audit_id>/reports/report.json（server 任务布局）与目标源码目录。"""
    report = AuditReport(audit_id=AID, project_name="proj", patches=patches)
    report_dir = tmp_path / "wr" / AID / "reports"
    report_dir.mkdir(parents=True)
    (report_dir / "report.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    proj = tmp_path / "proj"
    _write_lf(proj / "app.py", APP_PY)
    return tmp_path / "wr", proj


_APPLY_BASE = ["apply", AID, "--work-root"]


def test_cli_apply_dry_run_does_not_write(tmp_path: Path, capsys):
    work_root, proj = _seed_audit(tmp_path, [_patch(BARE_EXCEPT_DIFF, {"app.py": _sha(APP_PY)})])
    before = (proj / "app.py").read_bytes()

    rc = cli.main([*_APPLY_BASE, str(work_root), "--workdir", str(proj)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "预检通过" in out
    assert (proj / "app.py").read_bytes() == before  # 不落盘


def test_cli_apply_yes_writes_source(tmp_path: Path, capsys):
    work_root, proj = _seed_audit(tmp_path, [_patch(BARE_EXCEPT_DIFF, {"app.py": _sha(APP_PY)})])

    rc = cli.main([*_APPLY_BASE, str(work_root), "--workdir", str(proj), "--yes"])

    assert rc == 0
    assert "应用成功" in capsys.readouterr().out
    expected = APP_PY.replace(
        "    except:\n", '    except Exception:\n        logger.exception("divide failed")\n'
    )
    assert (proj / "app.py").read_text(encoding="utf-8") == expected


def test_cli_apply_patch_index_out_of_range(tmp_path: Path, capsys):
    work_root, proj = _seed_audit(tmp_path, [_patch(BARE_EXCEPT_DIFF, {"app.py": _sha(APP_PY)})])

    rc = cli.main([*_APPLY_BASE, str(work_root), "--workdir", str(proj), "--patch", "5", "--yes"])

    assert rc == 1
    assert "越界" in capsys.readouterr().err
    assert (proj / "app.py").read_text(encoding="utf-8") == APP_PY


def test_cli_apply_all_verified_gate(tmp_path: Path, capsys):
    work_root, proj = _seed_audit(
        tmp_path, [_patch(BARE_EXCEPT_DIFF, {"app.py": _sha(APP_PY)}, status="needs-review")]
    )
    base = [*_APPLY_BASE, str(work_root), "--workdir", str(proj)]

    # dry-run：不落盘，但预览中明确展示闸门拒绝
    rc = cli.main([*base, "--all-verified"])
    assert rc == 0
    assert "--all-verified 闸门" in capsys.readouterr().out
    assert (proj / "app.py").read_text(encoding="utf-8") == APP_PY

    # --yes：闸门拒绝 → 非 0 退出码
    rc = cli.main([*base, "--all-verified", "--yes"])
    assert rc == 1
    assert (proj / "app.py").read_text(encoding="utf-8") == APP_PY


def test_cli_apply_single_patch_prints_status_hint(tmp_path: Path, capsys):
    work_root, proj = _seed_audit(
        tmp_path, [_patch(BARE_EXCEPT_DIFF, {"app.py": _sha(APP_PY)}, status="syntax-ok")]
    )

    rc = cli.main([*_APPLY_BASE, str(work_root), "--workdir", str(proj), "--patch", "0"])

    assert rc == 0
    assert "非 verified" in capsys.readouterr().out  # 状态提示


def test_cli_apply_legacy_patch_dry_run_previews_yes_refuses(tmp_path: Path, capsys):
    legacy = Patch(id="PATCH-0001", issue_id="ISS-0001", diff=BARE_EXCEPT_DIFF, apply_status="verified")
    work_root, proj = _seed_audit(tmp_path, [legacy])
    base = [*_APPLY_BASE, str(work_root), "--workdir", str(proj)]

    rc = cli.main(base)  # dry-run：可预览
    assert rc == 0
    assert "缺少原文指纹" in capsys.readouterr().out

    rc = cli.main([*base, "--yes"])  # --yes：拒绝落盘
    assert rc == 1
    assert "重新审计" in capsys.readouterr().out
    assert (proj / "app.py").read_text(encoding="utf-8") == APP_PY


def test_cli_apply_all_or_nothing_leaves_source_untouched(tmp_path: Path, capsys):
    good = _patch(BARE_EXCEPT_DIFF, {"app.py": _sha(APP_PY)})
    drifted = _patch(BARE_EXCEPT_DIFF, {"app.py": _sha("user edited\n")}, pid="PATCH-0002")
    work_root, proj = _seed_audit(tmp_path, [good, drifted])

    rc = cli.main([*_APPLY_BASE, str(work_root), "--workdir", str(proj), "--yes"])

    assert rc == 1
    assert "all-or-nothing" in capsys.readouterr().err
    assert (proj / "app.py").read_text(encoding="utf-8") == APP_PY  # 整体未落盘


def test_cli_apply_audit_without_patches(tmp_path: Path, capsys):
    work_root, proj = _seed_audit(tmp_path, [])

    rc = cli.main([*_APPLY_BASE, str(work_root), "--workdir", str(proj)])

    assert rc == 0
    assert "没有可应用的补丁" in capsys.readouterr().out


def test_cli_apply_report_not_found_returns_1(tmp_path: Path):
    rc = cli.main(["apply", "ghost0099", "--work-root", str(tmp_path)])
    assert rc == 1


def test_cli_apply_help_exits_zero():
    import pytest

    with pytest.raises(SystemExit) as exc:
        cli.main(["apply", "--help"])
    assert exc.value.code == 0
