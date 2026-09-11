"""联调场景①（R2 必测场景 1）：CLI 真实全流程——python cli.py run demo_proj。

用真实子进程跑 ``cli.py run tests/samples/demo_proj --no-llm``：
- 退出码 0；stdout 呈现审计摘要；
- json / md / html 三格式报告齐全且非空；
- report.json 可被 AuditReport.from_dict 回读，summary 与 issues 一致；
- 金标规则（tests/samples/demo_proj/GOLDEN_ISSUES.md 的 G2~G11 组）命中 >= 9 组。
"""

from __future__ import annotations

import json
from pathlib import Path

from audit.models import AuditReport

# 金标规则组：GOLDEN_ISSUES.md 中 G2~G11 十组缺陷对应的内置规则 id。
# （G1 的 None 解引用在现行规则集中由 LLM 通道负责，纯规则模式不参与计数；
#   G12 与 G2 同位置，去重后归并为一组。）
GOLDEN_RULE_GROUPS = {
    "PY-SQL-INJECTION",  # G2/G12 orders.py SQL 拼接（critical）
    "PY-LIST-MEMBERSHIP",  # G3 orders.py list 成员判断（medium）
    "PY-OPEN-NO-CLOSE",  # G4 orders.py open 未关闭（high）
    "PY-BARE-EXCEPT",  # G5 orders.py 裸 except（medium）
    "PY-MUTABLE-DEFAULT",  # G6 mathx.py 可变默认参数（high）
    "PY-EQ-NONE",  # G7 mathx.py == None（low）
    "PY-STR-CONCAT-LOOP",  # G8 mathx.py 循环内字符串拼接（low）
    "PY-MAGIC-NUMBER",  # G9 mathx.py 魔法数字（low）
    "PY-HARDCODED-SECRET",  # G10 config.py 硬编码密钥（critical）
    "PY-NO-TIMEOUT",  # G11 net.py urlopen 无 timeout（medium）
}


def _hit_rule_ids(report: AuditReport) -> set[str]:
    ids: set[str] = set()
    for issue in report.issues:
        for item in issue.evidence:
            if item.startswith("rule:"):
                ids.add(item[len("rule:") :].strip())
    return ids


def test_cli_run_demo_proj_real_full_flow(run_cli_subprocess, tmp_path: Path) -> None:
    work_root = tmp_path / "work"
    out_dir = tmp_path / "out"
    proc = run_cli_subprocess(
        [
            "run", "tests/samples/demo_proj",
            "--no-llm",
            "--work-root", str(work_root),
            "--out", str(out_dir),
        ]
    )

    # 退出码 0 + 摘要输出
    assert proc.returncode == 0, proc.stderr
    assert "审计完成" in proc.stdout
    assert "demo_proj" in proc.stdout

    # 三格式齐全且非空
    products = {name: out_dir / f"report.{name}" for name in ("json", "md", "html")}
    for name, path in products.items():
        assert path.is_file(), f"缺少 {name} 报告"
        assert path.stat().st_size > 0, f"{name} 报告为空"

    # json 可整体解析并 from_dict 回读
    data = json.loads(products["json"].read_text(encoding="utf-8"))
    report = AuditReport.from_dict(data)
    assert report.project_name == "demo_proj"
    assert report.audit_id == data["audit_id"]
    assert report.stats.duration_sec > 0

    # summary 与 issues 一致
    assert sum(report.summary.values()) == len(report.issues)
    assert report.summary["critical"] >= 1  # SQL 注入 / 硬编码密钥

    # 金标规则命中 >= 9 组（G2~G11 中至少 9 组）
    hits = _hit_rule_ids(report) & GOLDEN_RULE_GROUPS
    assert len(hits) >= 9, f"金标规则命中不足 9 组：{sorted(hits)}"

    # md / html 报告承载同一份结果（健康分与问题 ID 出现）
    md_text = products["md"].read_text(encoding="utf-8")
    html_text = products["html"].read_text(encoding="utf-8")
    assert "健康分" in md_text and report.issues[0].id in md_text
    assert "<html" in html_text.lower() and report.issues[0].id in html_text

    # 工作区确实落在显式 --work-root 下（审计 ID 子目录 + 工作副本 src/）
    task_dirs = [p for p in work_root.iterdir() if p.is_dir()]
    assert task_dirs and (task_dirs[0] / "src").is_dir()
