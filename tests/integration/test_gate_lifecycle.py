"""联调场景③（R2 必测场景 3）：基线两段式 CI 门禁（真实流水线，无 LLM）。

第一段：``run --report-baseline --check --fail-on high`` → 真实缺陷命中阈值 →
退出码 3，且基线文件已写出（schema_version=1，指纹数 == 问题数）；
第二段：``run --baseline <file>`` 同门禁 → 全部命中基线被抑制 → 退出码 0、
stats.suppressed > 0、summary 无 high。

门禁文案位置不做方向性断言（A3 正按 ISSUE-R3-12 把它从 stdout 挪到 stderr），
统一在 out+err 合并流中断言内容。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import cli


def test_baseline_two_phase_gate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    work1, out1 = tmp_path / "work1", tmp_path / "out1"
    baseline = tmp_path / "baseline.json"

    # ---- 第一段：建立基线，门禁按 high 失败（demo_proj 有 2 critical + 3 high）
    rc1 = cli.main(
        [
            "run", "tests/samples/demo_proj",
            "--no-llm",
            "--work-root", str(work1),
            "--out", str(out1),
            "--report-baseline", str(baseline),
            "--check", "--fail-on", "high",
        ]
    )
    assert rc1 == 3
    captured1 = capsys.readouterr()
    combined1 = captured1.out + captured1.err
    assert "门禁" in combined1 and "未通过" in combined1
    assert "阈值 high" in combined1

    # 基线文件真实写出：schema_version=1，指纹数 == 第一段剩余问题数
    assert baseline.is_file()
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["created_at"]
    report1 = json.loads((out1 / "report.json").read_text(encoding="utf-8"))
    total_issues1 = sum(report1["summary"].values())
    assert total_issues1 >= 9
    assert len(payload["fingerprints"]) == total_issues1

    # ---- 第二段：同一输入 + 基线 → 全部抑制 → 门禁通过
    work2, out2 = tmp_path / "work2", tmp_path / "out2"
    rc2 = cli.main(
        [
            "run", "tests/samples/demo_proj",
            "--no-llm",
            "--work-root", str(work2),
            "--out", str(out2),
            "--baseline", str(baseline),
            "--check", "--fail-on", "high",
        ]
    )
    assert rc2 == 0
    captured2 = capsys.readouterr()
    combined2 = captured2.out + captured2.err
    assert "门禁" in combined2 and "通过" in combined2

    report2 = json.loads((out2 / "report.json").read_text(encoding="utf-8"))
    assert report2["stats"]["suppressed"] == total_issues1  # 已知问题全部命中基线
    assert report2["summary"].get("high", 0) == 0  # summary 无 high
    assert sum(report2["summary"].values()) == 0
    assert report2["issues"] == []
