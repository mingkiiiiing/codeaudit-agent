"""联调场景⑦（R2 必测场景 7）：配置文件贯通——.codeaudit.toml 三键 + 未知键警告。

契约 v1.4 合成优先级：默认值 < 配置文件 < 环境变量 < CLI 显式参数
（audit/config.py from_sources；CLI 自动发现 CWD 下的 .codeaudit.toml）。

覆盖（真实流水线 / 真实 CLI，无 LLM）：
- ``languages = ["python"]``：多语言项目的检测结果收窄到 Python
  （对照组：不配置时 JS 问题出现）；
- ``do_fix = true``：配置文件的布尔键驱动流水线真实执行 fix 阶段
  （而非"跳过（未启用 --fix）"占位）；
- ``fail_on_severity``：--check 时门禁阈值取自配置文件，真实缺陷 → 退出码 3；
- 未知键：config_warnings → CLI 打印「[配置警告]」进 stderr，审计照常完成。
"""

from __future__ import annotations

import json
from pathlib import Path

import cli
from audit.config import AuditConfig
from audit.orchestrator.pipeline import run_audit

_PY_ISSUE = "def collect(items, bucket=[]):\n    bucket.extend(items)\n    return bucket\n"
# JS 样本：var 声明 + console.log，必被 JS-VAR / JS-CONSOLE-LOG 命中（low）
_JS_ISSUE = "var counter = 0;\n\nfunction bump() {\n  counter += 1;\n  console.log(counter);\n}\n\nbump();\n"
# 低危样本：print 调试输出（PY-PRINT-DEBUG，low）——默认门禁阈值 high 下通过
_LOW_ISSUE_PY = 'import logging\n\nlogger = logging.getLogger(__name__)\n\n\ndef main():\n    print("debug output")\n    logger.info("done")\n'


def _write_config(project: Path, body: str) -> Path:
    config_path = project / ".codeaudit.toml"
    config_path.write_text(body, encoding="utf-8", newline="\n")
    return config_path


def _run_cli_on(project: Path, tmp_path: Path, capsys, monkeypatch, out_name: str, *extra: str) -> Path:
    """以项目目录为 CWD 运行 CLI（走 .codeaudit.toml 自动发现的真实路径），返回报告目录。"""
    monkeypatch.chdir(project)  # from_sources 在 CWD 下自动发现配置文件
    out_dir = tmp_path / out_name
    rc = cli.main(
        [
            "run", str(project), "--no-llm",
            "--work-root", str(tmp_path / "work"), "--out", str(out_dir),
            *extra,
        ]
    )
    assert rc == 0
    capsys.readouterr()  # 清空缓冲，避免用例间输出串扰
    return out_dir


# ---------------------------------------------------------------- languages 收窄


def test_config_languages_narrows_detection(tmp_path: Path, make_project, capsys, monkeypatch) -> None:
    project = make_project({"app.py": _PY_ISSUE, "scripts/tool.js": _JS_ISSUE}, name="langs")

    # 对照组：不配置时自动检测全部语言 → JS 问题出现
    out_auto = _run_cli_on(project, tmp_path, capsys, monkeypatch, "out_auto")
    report = json.loads((out_auto / "report.json").read_text(encoding="utf-8"))
    issue_files = {i["file"] for i in report["issues"]}
    assert any(f.endswith(".js") for f in issue_files), issue_files
    assert any(f.endswith(".py") for f in issue_files), issue_files

    # 配置 languages = ["python"] → 收窄：JS 问题消失
    _write_config(project, 'languages = ["python"]\n')
    out_narrow = _run_cli_on(project, tmp_path, capsys, monkeypatch, "out_narrow")
    narrowed = json.loads((out_narrow / "report.json").read_text(encoding="utf-8"))
    narrowed_files = {i["file"] for i in narrowed["issues"]}
    assert narrowed_files and all(f.endswith(".py") for f in narrowed_files)


# ---------------------------------------------------------------- do_fix 贯通（流水线级断言）


async def test_config_do_fix_drives_real_fix_stage(
    tmp_path: Path, make_project, stage_order, stage_messages
) -> None:
    project = make_project({"app.py": _PY_ISSUE}, name="dofix")
    _write_config(project, "do_fix = true\n")

    # 配置文件 → AuditConfig 走真实的 from_sources 合成（非直接构造）
    config = AuditConfig.from_sources(
        cli_overrides={
            "source_path": str(project),
            "work_root": str(tmp_path / "work"),
            "out_dir": str(tmp_path / "out"),
        },
        config_file=str(project / ".codeaudit.toml"),
    )
    assert config.do_fix is True  # 配置文件的布尔键真实写入契约字段

    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)

    report = await run_audit(config, emit)

    fix_msgs = stage_messages(events, "fix")
    assert any(m.startswith("修复阶段") for m in fix_msgs), fix_msgs  # fix 阶段真实执行
    assert not any("跳过（未启用 --fix）" in m for m in fix_msgs)
    # 无 LLM（FakeLLM 空脚本）→ 尝试修复 high 缺陷但生成失败，流水线照常产出报告
    assert report is not None
    assert stage_order(events)[-1] == "done"


# ---------------------------------------------------------------- fail_on_severity 进门禁


def test_config_fail_on_severity_gates(tmp_path: Path, make_project, capsys, monkeypatch) -> None:
    """配置文件阈值真正驱动门禁：low 缺陷 + fail_on_severity="low" → rc 3。

    对照组先以默认阈值（high）跑同一项目 → rc 0，证明 rc 3 只能来自配置文件阈值。
    """
    project = make_project({"app.py": _LOW_ISSUE_PY}, name="gatecfg")  # 仅 low 缺陷
    monkeypatch.chdir(project)

    # 对照组：无配置文件，默认阈值 high → low 问题不触发门禁
    rc_default = cli.main(
        [
            "run", str(project), "--no-llm",
            "--work-root", str(tmp_path / "work"), "--out", str(tmp_path / "out_default"),
            "--check",
        ]
    )
    assert rc_default == 0
    capsys.readouterr()

    # 配置 fail_on_severity = "low" → 同一项目门禁失败，且消息呈现配置阈值
    _write_config(project, 'fail_on_severity = "low"\n')
    rc = cli.main(
        [
            "run", str(project), "--no-llm",
            "--work-root", str(tmp_path / "work"), "--out", str(tmp_path / "out_gate"),
            "--check",
        ]
    )
    assert rc == 3
    captured = capsys.readouterr()
    assert "阈值 low" in (captured.out + captured.err)


# ---------------------------------------------------------------- 未知键警告


def test_config_unknown_key_warns_on_stderr(tmp_path: Path, make_project, capsys, monkeypatch) -> None:
    project = make_project({"app.py": _PY_ISSUE}, name="unkcfg")
    _write_config(project, 'languages = ["python"]\nno_such_option = 42\n')

    monkeypatch.chdir(project)
    rc = cli.main(
        [
            "run", str(project), "--no-llm",
            "--work-root", str(tmp_path / "work"), "--out", str(tmp_path / "out_warn"),
        ]
    )
    assert rc == 0  # 警告不阻断审计
    captured = capsys.readouterr()
    assert "[配置警告]" in captured.err
    assert "no_such_option" in captured.err
    assert "no_such_option" not in captured.out  # 警告只进 stderr，不进 stdout
    report = json.loads((tmp_path / "out_warn" / "report.json").read_text(encoding="utf-8"))
    assert sum(report["summary"].values()) >= 1
