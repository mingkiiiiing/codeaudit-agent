"""W2-A1 单测：patcher（build_fix_messages / generate_patch / validate_diff / apply_diff）。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from audit.llm.base import FakeLLMClient, LLMResponse
from audit.models import Category, Issue, Severity

import _fix_helpers as H
from audit.fix.patcher import apply_diff, build_fix_messages, generate_patch, validate_diff


def _issue(file: str = "app.py", line: int = 9, evidence: list[str] | None = None) -> Issue:
    return Issue(
        id="ISS-0001",
        category=Category.BUG,
        severity=Severity.CRITICAL,
        title="裸 except 吞掉所有异常",
        file=file,
        line_start=line,
        line_end=line,
        code_snippet="except:",
        description="except: 会捕获包括 KeyboardInterrupt 在内的所有异常",
        evidence=evidence if evidence is not None else ["rule_hit: bare-except"],
        suggestion="改为 except Exception 并记录日志",
        confidence=0.92,
    )


# ---------------------------------------------------------------- build_fix_messages


def test_build_fix_messages_layout_and_constraints():
    messages = build_fix_messages(
        _issue(),
        ["a = 1", "b = 2"],
        [("关联符号 get_user", "def get_user():\n    return None")],
        project_style_hint="日志用 logging 而非 print",
    )
    assert len(messages) == 2
    system, user = messages
    assert system["role"] == "system"
    assert "最小改动" in system["content"]  # docs/03 §3.3 核心约束
    assert "保持项目现有风格" in system["content"]
    assert '{"diff"' in system["content"].replace(" ", "").replace("：", ":") or "JSON" in system["content"]
    assert user["role"] == "user"
    assert '"ISS-0001"' in user["content"]  # issue JSON 注入
    assert "   1: a = 1" in user["content"]  # 行号从 1 起、右对齐 4 列
    assert "第 1-2 行" in user["content"]
    assert "[context-1] 关联符号 get_user" in user["content"]
    assert "[style] 日志用 logging 而非 print" in user["content"]
    assert '只输出一个 JSON 对象' in user["content"]  # 输出格式首尾双写


def test_build_fix_messages_supports_slice_start_line():
    """大文件切片场景：传 (起始行, 行列表) 时行号保持真实值。"""
    messages = build_fix_messages(_issue(), (10, ["x = 1"]), [])
    assert "  10: x = 1" in messages[1]["content"]


def test_build_fix_messages_without_optional_parts():
    messages = build_fix_messages(_issue(), ["x = 1"], [])
    assert "[style]" not in messages[1]["content"]
    assert "[context-1]" not in messages[1]["content"]


# ---------------------------------------------------------------- generate_patch


async def test_generate_patch_success_single_call(tmp_path: Path):
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    fake = FakeLLMClient([H.llm_json(H.BARE_EXCEPT_DIFF, "裸 except 改精确捕获")])
    result = await generate_patch(fake, _issue(), ws, None)
    assert result is not None
    diff, rationale = result
    assert diff == H.BARE_EXCEPT_DIFF
    assert rationale == "裸 except 改精确捕获"
    assert len(fake.calls) == 1
    assert fake.calls[0]["json_mode"] is True


async def test_generate_patch_retries_once_with_error_feedback(tmp_path: Path):
    """首次输出不可解析 → 带错误回喂重试 1 次后成功。"""
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    fake = FakeLLMClient(
        [
            LLMResponse(content="好的，以下是修复 diff：（忘记输出 JSON 了）"),
            H.llm_json(H.BARE_EXCEPT_DIFF),
        ]
    )
    result = await generate_patch(fake, _issue(), ws, None)
    assert result is not None
    assert len(fake.calls) == 2
    retry_messages = fake.calls[1]["messages"]
    assert [m["role"] for m in retry_messages] == ["system", "user", "assistant", "user"]
    assert "无法解析" in retry_messages[-1]["content"]  # 错误信息回喂
    assert retry_messages[2]["content"]  # 上次原始输出作为 assistant 消息保留


async def test_generate_patch_returns_none_after_failed_retry(tmp_path: Path):
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    fake = FakeLLMClient([LLMResponse(content="still not json"), LLMResponse(content="")])
    assert await generate_patch(fake, _issue(), ws, None) is None
    assert len(fake.calls) == 2  # 恰好重试 1 次，不无限循环


async def test_generate_patch_rejects_json_without_diff(tmp_path: Path):
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    fake = FakeLLMClient([{"content": '{"rationale": "只有说明没有 diff"}'}, {"content": '{"rationale": ""}'}])
    assert await generate_patch(fake, _issue(), ws, None) is None


async def test_generate_patch_missing_file_returns_none(tmp_path: Path):
    ws = H.make_workspace(tmp_path, {})
    fake = FakeLLMClient([H.llm_json(H.BARE_EXCEPT_DIFF)])
    assert await generate_patch(fake, _issue(file="ghost.py"), ws, None) is None
    assert fake.calls == []  # 文件不可读时不应发起 LLM 调用


# ---------------------------------------------------------------- validate_diff


def test_validate_diff_accepts_valid_diff(tmp_path: Path):
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    assert validate_diff(H.BARE_EXCEPT_DIFF, ws) == []


def test_validate_diff_accepts_dev_null_new_file(tmp_path: Path):
    ws = H.make_workspace(tmp_path, {})
    new_file_diff = (
        "diff --git a/newmod.py b/newmod.py\n"
        "--- /dev/null\n"
        "+++ b/newmod.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+x = 1\n"
    )
    assert validate_diff(new_file_diff, ws) == []


def test_validate_diff_requires_hunk_header(tmp_path: Path):
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    no_hunk = H.BARE_EXCEPT_DIFF.split("@@", 1)[0] + " no hunk\n"
    errors = validate_diff(no_hunk, ws)
    assert any("@@" in e for e in errors)


def test_validate_diff_requires_file_headers(tmp_path: Path):
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    body_only = "@@ -1,1 +1,1 @@\n-x = 1\n+x = 2\n"
    errors = validate_diff(body_only, ws)
    assert any("---" in e for e in errors)
    assert any("+++" in e for e in errors)


def test_validate_diff_rejects_parent_escape(tmp_path: Path):
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    escape_diff = (
        "diff --git a/../evil.py b/../evil.py\n"
        "--- a/../evil.py\n"
        "+++ b/../evil.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-a\n"
        "+b\n"
    )
    errors = validate_diff(escape_diff, ws)
    assert errors and any(".." in e for e in errors)


def test_validate_diff_rejects_absolute_path(tmp_path: Path):
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    absolute_diff = (
        "diff --git a/app.py b/app.py\n"
        "--- /etc/passwd\n"
        "+++ /etc/passwd\n"
        "@@ -1,1 +1,1 @@\n"
        "-a\n"
        "+b\n"
    )
    errors = validate_diff(absolute_diff, ws)
    assert errors and any("绝对路径" in e for e in errors)


def test_validate_diff_rejects_empty_diff(tmp_path: Path):
    ws = H.make_workspace(tmp_path, {})
    assert validate_diff("", ws) == ["diff 为空"]
    assert validate_diff("   \n", ws) == ["diff 为空"]


# ---------------------------------------------------------------- apply_diff


def test_apply_diff_applies_and_changes_file(tmp_path: Path):
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    ok, msg = apply_diff(ws, H.BARE_EXCEPT_DIFF)
    assert ok, msg
    content = ws.abs_path("app.py").read_text(encoding="utf-8")
    assert "except Exception:" in content
    assert 'logger.exception("divide failed")' in content


def test_apply_diff_check_only_leaves_file_untouched(tmp_path: Path):
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    ok, msg = apply_diff(ws, H.BARE_EXCEPT_DIFF, check_only=True)
    assert ok, msg
    assert "except:" in ws.abs_path("app.py").read_text(encoding="utf-8")


def test_apply_diff_rejects_unapplicable_diff(tmp_path: Path):
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})
    ok, msg = apply_diff(ws, H.BAD_CONTEXT_DIFF)
    assert not ok
    assert "git apply" in msg
    assert ws.abs_path("app.py").read_text(encoding="utf-8") == H.APP_PY  # 原子性：失败不落盘


def test_apply_diff_reports_git_unavailable(tmp_path: Path, monkeypatch):
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})

    def _raise(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git not found")

    monkeypatch.setattr("audit.fix.patcher.subprocess.run", _raise)
    ok, msg = apply_diff(ws, H.BARE_EXCEPT_DIFF)
    assert ok is False
    assert msg.startswith("git 不可用")


def test_apply_diff_reports_timeout(tmp_path: Path, monkeypatch):
    ws = H.make_workspace(tmp_path, {"app.py": H.APP_PY})

    def _raise(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="git apply", timeout=30)

    monkeypatch.setattr("audit.fix.patcher.subprocess.run", _raise)
    ok, msg = apply_diff(ws, H.BARE_EXCEPT_DIFF)
    assert ok is False
    assert "超时" in msg


# ---------------------------------------------------------------- R4-3 回归：删除型补丁


def test_apply_diff_applies_delete_patch(tmp_path: Path):
    """R4-3 前置：删除型补丁（+++ /dev/null）可正常应用，目标文件被删除。"""
    ws = H.make_workspace(tmp_path, {"victim.py": H.VICTIM_PY})
    ok, msg = apply_diff(ws, H.DELETE_VICTIM_DIFF)
    assert ok, msg
    assert not ws.abs_path("victim.py").exists()


def test_apply_diff_detects_silently_skipped_delete(tmp_path: Path, monkeypatch):
    """R4-3：删除型补丁被 git 静默跳过（exit 0 无变更）时生效性校验必须报错。

    修复前 _diff_targets 只解析 +++ 侧，删除补丁目标集合为空，
    before 快照为空 → 校验形同虚设，静默跳过被误判为成功。
    """

    class _FakeProc:
        returncode = 0
        stdout = b""
        stderr = b""

    def _fake_run(*args: object, **kwargs: object) -> _FakeProc:
        return _FakeProc()  # git 声称成功，但什么都没做

    monkeypatch.setattr("audit.fix.patcher.subprocess.run", _fake_run)
    ws = H.make_workspace(tmp_path, {"victim.py": H.VICTIM_PY})
    ok, msg = apply_diff(ws, H.DELETE_VICTIM_DIFF)
    assert ok is False
    assert "未产生任何变更" in msg
    assert ws.abs_path("victim.py").read_bytes() == H.VICTIM_PY.encode("utf-8")


# ---------------------------------------------------------------- R1-11 回归


def test_ensure_standalone_repo_removes_broken_git_skeleton(tmp_path: Path):
    """残缺 .git 骨架（指针文件 / 非仓库目录）被删除并强制 init 为有效仓库。"""
    import subprocess as sp

    from audit.fix.patcher import _ensure_standalone_repo

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = 1\n", encoding="utf-8")

    # 场景 1：.git 是 worktree 指针文件（实体未随副本复制 → rev-parse 必败）
    (src / ".git").write_text("gitdir: /somewhere/else/.git\n", encoding="utf-8")
    _ensure_standalone_repo(src)
    assert sp.run(["git", "-C", str(src), "rev-parse", "--git-dir"], capture_output=True).returncode == 0

    # 场景 2：.git 是空目录（骨架，无仓库元数据）
    src2 = tmp_path / "src2"
    src2.mkdir()
    (src2 / ".git").mkdir()
    _ensure_standalone_repo(src2)
    assert sp.run(["git", "-C", str(src2), "rev-parse", "--git-dir"], capture_output=True).returncode == 0

    # 场景 3：已是有效仓库 → 幂等，不重建
    head_before = (src / ".git" / "HEAD").read_bytes()
    _ensure_standalone_repo(src)
    assert (src / ".git" / "HEAD").read_bytes() == head_before
