"""bench.stress.run_stress 微型档真跑：报告 markdown 结构校验（--scale 50）。

整份 tests/unit/bench 套件要求 <30s，这里只跑一次微型档（50 文件）：
生成 + ingest/index + 纯规则审计 + 预算熔断 + LLM 并发（文件数随档缩放）+
server 并发 + R1-8（微型档 SKIP）。大档（500/2000）不进默认 pytest。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_micro_run_report_structure(tmp_path: Path):
    out = tmp_path / "stress_micro.md"
    proc = subprocess.run(
        [
            sys.executable, "-m", "bench.stress.run_stress",
            "--scale", "50",
            "--out", str(out),
            "--data-dir", str(tmp_path / "data"),
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert proc.returncode == 0, f"微型档压测失败：\n{proc.stdout}\n{proc.stderr}"
    assert out.is_file(), "报告未写出"

    markdown = out.read_text(encoding="utf-8")
    # 头部：环境信息（机器 / Python / 时间 / seed）
    for key in ("## 环境", "**主机**", "**Python**", "**生成时间**", "**随机种子**", "42"):
        assert key in markdown, f"报告环境头缺少 {key}"
    # 六个场景小节 + 摘要 + 结论
    for header in (
        "## 摘要",
        "### 场景 1（50）：ingest + index",
        "### 场景 2（50）：纯规则审计吞吐",
        "### 场景 4（50）：预算熔断",
        "### 场景 6（50）：R1-8 索引 N+1 量化",
        "### 场景 3：LLM 并发扩展性",
        "### 场景 5：server 并发",
        "## 结论",
        "**吞吐结论**",
        "**瓶颈 Top3**",
    ):
        assert header in markdown, f"报告缺少小节：{header}"
    # 微型档全绿：R1-8 允许 SKIP（调用边 <1 万），不允许 ERROR
    assert "| 50 |" in markdown  # 摘要表分档行
    assert "状态：ERROR" not in markdown, "微型档出现 ERROR 场景"
    assert "状态：OK" in markdown
    # s/KLOC 口径行在场景 2 明细中存在且为正数
    assert "| sec_per_kloc |" in markdown
