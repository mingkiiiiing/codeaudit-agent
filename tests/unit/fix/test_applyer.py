"""P0-2 apply-to-source 单测：applyer 核心语义 + 生成侧指纹补记 + 报告定位。

覆盖路径：
- dry-run 不落盘；--yes 文本精确替换落盘；
- 目标文件 sha256 漂移 → 拒绝 + all-or-nothing（整体不落盘）；
- --all-verified 闸门；单补丁不限状态；
- 无指纹老补丁：dry-run 可预览、--yes 拒绝并提示重新审计；
- 新增/删除文件补丁；CRLF 保持；纯插入 hunk（@@ -l,0）语义；
- run_fix_stage 生成侧补记 target_sha256（备份字节即原文指纹）；
- locate_audit：TaskStore / server 布局 / CLI 布局 / 未找到。

全部离线：LLM 用 FakeLLMClient 脚本回放。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import _fix_helpers as H
from audit.fix.applyer import ApplyResult, apply_patches, locate_audit
from audit.fix.stage import run_fix_stage
from audit.llm.base import FakeLLMClient
from audit.models import AuditReport, Issue, Patch, Severity
from audit.taskstore import TaskStore

AID = "apply0001"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _patch(
    diff: str,
    sha_map: dict[str, str] | None = None,
    *,
    status: str = "verified",
    pid: str = "PATCH-0001",
) -> Patch:
    return Patch(
        id=pid,
        issue_id="ISS-0001",
        diff=diff,
        rationale="测试补丁",
        apply_status=status,
        target_sha256=sha_map if sha_map is not None else {},
    )


def _seed_project(tmp_path: Path) -> Path:
    """目标源码目录：与 fix 样例一致的 app.py（LF 写入）。"""
    workdir = tmp_path / "proj"
    H.write_file(workdir / "app.py", H.APP_PY)
    return workdir


# ---------------------------------------------------------------- dry-run / --yes


def test_dry_run_lists_targets_without_writing(tmp_path: Path):
    workdir = _seed_project(tmp_path)
    before = (workdir / "app.py").read_bytes()
    patch = _patch(H.BARE_EXCEPT_DIFF, {"app.py": _sha(before)})

    result = apply_patches([patch], workdir, yes=False)

    assert result.dry_run is True
    assert result.applied == [] and result.written_files == []
    assert result.rejected == []  # 预检通过：无拒绝
    assert (workdir / "app.py").read_bytes() == before  # 不落盘


def test_yes_applies_exact_text_replacement(tmp_path: Path):
    workdir = _seed_project(tmp_path)
    before = (workdir / "app.py").read_bytes()
    patch = _patch(H.BARE_EXCEPT_DIFF, {"app.py": _sha(before)})

    result = apply_patches([patch], workdir, yes=True)

    assert result.ok and result.dry_run is False
    assert result.applied[0]["patch_id"] == "PATCH-0001"
    assert result.written_files == ["app.py"]
    expected = H.APP_PY.replace("    except:\n", '    except Exception:\n        logger.exception("divide failed")\n')
    assert (workdir / "app.py").read_text(encoding="utf-8") == expected
    assert not list(workdir.glob("*.codeaudit-apply.tmp"))  # 不留临时文件


# ---------------------------------------------------------------- 指纹漂移 / all-or-nothing


def test_sha256_drift_rejects_and_writes_nothing(tmp_path: Path):
    workdir = _seed_project(tmp_path)
    stale = _patch(H.BARE_EXCEPT_DIFF, {"app.py": _sha(b"outdated content\n")})
    before = (workdir / "app.py").read_bytes()

    result = apply_patches([stale], workdir, yes=True)

    assert not result.ok
    assert result.applied == [] and result.written_files == []
    reason = result.rejected[0]["reason"]
    assert "sha256 与审计时不一致" in reason
    assert (workdir / "app.py").read_bytes() == before  # 拒绝即不落盘


def test_any_rejection_blocks_whole_run_all_or_nothing(tmp_path: Path):
    workdir = _seed_project(tmp_path)
    before = (workdir / "app.py").read_bytes()
    good = _patch(H.BARE_EXCEPT_DIFF, {"app.py": _sha(before)})
    drifted = _patch(H.BARE_EXCEPT_DIFF, {"app.py": _sha(b"user edited\n")}, pid="PATCH-0002")

    result = apply_patches([good, drifted], workdir, yes=True)

    assert len(result.rejected) == 1
    assert result.rejected[0]["patch_index"] == 1
    assert result.applied == [] and result.written_files == []  # 第一个补丁也不落盘
    assert (workdir / "app.py").read_bytes() == before


# ---------------------------------------------------------------- 状态闸门


def test_all_verified_gate_blocks_non_verified(tmp_path: Path):
    workdir = _seed_project(tmp_path)
    before = (workdir / "app.py").read_bytes()
    patch = _patch(H.BARE_EXCEPT_DIFF, {"app.py": _sha(before)}, status="needs-review")

    result = apply_patches([patch], workdir, yes=True, require_all_verified=True)

    assert not result.ok
    assert "--all-verified 闸门" in result.rejected[0]["reason"]
    assert (workdir / "app.py").read_bytes() == before


def test_all_verified_gate_passes_when_all_verified(tmp_path: Path):
    workdir = _seed_project(tmp_path)
    patch = _patch(H.BARE_EXCEPT_DIFF, {"app.py": _sha((workdir / "app.py").read_bytes())})

    result = apply_patches([patch], workdir, yes=True, require_all_verified=True)

    assert result.ok and len(result.applied) == 1


def test_single_patch_any_status_applies_with_status_hint(tmp_path: Path):
    """单补丁不限状态：needs-review 也能应用（状态在结果中如实透出）。"""
    workdir = _seed_project(tmp_path)
    patch = _patch(H.BARE_EXCEPT_DIFF, {"app.py": _sha((workdir / "app.py").read_bytes())}, status="syntax-ok")

    result = apply_patches([patch], workdir, yes=True, only_index=0)

    assert result.ok and result.applied[0]["status"] == "syntax-ok"


# ---------------------------------------------------------------- 无指纹老补丁


def test_legacy_patch_without_fingerprint_dry_run_previews_but_yes_rejects(tmp_path: Path):
    workdir = _seed_project(tmp_path)
    before = (workdir / "app.py").read_bytes()
    legacy = _patch(H.BARE_EXCEPT_DIFF, None)  # 旧版产物：无 target_sha256

    dry = apply_patches([legacy], workdir, yes=False)
    assert dry.dry_run is True
    assert "缺少原文指纹" in dry.rejected[0]["reason"] and "预览" in dry.rejected[0]["reason"]
    assert (workdir / "app.py").read_bytes() == before  # dry-run 本就不落盘

    yes = apply_patches([legacy], workdir, yes=True)
    assert not yes.ok
    assert "重新审计" in yes.rejected[0]["reason"]
    assert (workdir / "app.py").read_bytes() == before  # --yes 仍被拒绝，未落盘


def test_empty_diff_patch_rejected(tmp_path: Path):
    workdir = _seed_project(tmp_path)
    result = apply_patches([_patch("")], workdir, yes=True)
    assert not result.ok and "补丁无 diff 内容" in result.rejected[0]["reason"]


# ---------------------------------------------------------------- 新增 / 删除 / 特殊 hunk


def test_new_file_patch_creates_file(tmp_path: Path):
    workdir = _seed_project(tmp_path)
    diff = (
        "diff --git a/newmod.py b/newmod.py\n"
        "--- /dev/null\n"
        "+++ b/newmod.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+VALUE = 1\n"
        "+\n"
    )
    patch = _patch(diff, {"newmod.py": ""})
    result = apply_patches([patch], workdir, yes=True)
    assert result.ok
    assert (workdir / "newmod.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_new_file_patch_rejected_when_target_exists(tmp_path: Path):
    workdir = _seed_project(tmp_path)
    diff = (
        "diff --git a/app.py b/app.py\n"
        "--- /dev/null\n"
        "+++ b/app.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+VALUE = 1\n"
    )
    patch = _patch(diff, {"app.py": ""})
    result = apply_patches([patch], workdir, yes=True)
    assert not result.ok and "已存在" in result.rejected[0]["reason"]


def test_delete_file_patch_removes_target(tmp_path: Path):
    workdir = _seed_project(tmp_path)
    extra = workdir / "victim.py"
    H.write_file(extra, H.VICTIM_PY)
    patch = _patch(H.DELETE_VICTIM_DIFF, {"victim.py": _sha(H.VICTIM_PY.encode("utf-8"))})
    result = apply_patches([patch], workdir, yes=True)
    assert result.ok and not extra.exists()


def test_pure_insertion_hunk_semantics(tmp_path: Path):
    """@@ -l,0 +m,n @@（纯插入）语义：插在第 l 行之后。"""
    workdir = _seed_project(tmp_path)
    content = "one\ntwo\n"
    H.write_file(workdir / "ins.py", content)
    diff = (
        "diff --git a/ins.py b/ins.py\n"
        "--- a/ins.py\n"
        "+++ b/ins.py\n"
        "@@ -1,0 +2,1 @@\n"
        "+inserted\n"
    )
    patch = _patch(diff, {"ins.py": _sha(content.encode("utf-8"))})
    result = apply_patches([patch], workdir, yes=True)
    assert result.ok
    assert (workdir / "ins.py").read_text(encoding="utf-8") == "one\ninserted\ntwo\n"


def test_crlf_file_context_preserved(tmp_path: Path):
    workdir = _seed_project(tmp_path)
    crlf_content = H.APP_PY.replace("\n", "\r\n")
    target = workdir / "crlf.py"
    target.write_bytes(crlf_content.encode("utf-8"))
    crlf_diff = H.BARE_EXCEPT_DIFF.replace("app.py", "crlf.py").replace("\n", "\r\n")
    patch = _patch(crlf_diff, {"crlf.py": _sha(crlf_content.encode("utf-8"))})

    result = apply_patches([patch], workdir, yes=True)

    assert result.ok
    after = target.read_bytes()
    assert b"\r\n" in after  # CRLF 风格保持
    assert b'except Exception:\r\n        logger.exception("divide failed")\r\n' in after


# ---------------------------------------------------------------- 生成侧指纹补记（fix 管线）


async def test_fix_stage_records_target_sha256(tmp_path: Path, fake_emitter):
    """run_fix_stage 产物 Patch 携带各目标文件 apply 前内容的 sha256。"""
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    fake = FakeLLMClient([H.llm_json(H.BARE_EXCEPT_DIFF, "修复裸 except")])
    ctx = H.make_ctx(ws, fake, fake_emitter)
    ctx.issues = [
        Issue(
            id="ISS-0001",
            title="裸 except",
            file="app.py",
            line_start=9,
            line_end=9,
            severity=Severity.CRITICAL,
        )
    ]

    await run_fix_stage(ctx)

    assert len(ctx.patches) == 1
    patch = ctx.patches[0]
    assert patch.target_sha256 == {"app.py": _sha(H.APP_PY.encode("utf-8"))}


# ---------------------------------------------------------------- 报告定位 locate_audit


def _report(aid: str, patches: list[Patch] | None = None) -> AuditReport:
    return AuditReport(audit_id=aid, project_name="p", patches=patches or [])


def _report_json(report: AuditReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False)


def test_locate_audit_via_taskstore(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CODEAUDIT_DB_PATH", raising=False)
    root = tmp_path / ".codeaudit"
    store = TaskStore(root / "audits.db")
    try:
        store.create(AID, created_at="2026-01-01T00:00:00+08:00", source_path="C:/tmp/proj", do_fix=True, do_tests=False)
        store.set_report(AID, _report(AID))
    finally:
        store.close()
    record = locate_audit(AID, root)
    assert record.source_path == "C:/tmp/proj"
    assert record.report.audit_id == AID
    assert record.location.endswith("audits.db")


def test_locate_audit_via_server_report_layout(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CODEAUDIT_DB_PATH", raising=False)
    report_dir = tmp_path / AID / "reports"
    report_dir.mkdir(parents=True)
    (report_dir / "report.json").write_text(_report_json(_report(AID)), encoding="utf-8")
    record = locate_audit(AID, tmp_path)
    assert record.report.audit_id == AID
    assert "report.json" in record.location


def test_locate_audit_via_cli_report_layout_matches_id(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CODEAUDIT_DB_PATH", raising=False)
    report_dir = tmp_path / "reports"
    report_dir.mkdir(parents=True)
    (report_dir / "report.json").write_text(_report_json(_report(AID)), encoding="utf-8")
    record = locate_audit(AID, tmp_path)
    assert record.report.audit_id == AID


def test_locate_audit_not_found_raises(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CODEAUDIT_DB_PATH", raising=False)
    with pytest.raises(FileNotFoundError):
        locate_audit("nope0009", tmp_path)


def test_apply_result_to_dict_shape(tmp_path: Path):
    workdir = _seed_project(tmp_path)
    patch = _patch(H.BARE_EXCEPT_DIFF, {"app.py": _sha((workdir / "app.py").read_bytes())})
    dry: ApplyResult = apply_patches([patch], workdir, yes=False)
    payload = dry.to_dict()
    assert set(payload) == {"applied", "rejected", "dry_run", "written_files"}
    assert payload["dry_run"] is True
    json.dumps(payload, ensure_ascii=False)  # API 返回体可整体 JSON 序列化
