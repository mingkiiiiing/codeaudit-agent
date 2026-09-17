"""scanner 契约单测：run(ctx) 挂点协议——异常输入不抛、错误记账、开关、Issue 字段完整性。

用 SimpleNamespace 构造最小 ctx（挂点契约只依赖 ctx.workspace.src_root 与
ctx.extra，与 audit.pipeline.PipelineContext 的鸭子类型兼容）。
"""

from __future__ import annotations

from types import SimpleNamespace

from audit.depcheck import scanner
from audit.models import Category, Issue, IssueSource, Severity


def make_ctx(src_root, extra=None) -> SimpleNamespace:
    return SimpleNamespace(
        workspace=SimpleNamespace(src_root=src_root),
        extra=extra if extra is not None else {},
    )


# ---------------------------------------------------------------- 异常输入


class TestAbnormalInputs:
    def test_missing_src_root_returns_empty(self, tmp_path):
        ctx = make_ctx(tmp_path / "no-such-dir")
        assert scanner.run(ctx) == []

    def test_none_src_root_returns_empty(self):
        ctx = SimpleNamespace(workspace=SimpleNamespace(src_root=None), extra={})
        assert scanner.run(ctx) == []

    def test_workspace_without_src_root_attr(self):
        ctx = SimpleNamespace(workspace=SimpleNamespace(), extra={})
        assert scanner.run(ctx) == []

    def test_no_workspace_attr(self):
        ctx = SimpleNamespace(extra={})
        assert scanner.run(ctx) == []

    def test_broken_extra_still_returns(self, tmp_path):
        # extra 不可用时只放弃记账，扫描照常
        ctx = SimpleNamespace(workspace=SimpleNamespace(src_root=tmp_path), extra=None)
        assert isinstance(scanner.run(ctx), list)

    def test_src_root_not_a_path(self):
        ctx = SimpleNamespace(workspace=SimpleNamespace(src_root=12345), extra={})
        assert scanner.run(ctx) == []


# ---------------------------------------------------------------- 正常扫描与 Issue 字段


class TestRunContract:
    def _project(self, tmp_path):
        (tmp_path / "requirements.txt").write_text(
            "jinja2==3.1.2\nrequests>=2.25.0,<2.31.0\n", encoding="utf-8"
        )
        (tmp_path / ".env").write_text("DB_PASSWORD=hunter2prod-secret\n", encoding="utf-8")
        return tmp_path

    def test_full_project_issue_fields(self, tmp_path):
        self._project(tmp_path)
        issues = scanner.run(make_ctx(tmp_path))
        by_rule = {}
        for issue in issues:
            for e in issue.evidence:
                if e.startswith("rule:"):
                    by_rule[e[len("rule:"):]] = issue
        assert "DEP-CVE-CVE-2024-22195" in by_rule
        assert "DEP-CVE-CVE-2023-32681" in by_rule
        assert "CFG-SECRET" in by_rule

        for issue in issues:
            assert isinstance(issue, Issue)
            assert issue.id == ""  # engine 统一补号
            assert issue.category in (Category.SECURITY, Category.STYLE)
            assert isinstance(issue.severity, Severity)
            assert issue.title and len(issue.title) <= 120
            assert "\\" not in issue.file  # posix 相对路径
            assert issue.line_start >= 1 and issue.line_end >= issue.line_start
            assert issue.evidence and issue.suggestion
            assert issue.confidence == 0.7
            assert issue.source == IssueSource.RULE

    def test_relative_posix_paths(self, tmp_path):
        sub = tmp_path / "deploy"
        sub.mkdir()
        (sub / "config.yaml").write_text("password: hunter2prod-secret\n", encoding="utf-8")
        issues = scanner.run(make_ctx(tmp_path))
        assert [i.file for i in issues] == ["deploy/config.yaml"]
        assert issues[0].line_start == 1

    def test_duplicate_pin_reported(self, tmp_path):
        (tmp_path / "requirements.txt").write_text(
            "requests==2.1.0\nflask==2.0.0\nrequests==2.25.0\n", encoding="utf-8"
        )
        issues = scanner.run(make_ctx(tmp_path))
        dups = [i for i in issues if any(e == "rule:DEP-DUPLICATE" for e in i.evidence)]
        assert len(dups) == 1
        assert dups[0].category == Category.STYLE
        assert dups[0].severity == Severity.LOW
        assert dups[0].line_start == 3  # 报在后出现的钉扎行

    def test_same_pin_twice_not_reported(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("requests==2.1.0\nrequests==2.1.0\n", encoding="utf-8")
        issues = scanner.run(make_ctx(tmp_path))
        assert all(not any(e == "rule:DEP-DUPLICATE" for e in i.evidence) for i in issues)

    def test_bad_manifest_does_not_crash(self, tmp_path):
        (tmp_path / "package.json").write_text("{broken json", encoding="utf-8")
        (tmp_path / ".env").write_text("TOKEN=real-secret-value\n", encoding="utf-8")
        issues = scanner.run(make_ctx(tmp_path))
        assert len(issues) == 1  # 坏清单跳过，密钥照常报
        assert any(e == "rule:CFG-SECRET" for i in issues for e in i.evidence)

    def test_file_budget_respected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scanner, "MAX_SCAN_FILES", 1)
        (tmp_path / "requirements.txt").write_text("jinja2==3.1.2\n", encoding="utf-8")
        (tmp_path / ".env").write_text("SECRET=real-secret-value\n", encoding="utf-8")
        issues = scanner.run(make_ctx(tmp_path))
        assert len(issues) == 1  # 只处理预算内的第一个文件


# ---------------------------------------------------------------- 环境开关与错误记账


class TestSwitchesAndErrors:
    def test_depcheck_off_keeps_cfgsecret(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEAUDIT_DEPCHECK_OFF", "1")
        (tmp_path / "requirements.txt").write_text("jinja2==3.1.2\n", encoding="utf-8")
        (tmp_path / ".env").write_text("SECRET=real-secret-value\n", encoding="utf-8")
        issues = scanner.run(make_ctx(tmp_path))
        assert [e for i in issues for e in i.evidence if e.startswith("rule:")] == ["rule:CFG-SECRET"]

    def test_cfgsecret_off_keeps_depcheck(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEAUDIT_CFGSECRET_OFF", "1")
        (tmp_path / "requirements.txt").write_text("jinja2==3.1.2\n", encoding="utf-8")
        (tmp_path / ".env").write_text("SECRET=real-secret-value\n", encoding="utf-8")
        issues = scanner.run(make_ctx(tmp_path))
        rules = [e for i in issues for e in i.evidence if e.startswith("rule:")]
        assert rules == ["rule:DEP-CVE-CVE-2024-22195"]

    def test_submodule_exception_recorded_not_raised(self, tmp_path, monkeypatch):
        def boom(_src_root):
            raise RuntimeError("injected failure")

        monkeypatch.setattr(scanner, "scan_dependencies", boom)
        ctx = make_ctx(tmp_path)
        (tmp_path / ".env").write_text("SECRET=real-secret-value\n", encoding="utf-8")
        issues = scanner.run(ctx)  # 不抛
        assert ctx.extra["post_scan_errors"]["depcheck.manifest"].startswith("RuntimeError")
        assert len(issues) == 1  # 密钥扫描不受影响

    def test_errors_recorded_into_existing_extra_dict(self, tmp_path, monkeypatch):
        extra: dict = {"post_scan_errors": {"pre.existing": "x"}}
        monkeypatch.setattr(scanner, "scan_dependencies", lambda _s: (_ for _ in ()).throw(ValueError("bad")))
        ctx = make_ctx(tmp_path, extra=extra)
        scanner.run(ctx)
        assert "pre.existing" in ctx.extra["post_scan_errors"]  # setdefault 不覆盖既有键
        assert "depcheck.manifest" in ctx.extra["post_scan_errors"]


# ---------------------------------------------------------------- 真实 PipelineContext 兼容


class TestRealPipelineContext:
    def test_run_with_real_context(self, tmp_path):
        """用真实 WorkspaceContext + PipelineContext 验证鸭子类型兼容（不跑引擎）。"""
        from audit.config import AuditConfig
        from audit.llm.base import FakeLLMClient
        from audit.pipeline import PipelineContext
        from audit.workspace import WorkspaceContext

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "requirements.txt").write_text("jinja2==3.1.2\n", encoding="utf-8")
        workspace = WorkspaceContext(
            audit_id="depcheck",
            src_root=tmp_path / "src",
            work_root=tmp_path,
            db_path=tmp_path / "index.db",
        )

        async def _emit(event: dict) -> None:
            return None

        ctx = PipelineContext(
            config=AuditConfig(source_path=str(workspace.src_root)),
            workspace=workspace,
            llm=FakeLLMClient(),
            emitter=_emit,
        )
        issues = scanner.run(ctx)
        assert len(issues) == 1
        assert issues[0].file == "requirements.txt"
        assert any(e == "rule:DEP-CVE-CVE-2024-22195" for e in issues[0].evidence)
