"""W3-A1 离线基线 run 记录测试：bench/results/run_20260911_offline.md 的结构与诚实性声明。

该记录由 bench/datasets/gen_offline.py --with-run 产出（write_run_record + 追加小节），
本测试只做只读结构校验（不重跑审计）：
- 头部 meta：日期 / model=offline-fakellm（纯规则基线）/ prompt_version=n/a / 配置摘要；
- 指标表：Precision / Recall（critical+high）、耗时 s/KLOC、tokens=0；
- 消融对比：full / −verify / rules_only 三配置均为真实执行（无"待接入"占位）；
- 必需小节：Precision / Recall / 备注 / 离线声明，且离线声明写明 LLM 指标待真跑。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RUN_RECORD_PATH = ROOT / "bench" / "results" / "run_20260911_offline.md"
MINUS = "\u2212"  # 消融配置名中的减号（U+2212）

REQUIRED_SECTIONS = ("## 备注", "## 离线声明", "## 项目明细")
OFFLINE_STATEMENT = "待提供 GLM_API_KEY 后真跑"


def _record_text() -> str:
    assert RUN_RECORD_PATH.is_file(), (
        f"run 记录不存在：{RUN_RECORD_PATH}（请先运行 python bench/datasets/gen_offline.py --with-run）"
    )
    return RUN_RECORD_PATH.read_text(encoding="utf-8")


def test_run_record_exists_with_required_sections() -> None:
    """记录存在且含 Precision/Recall 指标行与必需小节（备注/离线声明/项目明细）。"""
    text = _record_text()
    assert "Precision (critical+high)" in text
    assert "Recall (critical+high)" in text
    for section in REQUIRED_SECTIONS:
        assert section in text, f"缺少必需小节：{section}"


def test_run_record_header_marks_offline_baseline() -> None:
    """头部 meta 明确：离线日期、model=offline-fakellm、prompt_ver=n/a、10 项目配置摘要。"""
    header = _record_text().splitlines()[0]
    assert "## Run 2026-09-11" in header
    assert "model=offline-fakellm（纯规则基线）" in header
    assert "prompt_ver=n/a" in header
    assert "× 10 项目" in header


def test_run_record_declares_pending_llm_metrics() -> None:
    """备注与离线声明必须写明：本记录为离线纯规则基线，LLM 指标待提供 GLM_API_KEY 后真跑。"""
    text = _record_text()
    assert "离线纯规则基线" in text
    assert OFFLINE_STATEMENT in text, "缺少离线声明关键句（LLM 指标待真跑）"
    assert "## 离线声明" in text
    declaration = text.split("## 离线声明", 1)[1]
    assert "零 LLM 调用" in declaration or "零网络" in declaration


def test_run_record_metrics_shape() -> None:
    """指标行形态：Precision/Recall/F1 为 [0,1] 数值、耗时为 s/KLOC、tokens/KLOC 为 0。"""
    import re

    text = _record_text()

    def _metric_value(name: str) -> float:
        row = next(line for line in text.splitlines() if line.startswith(f"| {name} "))
        return float(row.split("|")[2].strip())

    for name in ("Precision (critical+high)", "Recall (critical+high)", "F1 (critical+high)"):
        value = _metric_value(name)
        assert 0.0 <= value <= 1.0, f"{name} 超出 [0,1]：{value}"

    assert "| 耗时 P50 / P90 |" in text, "缺少 s/KLOC 耗时基线行"
    tokens_row = next(line for line in text.splitlines() if line.startswith("| tokens/KLOC "))
    assert re.search(r"0k prompt \+ 0\.0k completion", tokens_row), f"离线 tokens 应为 0：{tokens_row}"


def test_run_record_ablation_three_real_configs() -> None:
    """消融表包含 full / −verify / rules_only 三配置且全部真实执行（无"待接入"行）。"""
    text = _record_text()
    assert "## 消融对比" in text
    ablation = text.split("## 消融对比", 1)[1].split("## ", 1)[0]
    for name in ("full", f"{MINUS}verify", "rules_only"):
        assert f"| {name} |" in ablation, f"消融表缺少配置行：{name}"
    assert "待接入" not in ablation, "三配置应全部真实执行，不得残留占位备注"
