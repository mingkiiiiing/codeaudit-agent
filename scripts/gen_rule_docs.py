"""规则手册生成器（W6-A3）：从全局规则注册表渲染单页 docs-site/rules.md。

用法（项目根目录）::

    python scripts/gen_rule_docs.py                # 生成到默认 docs-site/rules.md
    python scripts/gen_rule_docs.py --out x.md     # 生成到指定路径

产物结构：规则总表（按语言 + 类别分组）→ 每规则小节（说明 + 可选正反示例）→ 统计段。
生成是纯函数式的：内容只来源于 DEFAULT_REGISTRY，排序显式固定、不写入时间戳，
重复运行字节一致（幂等）。规则带 good_example/bad_example 类属性（契约 v1.6）时
才渲染"正反示例"小节，存量规则缺省为空串、自动省略。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from audit.detect.base import Rule, RuleRegistry  # noqa: E402
from audit.detect.registry import get_registry  # noqa: E402
from audit.models import Category, Severity  # noqa: E402

LANG_ORDER = ("python", "javascript", "typescript")
LANG_TITLES = {"python": "Python", "javascript": "JavaScript", "typescript": "TypeScript"}
LANG_MD_FENCE = {"python": "python", "javascript": "javascript", "typescript": "typescript"}

CATEGORY_ORDER = (Category.BUG, Category.PERFORMANCE, Category.STYLE, Category.SECURITY)
CATEGORY_TITLES = {
    Category.BUG: "bug 缺陷",
    Category.PERFORMANCE: "performance 性能",
    Category.STYLE: "style 风格",
    Category.SECURITY: "security 安全",
}

SEVERITY_ORDER = (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW)

GENERATOR_NOTE = (
    "> 本页由 `scripts/gen_rule_docs.py` 从规则注册表（audit/detect/registry.py 的 "
    "DEFAULT_REGISTRY）自动生成——请勿手改；新增或调整规则后重新运行即可。"
)


def _md_escape(text: str) -> str:
    """表格单元格转义：竖线会截断表格列。"""
    return text.replace("|", "\\|").replace("\n", " ")


def _rule_langs(rule: Rule) -> str:
    return "、".join(LANG_TITLES[lang] for lang in LANG_ORDER if lang in rule.languages)


def _has_examples(rule: Rule) -> bool:
    return bool(getattr(rule, "good_example", "") or getattr(rule, "bad_example", ""))


def _render_summary_table(rules: list[Rule]) -> list[str]:
    lines = ["## 规则总表", ""]
    for lang in LANG_ORDER:
        lang_rules = sorted((r for r in rules if lang in r.languages), key=lambda r: r.id)
        if not lang_rules:
            continue
        lines.append(f"### {LANG_TITLES[lang]}（{len(lang_rules)} 条）")
        lines.append("")
        for category in CATEGORY_ORDER:
            group = [r for r in lang_rules if r.category == category]
            if not group:
                continue
            lines.append(f"#### {CATEGORY_TITLES[category]}（{len(group)} 条）")
            lines.append("")
            lines.append("| 规则 ID | 语言 | 类别 | 严重度 | 说明 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for r in group:
                lines.append(
                    "| {rid} | {langs} | {cat} | {sev} | {desc} |".format(
                        rid=r.id,
                        langs=_rule_langs(r),
                        cat=r.category.value,
                        sev=r.severity.value,
                        desc=_md_escape(r.description),
                    )
                )
            lines.append("")
    return lines


def _render_examples_block(rule: Rule) -> list[str]:
    lines: list[str] = []
    bad = getattr(rule, "bad_example", "")
    good = getattr(rule, "good_example", "")
    primary_lang = next((lang for lang in LANG_ORDER if lang in rule.languages), "text")
    fence = LANG_MD_FENCE.get(primary_lang, "text")
    if bad:
        lines.append("**反例**")
        lines.append("")
        lines.append(f"```{fence}")
        lines.append(bad.rstrip("\n"))
        lines.append("```")
        lines.append("")
    if good:
        lines.append("**正例**")
        lines.append("")
        lines.append(f"```{fence}")
        lines.append(good.rstrip("\n"))
        lines.append("```")
        lines.append("")
    return lines


def _render_details(rules: list[Rule]) -> list[str]:
    lines = ["## 规则明细", ""]
    for rule in sorted(rules, key=lambda r: r.id):
        lines.append(f"### {rule.id}")
        lines.append("")
        lines.append(f"- 语言：{_rule_langs(rule)}")
        lines.append(f"- 类别：{CATEGORY_TITLES[rule.category]}（{rule.category.value}）")
        lines.append(f"- 严重度：{rule.severity.value}")
        lines.append("")
        lines.append(rule.description)
        lines.append("")
        if _has_examples(rule):
            lines.extend(_render_examples_block(rule))
    return lines


def _render_stats(rules: list[Rule]) -> list[str]:
    by_lang = {lang: sum(1 for r in rules if lang in r.languages) for lang in LANG_ORDER}
    by_category = {cat: sum(1 for r in rules if r.category == cat) for cat in CATEGORY_ORDER}
    by_severity = {sev: sum(1 for r in rules if r.severity == sev) for sev in SEVERITY_ORDER}
    lines = [
        "## 统计",
        "",
        "| 维度 | 分布 |",
        "| --- | --- |",
        f"| 规则总数 | {len(rules)} |",
        "| 按语言 | {} |".format(
            " / ".join(f"{LANG_TITLES[lang]} {count}" for lang, count in by_lang.items() if count)
            + "（JS/TS 共享规则在两种语言下重复计数）"
        ) + " |",
        "| 按类别 | {} |".format(
            " / ".join(f"{cat.value} {count}" for cat, count in by_category.items() if count)
        ),
        "| 按严重度 | {} |".format(
            " / ".join(f"{sev.value} {count}" for sev, count in by_severity.items() if count)
        ),
        "",
    ]
    return lines


def build_markdown(registry: RuleRegistry) -> str:
    """渲染规则手册 Markdown（纯函数，输出与调用次数无关）。"""
    rules = registry.all_rules
    lines: list[str] = [
        "# 规则手册",
        "",
        GENERATOR_NOTE,
        "",
        f"当前共 **{len(rules)}** 条内置规则；JS/TS 共享规则（security 类）同时作用于两种语言。",
        "",
        *_render_summary_table(rules),
        *_render_details(rules),
        *_render_stats(rules),
    ]
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    # Windows runner 的 stdout 可能是 cp1252：中文进度信息强制 UTF-8（CI 实证）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(
        description="从 DEFAULT_REGISTRY 生成单页规则手册（默认 docs-site/rules.md）"
    )
    parser.add_argument(
        "--out",
        default=str(ROOT / "docs-site" / "rules.md"),
        help="输出路径（相对路径按项目根解析）",
    )
    args = parser.parse_args(argv)
    registry = get_registry()
    content = build_markdown(registry)
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8", newline="\n")
    print(f"[gen_rule_docs] 已生成 {out_path}（共 {len(registry.all_rules)} 条规则）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
