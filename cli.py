"""CodeAudit Agent 命令行入口。

用法：
  python cli.py run <source_path> [--out DIR] [--work-root DIR] [--lang python ...]
                   [--fix] [--tests] [--no-llm] [--json] [--quiet]
                   [--review-mode simple|tools] [--no-verify]
                   [--fix-max N] [--testgen-max N]
                   [--format json|md|html|sarif] [--check] [--fail-on critical|high|medium|low]
                   [--baseline PATH] [--report-baseline PATH] [--diff REF] [--config PATH]
                   [--disable-rule RULE_ID] [--ignore-path PATTERN]
                   [--version]
  python cli.py index <source_path> [--work-root DIR]
  python cli.py report <report_json> [--format md|html] [--out PATH]
  python cli.py apply <audit_id> [--patch N] [--all-verified] [--yes]
                      [--workdir DIR] [--work-root DIR]
  python cli.py rename <source> <old_name> <new_name> [--yes] [--diff-only]
                       [--lang python]   # safe-rename 符号重命名（W28-A，默认 dry-run 只预览）
  python cli.py diff <audit_a> <audit_b> [--from-seq N] [--to-seq N] [--work-root DIR]
  python cli.py resume <audit_id> [--work-root DIR]   # 续跑被中断的任务（W24-E）
  python cli.py doctor                      # 环境自检（零 API Key 可跑）
  python cli.py init [DIR]                  # 生成 .codeaudit.toml 注释模板骨架
  python cli.py serve [--host 127.0.0.1] [--port 8000] [--workers 1] [--allow-insecure]

退出码约定（docs/09 §3）：0 完成/门禁通过 ｜ 1 运行错误 ｜ 2 bench 保留 ｜ 3 --check 门禁失败。
（doctor：存在"失败"级检查项时返回 1；仅有"警告"级（如 java 语言包未装）仍为 0。）
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import ipaddress
import json
import os
import shutil
import sys
from pathlib import Path

from audit.config import AuditConfig
from audit.orchestrator.pipeline import run_audit_simple  # 模块级导入，便于测试注入替身
from audit.models import AuditReport

# 门禁严重级从高到低（阈值语义：命中 >= 阈值级别的问题即失败）
_GATE_ORDER: tuple[str, ...] = ("critical", "high", "medium", "low")

# W24-B：CLI 实时进度。阶段序号按 README 七阶段流水线口径（①ingest ②index
# ③understand ④detect ⑤fix ⑥testgen ⑦report）；refactor 在编排层是 Stage 4c
# （detect 的子阶段，见 audit/orchestrator/pipeline.py），序号随 detect 记 4。
_STAGE_ORDINAL: dict[str, int] = {
    "ingest": 1,
    "index": 2,
    "understand": 3,
    "detect": 4,
    "refactor": 4,
    "fix": 5,
    "testgen": 6,
    "report": 7,
}

# 阶段中文名（进度行的描述用；编排层"阶段 X 开始/完成"样板事件替换为中文）。
_STAGE_LABELS: dict[str, str] = {
    "ingest": "接入与工作副本",
    "index": "符号索引构建",
    "understand": "架构理解",
    "detect": "双通道检测",
    "refactor": "重构方案生成",
    "fix": "修复补丁生成与验证",
    "testgen": "单测生成与运行",
    "report": "报告聚合与落盘",
}


def _print_progress_event(event: dict[str, object]) -> None:
    """把单条流水线事件打印为 stderr 进度行（W24-B）。

    - 进度一律走 stderr：--check 门禁 stdout 纯净契约（R3-12）不可破坏；
    - 仅 type="progress" 的事件进进度行（其余类型如 resume 的 stage_done 标记
      事件只入列不打印，缺 type 字段的历史事件按 progress 兼容处理）；
    - warning 事件（LLM 未配置 / 预算熔断 / diff 跳过等）以 [警告] 前缀整行展示；
    - init / done 通道不是业务阶段，分别以 [提示] / [完成] 前缀展示；
    - 业务阶段按 `[stage n/7] 描述` 打印，事件携带 current/total 时追加 ` (c/t)`；
      编排层"阶段 X 开始/完成"样板文案替换为中文阶段名（完成态译作"结束"，与
      阶段体自带的"XX完成"消息区分开），其余文案原样透传。
    """
    if str(event.get("type", "progress")) != "progress":
        return
    stage = str(event.get("stage", ""))
    message = str(event.get("message", ""))
    if event.get("warning"):
        print(f"[警告] {message}", file=sys.stderr)
        return
    if stage == "init":
        print(f"[提示] {message}", file=sys.stderr)
        return
    if stage == "done":
        print(f"[完成] {message}", file=sys.stderr)
        return
    ordinal = _STAGE_ORDINAL.get(stage)
    if ordinal is None:  # 未知阶段（前向兼容）：不编号，原样展示
        print(f"[进度] {message}", file=sys.stderr)
        return
    if message == f"阶段 {stage} 开始":
        desc = f"{_STAGE_LABELS.get(stage, stage)}开始"
    elif message == f"阶段 {stage} 完成":
        desc = f"{_STAGE_LABELS.get(stage, stage)}结束"
    else:
        desc = message
    current, total = event.get("current"), event.get("total")
    counter = (
        f" ({current}/{total})"
        if isinstance(current, int) and isinstance(total, int)
        else ""
    )
    print(f"[stage {ordinal}/7] {desc}{counter}", file=sys.stderr)


class _ProgressEvents(list):
    """事件收集列表：每个事件入列时实时打印进度行（W24-B）。

    run_audit_simple 的收集器逐条 append（真实流水线路径），测试替身多用
    extend 批量注入——两处都桥接打印，保证真实运行时进度逐阶段实时可见。
    """

    def append(self, item: object) -> None:
        super().append(item)  # type: ignore[arg-type]
        _print_progress_event(item)  # type: ignore[arg-type]

    def extend(self, items: object) -> None:
        super().extend(items)  # type: ignore[arg-type]
        for item in items:  # type: ignore[union-attr]
            _print_progress_event(item)


def _print_version() -> int:
    """打印 "codeaudit-agent <版本>"（版本单源：audit.__version__），返回退出码 0。"""
    import audit

    print(f"codeaudit-agent {audit.__version__}")
    return 0


def _resolve_config_discovery(args: argparse.Namespace) -> tuple[str, Path | None]:
    """配置文件发现（W18-F2 修复）：显式 --config 优先；否则 CWD 优先、回退被审项目根。

    此前自动发现仅查 CWD——README 承诺"自动发现项目根的 .codeaudit.toml"，从其他
    目录（及 serve 常驻场景）审计项目时配置静默失效。现保持 CWD 行为零变化（CWD
    下存在 .codeaudit.toml 时与旧版完全一致），仅当 CWD 无配置且被审项目为目录时，
    回退到项目根发现（.codeaudit.toml / pyproject.toml [tool.codeaudit]）。
    """
    if args.config:
        return args.config, None
    src = Path(args.source_path)
    if not (Path.cwd() / ".codeaudit.toml").is_file() and src.is_dir():
        if (src / ".codeaudit.toml").is_file() or (src / "pyproject.toml").is_file():
            return "", src
    return "", None


def _build_run_config(args: argparse.Namespace) -> AuditConfig:
    """根据 CLI 参数构造 AuditConfig（契约 v1.4：默认值 < 配置文件 < 环境变量 < CLI 显式参数）。

    仅把用户显式传入的参数写进 cli_overrides（default=None 的键会被 from_sources 跳过）；
    store_true 布尔开关按既有逻辑换算后恒写入。
    """
    overrides: dict[str, object] = {"source_path": args.source_path}
    overrides["do_fix"] = args.fix
    overrides["do_tests"] = args.tests
    overrides["enable_llm_review"] = not args.no_llm
    overrides["enable_verify"] = not args.no_verify
    overrides["review_mode"] = args.review_mode  # 默认 None：未显式传入时由 from_sources 跳过
    if args.out is not None:
        overrides["out_dir"] = args.out
    if args.work_root is not None:
        overrides["work_root"] = args.work_root
    if args.lang:
        overrides["languages"] = list(args.lang)
    if args.fix_max is not None:
        overrides["fix_max_patches"] = args.fix_max
    if args.testgen_max is not None:
        overrides["testgen_max_functions"] = args.testgen_max
    # W14-A2（M-2a）：沙箱后端（CLI 显式参数层，高于配置文件/环境变量层）
    if args.sandbox_backend is not None:
        overrides["sandbox_backend"] = args.sandbox_backend
    # 契约 v1.4：门禁阈值 / 基线 / 增量（字段行为由流水线与门禁实现，这里只透传）
    if args.fail_on is not None:
        overrides["fail_on_severity"] = args.fail_on
    if args.baseline is not None:
        overrides["baseline_path"] = args.baseline
    if args.report_baseline is not None:
        overrides["report_baseline_out"] = args.report_baseline
    if args.diff is not None:
        overrides["diff_ref"] = args.diff
    # W22-C：规则级配置（CLI 显式参数层；severity_overrides 只走配置文件）
    if args.disable_rule:
        overrides["disabled_rules"] = list(args.disable_rule)
    if args.ignore_path:
        overrides["ignore_paths"] = list(args.ignore_path)
    config_file, search_root = _resolve_config_discovery(args)
    return AuditConfig.from_sources(cli_overrides=overrides, config_file=config_file, search_root=search_root)


def _print_summary(report: AuditReport, paths: dict[str, str], sarif_path: Path | None = None) -> None:
    """终端打印审计摘要（sarif_path 非空时追加 SARIF 产物行）。"""
    print("\n========== 审计完成 ==========")
    print(f"项目：{report.project_name}　审计 ID：{report.audit_id}")
    print(f"健康分：{report.health_score} / 100")
    summary = report.summary
    print(
        "问题统计："
        f"critical {summary.get('critical', 0)} ｜ "
        f"high {summary.get('high', 0)} ｜ "
        f"medium {summary.get('medium', 0)} ｜ "
        f"low {summary.get('low', 0)}"
    )
    print(f"报告目录：{Path(paths['md']).resolve().parent}")
    for key, label in (("json", "JSON"), ("md", "Markdown"), ("html", "HTML")):
        print(f"  {label}：{Path(paths[key]).resolve()}")
    if sarif_path is not None:
        print(f"  SARIF：{sarif_path.resolve()}")
    print("==============================")


def _gate_threshold(args: argparse.Namespace, config: AuditConfig) -> str:
    """解析门禁阈值：--fail-on > 配置文件 fail_on_severity > 默认 high。

    默认 high 与 SARIF error 档（critical/high）对齐，等价于 semgrep --error 的语义。
    """
    if args.fail_on is not None:
        return args.fail_on
    if config.fail_on_severity in _GATE_ORDER:
        return config.fail_on_severity
    return "high"


def _enforce_gate(report: AuditReport, threshold: str) -> int:
    """CI 门禁（对标 semgrep --error）：summary 中 >= 阈值各级计数之和 > 0 → 退出码 3。

    门禁消息一律走 stderr（R3-12）：--json 组合下 stdout 只含报告 JSON，
    可被 CI 脚本整体 json.loads。
    """
    summary = report.summary or {}
    counts = {sev: int(summary.get(sev, 0)) for sev in _GATE_ORDER}
    cutoff = _GATE_ORDER.index(threshold)
    hit = sum(counts[sev] for sev in _GATE_ORDER[: cutoff + 1])
    total = sum(counts.values())
    suppressed = int(getattr(report.stats, "suppressed", 0) or 0)
    degraded = bool(getattr(report.stats, "degraded_ingest", False))
    detail = (
        f"阈值 {threshold}：critical {counts['critical']} ｜ high {counts['high']} ｜ "
        f"medium {counts['medium']} ｜ low {counts['low']}；"
        f"问题总数 {total}；基线抑制 {suppressed}；降级运行 {'是' if degraded else '否'}"
    )
    # W17（F1，FR-1.4）：ingest 降级运行（如超规模上限）时门禁视为未通过——
    # 审计未实际执行，CI 不得凭 exit 0 放行。普通模式（无 --check/--fail-on）
    # 保持退出码 0 + stderr 警告，不阻断交互式使用。
    if degraded:
        print(
            f"\n[门禁] 未通过：ingest 降级运行（审计未实际执行）——{detail}（退出码 3）",
            file=sys.stderr,
        )
        return 3
    if hit > 0:
        print(f"\n[门禁] 未通过：发现 {hit} 个 >= 阈值的问题——{detail}（退出码 3）", file=sys.stderr)
        return 3
    print(f"\n[门禁] 通过：未发现 >= 阈值的问题——{detail}", file=sys.stderr)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """run 子命令：执行完整七阶段审计；可选 SARIF 产物与 CI 门禁。

    W24-B：事件流经 _ProgressEvents 收集（append/extend 时实时打印 stderr 进度行），
    --quiet 时退回普通 list 关闭进度；stdout 只承载报告/摘要，纯净契约不回退。
    """
    if not Path(args.source_path).exists():
        raise FileNotFoundError(f"源路径不存在：{args.source_path}")
    config = _build_run_config(args)
    for warning in config.config_warnings:
        print(f"[配置警告] {warning}", file=sys.stderr)
    # R4-10：收集事件流，ingest 失败（降级运行）时在 stderr 打一行降级警告。
    # W24-B：默认（非 --quiet）用 _ProgressEvents 实时把事件打印为 stderr 进度。
    events: list[dict[str, object]] = [] if args.quiet else _ProgressEvents()
    report = asyncio.run(run_audit_simple(config, events=events))
    # W21：refactor 事件的 degraded 是整数计数（LLM 降级次数），只有编排层的
    # degraded=True 才代表 ingest 降级——恒等比较避免计数真值误报。
    if any(event.get("degraded") is True for event in events):
        print(
            "[警告] ingest 未成功：本次审计为降级运行（无可用工作副本，"
            "index/detect/understand 已跳过，报告不含代码统计）",
            file=sys.stderr,
        )
    sarif_path: Path | None = None
    if args.format == "sarif":  # json/md/html 由流水线照旧落盘，这里只追加 SARIF
        from audit.report.sarif import write_sarif

        sarif_path = write_sarif(report, config.resolve_out_dir() / "report.sarif")
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_summary(report, _report_paths(config), sarif_path=sarif_path)
    if args.check or args.fail_on is not None:
        return _enforce_gate(report, _gate_threshold(args, config))
    return 0


def _report_paths(config: AuditConfig) -> dict[str, str]:
    """报告落盘路径（与 audit.report.render.write_report 的命名一致）。"""
    out_dir = config.resolve_out_dir()
    return {"json": str(out_dir / "report.json"), "md": str(out_dir / "report.md"), "html": str(out_dir / "report.html")}


def cmd_index(args: argparse.Namespace) -> int:
    """index 子命令：只跑 ingest + index，打印统计。"""
    from pathlib import Path as _Path

    from audit.indexer import create_index
    from audit.ingest import ingest

    work_root = _Path(args.work_root) if args.work_root else _Path(".codeaudit")
    workspace = ingest(args.source_path, work_root)
    store = create_index(workspace)
    try:
        store.build()
        stats = store.stats()
    finally:
        store.close()  # R1-4：索引连接用完即释放
    print("\n========== 索引构建完成 ==========")
    print(f"工作副本：{workspace.src_root}")
    for key, value in stats.items():
        print(f"{key}: {value}")
    print("==================================")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """report 子命令：读取已落盘的 report.json，转换为 Markdown / HTML 输出。"""
    from audit.models import AuditReport
    from audit.report.render import render_html, render_markdown

    report_path = Path(args.report_json)
    if not report_path.exists():
        raise FileNotFoundError(f"报告文件不存在：{args.report_json}")
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"报告文件不是合法 JSON：{report_path}（{exc}）") from exc
    report = AuditReport.from_dict(data)

    fmt = args.format.lower()
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            render_html(report) if fmt == "html" else render_markdown(report),
            encoding="utf-8",
        )
        print(f"报告已写入：{out_path.resolve()}（格式：{fmt}）")
    else:
        print(render_html(report) if fmt == "html" else render_markdown(report))
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    """apply 子命令（P0-2）：把审计补丁应用回源码；默认 dry-run 预览不落盘。

    - 补丁序号 N 为 0 起，与 GET /api/audits/{id}/patches 的顺序一致；
    - 默认 dry-run：列出将写入的目标文件与 patch 概要，不落盘；
    - --yes 才真正写入；任何拒绝（指纹漂移 / 无指纹老补丁 / 路径非法）→ 整体不落盘，
      退出码 1；
    - --all-verified 仅当选中补丁全部为 verified 才允许；单 --patch N 不限状态，
      但会输出当前状态提示。
    """
    from audit.fix.applyer import apply_patches, locate_audit
    from audit.fix.patcher import _diff_targets

    record = locate_audit(args.audit_id, args.work_root)
    report = record.report
    if not report.patches:
        print(f"[提示] 审计 {args.audit_id} 没有可应用的补丁（未启用 --fix 或未生成）。")
        return 0
    index = args.patch
    if index is not None and (index < 0 or index >= len(report.patches)):
        print(
            f"[错误] 补丁序号越界：--patch {index}（有效范围 0-{len(report.patches) - 1}，"
            f"共 {len(report.patches)} 个补丁）。",
            file=sys.stderr,
        )
        return 1

    # 目标源码目录：--workdir 显式优先；否则用任务记录的 source_path（须为目录）
    if args.workdir:
        workdir = Path(args.workdir)
    elif record.source_path and Path(record.source_path).is_dir():
        workdir = Path(record.source_path)
    else:
        workdir = Path.cwd()
    if not workdir.is_dir():
        print(
            f"[错误] 目标源码目录不存在或不是目录：{workdir}"
            "（zip 输入的审计请用 --workdir 指定解包后的源码目录）。",
            file=sys.stderr,
        )
        return 1

    result = apply_patches(
        report.patches,
        workdir,
        yes=args.yes,
        only_index=index,
        require_all_verified=args.all_verified,
    )

    patches = report.patches
    selected = [
        (i, p) for i, p in enumerate(patches) if index is None or i == index
    ]
    mode = "dry-run 预览（未落盘）" if not args.yes else "写入源码"
    print(f"\n========== 补丁应用{mode} ==========")
    print(f"审计：{args.audit_id}（报告来源：{record.location}）")
    print(f"目标源码目录：{workdir.resolve()}")
    rejected_by_index = {entry["patch_index"]: entry["reason"] for entry in result.rejected}
    for i, patch in selected:
        targets = _diff_targets(patch.diff) or ["-"]
        line = (
            f"[{i}] {patch.id}（{patch.apply_status}）→ {', '.join(targets)}\n"
            f"    rationale：{patch.rationale[:80] or '（无）'}"
        )
        print(line)
        if patch.apply_status != "verified" and index == i:
            print(
                f"    [提示] 当前状态为 {patch.apply_status}（非 verified）："
                "单补丁应用不限状态，应用后建议人工复核或补跑测试。"
            )
        reason = rejected_by_index.get(i)
        if reason:
            print(f"    [拒绝] {reason}")
        elif args.yes:
            print("    [应用成功]")
        else:
            print("    [预检通过] 加 --yes 将写入上述文件")
    print("==================================")

    # P0-8：建议 commit message / PR 描述（纯字符串模板拼接，零 LLM token）。
    # 选中补丁集（--patch N / --all-verified 的结果集）确定后输出；只增打印，
    # 不改变 apply 既有行为、参数与退出码语义。dry-run 场景标题带（预览）标注。
    chosen = [patch for _, patch in selected]
    if args.commit_message or args.pr_description:
        from audit.fix.commitmsg import build_commit_message, build_pr_description

        preview_tag = "（预览）" if not args.yes else ""
        if args.commit_message:
            print(f"\n========== 建议 commit message{preview_tag} ==========")
            print(build_commit_message(chosen, report.issues))
        if args.pr_description:
            print(f"\n========== 建议 PR 描述{preview_tag} ==========")
            print(build_pr_description(chosen, report.issues))

    if not args.yes:
        print("本次为 dry-run：未写入任何文件。确认无误后加 --yes 应用。")
        return 0
    if result.rejected:
        print(
            f"\n[结果] 存在 {len(result.rejected)} 个被拒绝的补丁，"
            "整体未落盘（all-or-nothing）。",
            file=sys.stderr,
        )
        return 1
    print(f"\n[结果] 已应用 {len(result.applied)} 个补丁，写入 {len(result.written_files)} 个文件。")
    return 0


# ---------------------------------------------------------------- W28-A：safe-rename 接线

def cmd_rename(args: argparse.Namespace) -> int:
    """rename 子命令（W28-A）：safe-rename 符号重命名，默认 dry-run 只预览不落盘。

    - 调 audit.refactor.plan_rename 生成计划（tree-sitter identifier 节点区间
      精确替换，字符串/注释/子串天然不越界），本命令只做消费接线；
    - 默认 dry-run：打印涉及文件/替换点数/逐文件 unified diff，退出码 0，
      摘要标注「预览模式（未落盘，加 --yes 应用）」；
    - --yes 才 apply_rename(dry_run=False) 落盘，成功后打印中文摘要
      （写入文件数/替换点数）；apply 写入失败（res.ok=False，含 all-or-nothing
      复检拒绝）→ 中文报错退出 1 并列出逐文件原因；
    - 计划阶段 errors 非空（非法名/多定义点/解析失败/找不到定义等）→ 中文报错
      退出 1，绝不落盘；
    - --diff-only：与默认 dry-run 同效的语义别名（显式表达"只要 diff"，方便
      脚本化），与 --yes 互斥。
    """
    from audit.refactor.rename import apply_rename, plan_rename

    if args.diff_only and args.yes:
        print(
            "[错误] --diff-only 与 --yes 互斥：--diff-only 只输出 diff，不落盘。",
            file=sys.stderr,
        )
        return 1
    source = Path(args.source)
    if not source.exists():
        print(f"[错误] 源路径不存在：{args.source}（可为单文件或目录）。", file=sys.stderr)
        return 1

    plan = plan_rename(source, args.old_name, args.new_name, language=args.lang)
    if not plan.ok:
        print(
            f"[错误] 重命名计划生成失败（{args.old_name} → {args.new_name}），未落盘：",
            file=sys.stderr,
        )
        for err in plan.errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print("========== safe-rename 符号重命名 ==========")
    print(f"符号：{plan.old_name} → {plan.new_name}（语言：{plan.language}）")
    print(f"扫描范围：{Path(plan.source_root).resolve()}")
    defs = "；".join(f"{d.file}:{d.qualified_name}({d.kind}) @ 行 {d.line}" for d in plan.definitions)
    print(f"定义点：{defs}")
    print(f"涉及文件 {len(plan.patches)} 个，替换点 {plan.total_replace_points} 个：")
    for patch in plan.patches:
        print(f"  - {patch.file}（{len(patch.replace_points)} 个替换点）")

    if not args.yes:
        mode = "预览模式（未落盘，加 --yes 应用）"
        print(f"模式：{mode}")
        for patch in plan.patches:
            print(f"\n---------- diff：{patch.file} ----------")
            print(patch.unified_diff, end="")
        print("\n本次为预览模式（未落盘，加 --yes 应用）。")
        print("==================================")
        return 0

    result = apply_rename(plan, dry_run=False)
    if not result.ok:
        print("[错误] 重命名应用失败，全部未落盘（all-or-nothing）：", file=sys.stderr)
        for err in result.errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    print("模式：已应用（写入源码）")
    for rel in result.written_files:
        print(f"  已写入：{rel}")
    print(
        f"\n[结果] 重命名完成：写入 {len(result.written_files)} 个文件，"
        f"共 {plan.total_replace_points} 个替换点（{plan.old_name} → {plan.new_name}）。"
    )
    print("==================================")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    """diff 子命令（W24-C）：两次审计报告对比，输出 fixed / new / persisted 三栏。

    - 两个参数均可为 audit_id（经 audits.db / 报告目录定位）或 report.json 路径；
    - W28-A：--from-seq / --to-seq（可选）按历史版本号取 audit_id 侧的报告
      （TaskStore.get_report(audit_id, seq=N)）；缺省不传 = 取当前报告，行为
      零变化；seq 只对 audit_id 目标有效，report.json 文件路径给了就中文报错
      退出 1；seq 超界 / 任务无该版本 → 中文报错退出 1；
    - 匹配口径见 audit/report/comparator.py（(file, rule, 行号±3)）；本命令恒退出
      0（对比展示而非 CI 门禁，new>0 的门禁口径留待后续轮次定夺）。
    """
    from audit.config import AuditConfig
    from audit.fix.applyer import locate_audit
    from audit.models import AuditReport
    from audit.report.comparator import compare, format_result, load
    from audit.taskstore import TaskStore

    from_seq = getattr(args, "from_seq", None)
    to_seq = getattr(args, "to_seq", None)

    # W28-A：seq 只对 audit_id 目标有意义——report.json 文件路径无版本概念，先做适用性预检
    for label, token, seq in (
        ("基线（A）", args.audit_a, from_seq),
        ("当前（B）", args.audit_b, to_seq),
    ):
        if seq is not None and Path(token).is_file():
            print(
                f"[错误] {label} {token} 是 report.json 文件路径，不支持 seq 版本选择"
                "（--from-seq/--to-seq 仅适用于 audit_id）。",
                file=sys.stderr,
            )
            return 1

    def _history_version(audit_id: str, seq: int) -> tuple[AuditReport, str]:
        """按 seq 读历史版本（W28-A）。任务库定位口径与 cmd_resume / locate_audit
        一致：CODEAUDIT_DB_PATH 优先，否则 <work-root>/audits.db。"""
        work_root = Path(args.work_root) if args.work_root else Path(AuditConfig.from_env().work_root)
        db_path = Path(os.environ.get("CODEAUDIT_DB_PATH") or (work_root / "audits.db"))
        if not db_path.is_file():
            raise LookupError(
                f"任务库不存在：{db_path}，无法按 seq 读取 {audit_id} 的历史版本"
                "（--from-seq/--to-seq 仅支持经任务库持久化的 audit_id）"
            )
        store = TaskStore(db_path, work_root=work_root)
        try:
            data = store.get_report(audit_id, seq=seq)
        finally:
            store.close()
        if data is None:
            raise LookupError(
                f"未找到审计 {audit_id} 的历史版本 seq={seq}"
                f"（任务不存在 / 未落报告 / seq 越界；任务库：{db_path}）"
            )
        return AuditReport.from_dict(data), f"{db_path}#seq={seq}"

    def _report_of(token: str, seq: int | None):
        path = Path(token)
        if path.is_file():
            return load(path), str(path)
        if seq is not None:
            return _history_version(token, seq)
        record = locate_audit(token, args.work_root)
        return record.report, record.location

    try:
        report_a, loc_a = _report_of(args.audit_a, from_seq)
        report_b, loc_b = _report_of(args.audit_b, to_seq)
    except LookupError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1
    print("========== 审计报告对比 ==========")
    print(f"基线（A）：{args.audit_a}（{loc_a}）")
    print(f"当前（B）：{args.audit_b}（{loc_b}）")
    print(format_result(compare(report_a, report_b)))
    return 0


# ---------------------------------------------------------------- W24-E：resume 断点续跑

def _rebuild_config_from_task_json(config_json: str) -> AuditConfig:
    """从任务行 config_json 重建 AuditConfig（W24-E，续跑与原任务口径一致）。

    - 只取 AuditConfig 已知字段，未知字段忽略（跨版本前向兼容）；
    - api_key 落库前已脱敏为 "<redacted>"（server F6），打码值不可用——从已知
      字段中剔除，交由 AuditConfig.from_env() 按「进程环境 > .env > 默认值」
      口径补全（.env 自动加载）；
    - 空 / 非法 JSON / 非 dict：按空配置处理（默认值 + 环境变量层）——
      source_path 缺失时由 resume_stage_done 的同源校验兜底（路径不一致不放行
      跳过任何阶段，宁可全量重跑）。
    """
    try:
        raw = json.loads(config_json) if config_json.strip() else {}
    except json.JSONDecodeError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    known = {f.name for f in dataclasses.fields(AuditConfig)} - {"api_key", "config_warnings"}
    values = {key: val for key, val in raw.items() if key in known}
    return AuditConfig.from_env(**values)


def _resume_guard_action(status: str, progress: list[str] | tuple[str, ...]) -> str:
    """resume 可续跑判定纯函数（P0-9）：CLI cmd_resume 与 server resume 端点单源共用。

    入参为任务行 status 与 stage_done 进度列表（无 I/O，两处判定逻辑零复制）。
    返回动作码：
    - "resume"：interrupted，或 failed 且带阶段进度（stage_done 非空）——直接续跑；
    - "sweep"：running 且带阶段进度——疑似 SIGKILL 级硬杀滞留，调用方先按
      grace 窗口 sweep 自愈（sweep_interrupted 只清 updated_at 早于 now-grace
      的行，活跃任务持续被 touch 不会误伤），再重读任务定夺；重读后仍为
      "sweep"（窗口内有活动）即报「仍在运行或刚有活动」；
    - "reject"：其余状态（queued / done / 无进度的 failed / 无进度的 running）
      ——W24-E 既有「不可续跑」语义，报错文案与退出码逐字节保持。
    """
    if status == "interrupted":
        return "resume"
    if not progress:
        return "reject"
    if status == "failed":
        return "resume"
    if status == "running":
        return "sweep"
    return "reject"


def _resume_sweep_grace_seconds() -> int:
    """resume 自愈清扫窗口（秒）：env CODEAUDIT_SWEEP_GRACE_SEC，缺省 30。

    解析语义与 server.app._int_from_env 同口径（缺省/非法/负数均回落默认值）；
    与 server 启动 sweep 的缺省 0 不同——自愈是用户显式发起的动作，默认给
    30s 窗口防误伤真在跑的活跃任务。
    """
    raw = os.environ.get("CODEAUDIT_SWEEP_GRACE_SEC")
    if raw is None:
        return 30
    try:
        value = int(raw)
    except ValueError:
        return 30
    return value if value >= 0 else 30


def cmd_resume(args: argparse.Namespace) -> int:
    """resume 子命令（W24-E）：续跑被中断的审计任务（ingest/index/detect 可跳过）。

    - 任务行经 TaskStore 读取（CODEAUDIT_DB_PATH 优先，否则 <work-root>/audits.db，
      与 apply/diff 的定位口径一致）；
    - 可续跑态：interrupted，或 failed 且带阶段进度（stage_done 非空）——其余
      状态中文报错退出 1；任务不存在同样中文报错退出 1；
    - running 且带阶段进度（SIGKILL 级硬杀滞留态，P0-9/F5-R2）：先按 grace 窗口
      （env CODEAUDIT_SWEEP_GRACE_SEC，缺省 30s）调 sweep_interrupted 自愈——
      窗口外无 updated_at 活动才视为真死置 interrupted；重读任务后已变
      interrupted → 正常续跑；仍 running（窗口内有活动，疑似真有执行体在跑）
      → 中文报错退出 1；
    - 复位 interrupted/failed → running（TaskStore.mark_resuming），经
      resume_stage_done 判定可跳过阶段后 run_audit(resume_stages=done)；
      进度事件走 stderr，与 cmd_run 完全一致；
    - 成功：报告落库 + 状态置 done + 中文摘要（跳过阶段 / issue 总数 / 报告路径）；
    - 中途再断（Ctrl+C / 异常）：状态回到 interrupted——stage_done 机制保留进度，
      天然支持再次 resume；错误本身照常上抛交由顶层统一提示。
    """
    from audit.orchestrator.pipeline import resume_stage_done, run_audit
    from audit.taskstore import TaskStore

    work_root = Path(args.work_root) if args.work_root else Path(AuditConfig.from_env().work_root)
    db_path = Path(os.environ.get("CODEAUDIT_DB_PATH") or (work_root / "audits.db"))
    store = TaskStore(db_path, work_root=work_root)
    try:
        task = store.get(args.audit_id)
        if task is None:
            print(f"[错误] 任务不存在：{args.audit_id}（任务库：{db_path}）。", file=sys.stderr)
            return 1
        status = str(task.get("status") or "")
        progress = store.get_stage_done(args.audit_id)
        action = _resume_guard_action(status, progress)
        if action == "sweep":
            # SIGKILL 滞留自愈：先按窗口清扫再重读定夺（判定单源见 _resume_guard_action）
            store.sweep_interrupted(grace_seconds=_resume_sweep_grace_seconds())
            task = store.get(args.audit_id)
            status = str((task or {}).get("status") or "")
            action = _resume_guard_action(status, progress)
            if action == "sweep":
                print(
                    f"[错误] 任务 {args.audit_id} 仍在运行或刚有活动，请稍后重试。",
                    file=sys.stderr,
                )
                return 1
        if action == "reject":
            print(
                f"[错误] 任务 {args.audit_id} 状态为 {status}，不可续跑"
                "（仅 interrupted，或带阶段进度的 failed 支持 resume）。",
                file=sys.stderr,
            )
            return 1
        config = _rebuild_config_from_task_json(str(task.get("config_json") or ""))
        for warning in config.config_warnings:
            print(f"[配置警告] {warning}", file=sys.stderr)
        if not store.mark_resuming(args.audit_id):
            print(
                f"[错误] 任务 {args.audit_id} 状态复位失败（状态已变化，请查列表后重试）。",
                file=sys.stderr,
            )
            return 1
        done = resume_stage_done(store, args.audit_id, config)

        events: list[dict[str, object]] = _ProgressEvents()  # 进度实时打印 stderr（与 cmd_run 同款）

        async def _emit(event: dict[str, object]) -> None:
            events.append(event)

        try:
            report = asyncio.run(run_audit(config, _emit, audit_id=args.audit_id, resume_stages=done))
        except BaseException:
            # 中途再断：回到 interrupted（非终态，工作副本与阶段进度保留，可再续）
            store.set_status(args.audit_id, "interrupted")
            raise
        # server 收尾同形态：报告落库 + 终态 done
        store.set_report(args.audit_id, report)
        store.set_status(args.audit_id, "done")
        if any(event.get("degraded") is True for event in events):
            print(
                "[警告] ingest 未成功：本次续跑为降级运行（无可用工作副本，"
                "index/detect/understand 已跳过，报告不含代码统计）",
                file=sys.stderr,
            )
        paths = _report_paths(config)
        print("\n========== 续跑完成 ==========")
        print(f"任务：{args.audit_id}　状态：done")
        # F7-R1（W27 卡 A）防御性标注：LLM 通道状态显式化——此前 api_key 在重建
        # 链路丢失时 pipeline 静默走 FakeLLM（纯规则模式），用户无感知。
        llm_channel = "已启用" if config.api_key else "未启用（未检测到 API Key，本次为纯规则模式）"
        print(f"LLM 通道：{llm_channel}")
        print(f"跳过阶段：{'、'.join(sorted(done)) if done else '（无——全量重跑）'}")
        print(f"问题总数：{len(report.issues)}")
        print(f"报告目录：{Path(paths['md']).resolve().parent}")
        for key, label in (("json", "JSON"), ("md", "Markdown"), ("html", "HTML")):
            print(f"  {label}：{Path(paths[key]).resolve()}")
        print("==============================")
        return 0
    finally:
        store.close()


# ---------------------------------------------------------------- W24-B：doctor 环境自检

# doctor 检查项状态级：OK 通过 ｜ 警告 有损降级（不影响核心功能）｜ 失败 核心功能受损
_DOCTOR_OK, _DOCTOR_WARN, _DOCTOR_FAIL = "OK", "警告", "失败"

# doctor 检查行类型：(状态, 检查项名, 说明, 修复建议)；建议为空串表示无需修复。
_DoctorRow = tuple[str, str, str, str]

# doctor 语言包探测清单：前三个是 pyproject 声明的核心依赖（缺失记"失败"）；
# java（W24-A）、go（W26 卡 C）与 cpp（W29 集成收口，清偿 W28 已知边界）为
# 新增可选语言包，缺失只记"警告"（未装如实报未安装，不报错）。
_DOCTOR_LANGS: tuple[tuple[str, bool], ...] = (
    ("python", True),
    ("javascript", True),
    ("typescript", True),
    ("java", False),
    ("go", False),
    ("cpp", False),
)


def _import_module(name: str) -> object:
    """importlib.import_module 薄包装（doctor 语言包探测；测试注入替身用）。"""
    import importlib

    return importlib.import_module(name)


def _check_python() -> _DoctorRow:
    """Python 版本检查：低于 3.11 记失败（tomllib 等标准库依赖，pyproject 同口径）。"""
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if (sys.version_info.major, sys.version_info.minor) < (3, 11):
        return _DOCTOR_FAIL, "Python 版本", f"Python {version}（低于 3.11，缺少 tomllib）", "升级到 Python 3.11 及以上"
    return _DOCTOR_OK, "Python 版本", f"Python {version}", ""


def _check_git() -> _DoctorRow:
    """git 可用性检查：缺失记警告（--diff 增量与补丁验证降级，核心审计不受损）。"""
    path = shutil.which("git")
    if not path:
        return _DOCTOR_WARN, "git", "未找到 git 命令", "安装 git 并加入 PATH（--diff 增量审计与补丁 git apply 验证依赖 git）"
    return _DOCTOR_OK, "git", f"git 可用：{path}", ""


def _check_tree_sitter() -> list[_DoctorRow]:
    """tree-sitter 语言包逐个 import 探测：核心三语言缺失记失败，java/go 等可选语言包缺失记警告。"""
    rows: list[_DoctorRow] = []
    for lang, core in _DOCTOR_LANGS:
        module_name = f"tree_sitter_{lang}"
        try:
            _import_module(module_name)
        except Exception as exc:  # noqa: BLE001 —— 单语言包探测失败不中断 doctor
            status = _DOCTOR_FAIL if core else _DOCTOR_WARN
            reason = "导入失败" if core else "未安装（可选语言包）"
            rows.append(
                (
                    status,
                    f"语言包 {lang}",
                    f"{module_name} {reason}（{type(exc).__name__}）",
                    f"pip install {module_name}",
                )
            )
        else:
            rows.append((_DOCTOR_OK, f"语言包 {lang}", f"{module_name} 可用", ""))
    return rows


def _check_dotenv() -> _DoctorRow:
    """.env 白名单键合法性检查：白名单外键会被配置加载器静默忽略，这里显式提示。

    只读键名、绝不回显键值（.env 通常是密钥文件）。白名单口径与 audit.config
    的 _DOTENV_KEYS 单源对齐（cmd_apply 引 _diff_targets 私有名同款先例）。
    """
    from audit.config import _DOTENV_KEYS, _parse_dotenv_line  # 单源复用，见 docstring

    path = Path.cwd() / ".env"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return _DOCTOR_OK, ".env", "未找到 .env（跳过；需要 LLM 时可配置 GLM_API_KEY）", ""
    except UnicodeDecodeError:
        return _DOCTOR_FAIL, ".env", ".env 不是 UTF-8 文本，无法解析", "以 UTF-8（无 BOM 亦可）重新保存 .env"
    ignored: list[str] = []
    known = 0
    for line in text.splitlines():
        parsed = _parse_dotenv_line(line)
        if parsed is None:
            continue
        key, _value = parsed  # 值不落日志/终端：.env 值通常是密钥
        if key in _DOTENV_KEYS:
            known += 1
        else:
            ignored.append(key)
    if ignored:
        return (
            _DOCTOR_WARN,
            ".env",
            f"含 {len(ignored)} 个白名单外键（将被忽略）：{', '.join(sorted(ignored))}",
            f"白名单键：{', '.join(sorted(_DOTENV_KEYS))}；其余键请移出 .env",
        )
    return _DOCTOR_OK, ".env", f"可解析，含 {known} 个白名单键", ""


def _check_config_file() -> _DoctorRow:
    """配置文件可解析性检查：复用 AuditConfig.from_sources 的解析与未知键告警。"""
    path = Path.cwd() / ".codeaudit.toml"
    if not path.is_file():
        return _DOCTOR_OK, "配置文件", "未找到 .codeaudit.toml（使用默认配置；codeaudit init 可生成模板）", ""
    try:
        config = AuditConfig.from_sources(config_file=str(path))
    except Exception as exc:  # noqa: BLE001 —— 解析异常如实报告，不中断 doctor
        return _DOCTOR_FAIL, "配置文件", f".codeaudit.toml 解析异常：{type(exc).__name__}: {exc}", "修正 TOML 语法后重试"
    if config.config_warnings:
        return (
            _DOCTOR_WARN,
            "配置文件",
            "；".join(config.config_warnings),
            "改为配置契约内的键（见 audit/config.py AuditConfig 字段）",
        )
    return _DOCTOR_OK, "配置文件", ".codeaudit.toml 可解析且无未知键", ""


def _check_docker() -> _DoctorRow:
    """Docker 沙箱后端可用性检查：docker 不存在报"不可用"（警告），绝不崩溃。"""
    docker_path = shutil.which("docker")
    if not docker_path:
        return (
            _DOCTOR_WARN,
            "Docker 沙箱",
            "docker 不可用（未安装 docker CLI；沙箱自动走 subprocess 后端）",
            "需要容器沙箱时安装 Docker Desktop / Engine，再用 --sandbox-backend docker 显式启用",
        )
    try:
        from audit.sandbox.executor import docker_available  # 延迟导入：探测失败不影响 doctor

        available = bool(docker_available())
    except Exception as exc:  # noqa: BLE001 —— 探测异常按不可用处理
        return _DOCTOR_WARN, "Docker 沙箱", f"docker 探测异常（按不可用处理）：{type(exc).__name__}: {exc}", "确认 Docker 已启动后重试"
    if available:
        return _DOCTOR_OK, "Docker 沙箱", f"docker 可用：{docker_path}", ""
    return _DOCTOR_WARN, "Docker 沙箱", "docker CLI 存在，但守护进程不可用（docker info 失败）", "启动 Docker Desktop / Engine 后重试"


def cmd_doctor(args: argparse.Namespace) -> int:
    """doctor 子命令（W24-B）：零 API Key 的环境自检，输出中文诊断表。

    检查项：Python 版本 / git / tree-sitter 语言包（含 java，逐个 import 探测）/
    .env 白名单键 / .codeaudit.toml 可解析性 / Docker 沙箱后端。任何单项失败不
    中断整体；存在"失败"级条目时退出码 1，仅"警告"（如 java 未装）仍为 0。
    """
    checks: list[_DoctorRow] = [_check_python(), _check_git()]
    checks.extend(_check_tree_sitter())
    checks.extend([_check_dotenv(), _check_config_file(), _check_docker()])
    print("========== CodeAudit 环境诊断 ==========")
    for status, name, detail, suggestion in checks:
        line = f"[{status}] {name}：{detail}"
        if suggestion:
            line += f"　→ 建议：{suggestion}"
        print(line)
    fails = sum(1 for c in checks if c[0] == _DOCTOR_FAIL)
    warns = sum(1 for c in checks if c[0] == _DOCTOR_WARN)
    print("-----------------------------------------")
    print(f"结论：{len(checks) - fails - warns} 项通过，{warns} 项警告，{fails} 项失败")
    if fails:
        print("存在失败项：请按上方建议修复后重跑 doctor（退出码 1）。")
        return 1
    print("核心环境就绪，可直接运行 codeaudit run。")
    return 0


# ---------------------------------------------------------------- W24-B：init 配置模板

# .codeaudit.toml 注释模板骨架：三键全部注释掉——init 只铺骨架不改行为，
# 用户按需取消注释（键说明与 audit/config.py W22-C 契约一致）。
_INIT_TEMPLATE = """\
# CodeAudit 审计配置（.codeaudit.toml，顶层键）
# 说明：优先级为 默认值 < 本配置文件 < 环境变量 < CLI 显式参数；
#       全部键与默认值见 audit/config.py 的 AuditConfig 字段。
#       pyproject.toml 用户请写在 [tool.codeaudit] 节下（键名相同）。
# 以下三键为规则级配置（默认全部注释 = 完整默认行为）：

# 禁用指定静态规则（TOML 数组；规则 ID 见 docs-site/rules.md）：
# disabled_rules = ["PY-EQ-NONE", "PY-BARE-EXCEPT"]

# 覆盖规则严重级别（键 = 规则 id，值 = critical|high|medium|low；只走配置文件）：
# [severity_overrides]
# PY-MAGIC-NUMBER = "low"

# 路径白名单：跳过匹配文件不参与检测（fnmatch 或目录前缀；
# 只影响规则扫描与 LLM 审查，不影响索引构建与后处理扫描器）：
# ignore_paths = ["vendor/", "tests/fixtures/", "*.min.js"]
"""


def cmd_init(args: argparse.Namespace) -> int:
    """init 子命令（W24-B）：生成 .codeaudit.toml 注释模板骨架。

    目标已存在时拒绝覆盖（退出码 1），绝不改动既有配置内容。
    """
    target_dir = Path(args.dir).resolve() if args.dir else Path.cwd()
    target = target_dir / ".codeaudit.toml"
    if target.exists():
        print(
            f"[错误] 目标已存在，拒绝覆盖：{target}（如需重新生成请先手动删除该文件）。",
            file=sys.stderr,
        )
        return 1
    target.write_text(_INIT_TEMPLATE, encoding="utf-8")
    print(f"已生成配置模板：{target}")
    print("三键（disabled_rules / severity_overrides / ignore_paths）默认注释，按需取消注释后生效。")
    return 0


def _per_worker_concurrency() -> int:
    """读取每 worker 并发上限（env ``CODEAUDIT_MAX_RUNNING``，默认 4，最小 1）。

    与 ``server/app.py`` 的 ``_MAX_RUNNING_AUDITS`` 同口径——这里独立读 env，
    避免为打印横幅而耦合 server 模块私有名。
    """
    raw = os.environ.get("CODEAUDIT_MAX_RUNNING", "")
    try:
        return max(1, int(raw))
    except ValueError:
        return 4


def _is_loopback_host(host: str) -> bool:
    """判定监听地址是否回环（W22-D）：127.0.0.0/8、::1、localhost。

    - IP 字面量（含 IPv6 ``[::1]`` 方括号形态）走 ``ipaddress`` 判定；
    - 解析失败按主机名等值 ``localhost`` 判定（uvicorn 对 localhost 绑回环）；
    - 空值按回环处理（uvicorn 缺省 127.0.0.1）。
    ``0.0.0.0`` / ``::`` 等 unspecified 地址不是回环，返回 False。
    """
    text = str(host or "").strip().strip("[]")
    if not text:
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return text.lower() == "localhost"


def cmd_serve(args: argparse.Namespace) -> int:
    """serve 子命令：启动 FastAPI 服务（SSE 进度 + Web 演示页）。

    ``--workers N``（默认 1）：

    - N=1：保持现状 ``uvicorn.run(create_app(), ...)`` 实例启动——单 worker 是
      默认与推荐形态，行为零变化；
    - N>1：实验特性，以 import string 形式启动（``"server.app:app"``，模块底部
      已有模块级 ``app = create_app()``）——实例形式与 workers>1 不兼容；跨
      worker 可见性依赖任务持久化（W11 TaskStore）。全局并发上限 =
      workers × 每 worker 上限（CODEAUDIT_MAX_RUNNING，默认 4）；
    - N<1：参数错误，中文报错并返回退出码 2。
    """
    if args.workers < 1:
        print(
            f"[错误] --workers 必须 >= 1，收到：{args.workers}。"
            "单 worker（默认值 1）是推荐形态；多 worker 为实验特性。",
            file=sys.stderr,
        )
        return 2

    # W22-D 安全默认：非回环绑定 && 无 API Token && 未显式豁免 → 拒绝启动。
    # Token 判定与 server 侧 _get_security_config 同口径（CODEAUDIT_API_TOKEN
    # 真实进程环境直读）；挂点在 cli（lifespan/create_app 拿不到 host）。
    if not _is_loopback_host(args.host) and not args.allow_insecure:
        if not os.environ.get("CODEAUDIT_API_TOKEN", "").strip():
            print(
                f"[错误] 监听地址 {args.host} 不是回环地址，但未配置 API Token——"
                "服务将暴露给局域网/公网且 /api/* 无鉴权。请任选其一：\n"
                "  1) 设置 CODEAUDIT_API_TOKEN 环境变量后再启动；\n"
                "  2) 确认风险后加 --allow-insecure 显式豁免本检查；\n"
                "  3) 本机开发改用 --host 127.0.0.1（默认，无需 token）。",
                file=sys.stderr,
            )
            return 1

    import uvicorn

    from server.app import create_app

    print("========== CodeAudit Agent 服务 ==========")
    print(f"监听地址：http://{args.host}:{args.port}")
    print(f"worker 数：{args.workers}")
    if args.workers > 1:
        per_worker = _per_worker_concurrency()
        print(
            f"[实验特性] 多 worker 模式（workers={args.workers}）："
            "任务由接收请求的 worker 执行，其他 worker 经持久化存储只读可见"
            "（需 W11 任务持久化生效）。"
        )
        print(
            f"[实验特性] 全局并发上限 = workers × 每 worker 上限"
            f"（CODEAUDIT_MAX_RUNNING）= {args.workers} × {per_worker}"
            f" = {args.workers * per_worker}"
        )
    print("演示页： /　｜　创建任务： POST /api/audits　｜　按 Ctrl+C 停止")
    print("==========================================")
    if args.workers > 1:
        # workers>1 与实例形式不兼容：必须传 import string（模块级 app = create_app()）。
        uvicorn.run(
            "server.app:app",
            host=args.host,
            port=args.port,
            workers=args.workers,
            log_level="info",
        )
    else:
        uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
    return 0


def _prog_name() -> str:
    """按 sys.argv[0] 推导 help/usage 中的程序名（R3-7）。

    - 源码直跑 ``python cli.py`` → "cli.py"；
    - console script 安装态（``codeaudit ...``）→ "codeaudit"（Windows 的 .exe 去后缀）。
    """
    raw = Path(str(sys.argv[0]).replace("\\", "/")).name if sys.argv and sys.argv[0] else ""
    if raw.endswith(".exe"):
        raw = raw[: -len(".exe")]
    return raw or "cli.py"


def build_parser() -> argparse.ArgumentParser:
    import audit

    version_help = f"打印版本号并退出（codeaudit-agent {audit.__version__}）"
    parser = argparse.ArgumentParser(
        prog=_prog_name(), description="代码库级智能审计与重构 Agent（GLM-5.3 Flash）"
    )
    parser.add_argument("--version", action="store_true", help=version_help)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="执行完整审计并生成报告")
    p_run.add_argument("source_path", help="待审计项目路径或 zip")
    p_run.add_argument("--out", default=None, help="报告输出目录（默认 <work-root>/reports）")
    p_run.add_argument("--work-root", default=None, help="工作区根目录（默认 .codeaudit）")
    p_run.add_argument("--lang", nargs="*", default=[], help="限定语言（如 python javascript），空为自动")
    p_run.add_argument("--fix", action="store_true", help="生成修复补丁（Wave 2）")
    p_run.add_argument("--tests", action="store_true", help="生成单测（Wave 2）")
    p_run.add_argument("--no-llm", action="store_true", help="禁用 LLM，纯规则模式")
    p_run.add_argument(
        "--review-mode",
        choices=["simple", "tools"],
        default=None,
        help="LLM 审查模式：simple=单次 json 调用，tools=工具取证循环（默认 simple）",
    )
    p_run.add_argument(
        "--no-verify", action="store_true", help="禁用 Verify Agent 复核（默认启用）"
    )
    p_run.add_argument(
        "--fix-max", type=int, default=None, help="单次审计最多生成修复 Patch 数（默认 50）"
    )
    p_run.add_argument(
        "--testgen-max", type=int, default=None, help="单次审计最多生成单测的目标函数数（默认 30）"
    )
    p_run.add_argument(
        "--sandbox-backend",
        choices=["subprocess", "docker"],
        default=None,
        help="沙箱后端（默认 subprocess；docker 为 opt-in 实验特性，探测失败自动降级 subprocess）",
    )
    p_run.add_argument("--json", action="store_true", help="以 JSON 输出完整报告")
    p_run.add_argument(
        "--quiet",
        action="store_true",
        help="关闭 stderr 实时进度输出（[stage n/7] 行）；门禁结果与报告输出不受影响",
    )
    p_run.add_argument(
        "--format",
        choices=["json", "md", "html", "sarif"],
        default="json",
        help="产物格式：sarif 时在报告目录额外生成 report.sarif（GitHub Security 集成），json/md/html 照旧（默认 json）",
    )
    p_run.add_argument(
        "--check", action="store_true", help="启用 CI 门禁：存在 >= 阈值的问题时以退出码 3 结束（阈值见 --fail-on）"
    )
    p_run.add_argument(
        "--fail-on",
        choices=["critical", "high", "medium", "low"],
        default=None,
        help="门禁阈值（critical>high>medium>low）；指定即隐含启用门禁（默认 high）",
    )
    p_run.add_argument(
        "--baseline", default=None, help="已知问题基线文件；命中指纹的问题被抑制（配合 --diff 做 PR 增量）"
    )
    p_run.add_argument(
        "--report-baseline", default=None, help="审计结束后把本次剩余问题写入该基线文件"
    )
    p_run.add_argument(
        "--diff", default=None, help="git ref（如 HEAD~1 / origin/main...）：仅审计相对该 ref 变更的文件"
    )
    p_run.add_argument(
        "--config",
        default=None,
        help="显式配置文件路径；缺省自动发现 .codeaudit.toml / pyproject [tool.codeaudit]",
    )
    p_run.add_argument(
        "--disable-rule",
        action="append",
        default=None,
        metavar="RULE_ID",
        help="禁用指定静态规则（可多次，如 --disable-rule PY-EQ-NONE）；等价配置键 disabled_rules",
    )
    p_run.add_argument(
        "--ignore-path",
        action="append",
        default=None,
        metavar="PATTERN",
        help="路径白名单：跳过匹配文件不参与检测（fnmatch 或目录前缀，可多次，如 --ignore-path vendor/）；只影响规则扫描与 LLM 审查",
    )
    p_run.add_argument("--version", action="store_true", help=version_help)
    p_run.set_defaults(func=cmd_run)

    p_idx = sub.add_parser("index", help="只构建接入与索引（Stage1+2）")
    p_idx.add_argument("source_path", help="待审计项目路径或 zip")
    p_idx.add_argument("--work-root", default=".codeaudit", help="工作区根目录")
    p_idx.set_defaults(func=cmd_index)

    p_report = sub.add_parser("report", help="把已生成的 report.json 转换为 Markdown / HTML")
    p_report.add_argument("report_json", help="report.json 文件路径")
    p_report.add_argument(
        "--format", choices=["md", "html"], default="md", help="输出格式（默认 md）"
    )
    p_report.add_argument("--out", default="", help="输出文件路径（缺省打印到标准输出）")
    p_report.set_defaults(func=cmd_report)

    p_apply = sub.add_parser("apply", help="把审计补丁应用回源码（默认 dry-run 预览，--yes 才写入）")
    p_apply.add_argument("audit_id", help="审计 ID（report.audit_id / 任务表 audit_id）")
    p_apply.add_argument(
        "--patch",
        type=int,
        default=None,
        metavar="N",
        help="只应用第 N 个补丁（0 起，与 GET /patches 顺序一致；不限状态，会输出状态提示）",
    )
    p_apply.add_argument(
        "--all-verified",
        action="store_true",
        help="闸门：仅当选中的补丁全部为 verified 状态才允许应用",
    )
    p_apply.add_argument(
        "--yes", action="store_true", help="真正写入源码（缺省 dry-run 只预览不落盘）"
    )
    p_apply.add_argument(
        "--workdir",
        default=None,
        help="目标源码目录（默认用任务记录的 source_path，否则当前目录）",
    )
    p_apply.add_argument(
        "--work-root",
        default=None,
        help="审计工作区根（定位 audits.db / 报告，默认 .codeaudit）",
    )
    p_apply.add_argument(
        "--commit-message",
        action="store_true",
        help="输出选中补丁集的建议 commit message（模板拼接生成；dry-run 下带（预览）标注）",
    )
    p_apply.add_argument(
        "--pr-description",
        action="store_true",
        help="输出选中补丁集的 Markdown PR 描述（含命中规则计数、验证状态表与兼容性说明）",
    )
    p_apply.set_defaults(func=cmd_apply)

    p_rename = sub.add_parser(
        "rename",
        help="safe-rename 符号重命名（默认 dry-run 预览 diff，--yes 才落盘）",
    )
    p_rename.add_argument(
        "source", help="扫描范围（单 .py 文件或目录；目录递归扫 *.py，跳过忽略/隐藏目录）"
    )
    p_rename.add_argument("old_name", help="旧符号名（须为合法 python 标识符）")
    p_rename.add_argument("new_name", help="新符号名（须为合法 python 标识符）")
    p_rename.add_argument(
        "--yes", action="store_true", help="真正写入源码（缺省 dry-run 只预览 diff 不落盘）"
    )
    p_rename.add_argument(
        "--diff-only",
        action="store_true",
        help="显式只要 diff（与默认 dry-run 同效的语义别名，便于脚本化；与 --yes 互斥）",
    )
    p_rename.add_argument(
        "--lang",
        default="python",
        help="语言（safe-rename MVP 仅支持 python，默认 python）",
    )
    p_rename.set_defaults(func=cmd_rename)

    p_diff = sub.add_parser(
        "diff", help="对比两次审计报告：fixed（已修复）/ new（新增）/ persisted（仍存在）三栏"
    )
    p_diff.add_argument("audit_a", help="基线审计：audit_id 或 report.json 路径")
    p_diff.add_argument("audit_b", help="当前审计：audit_id 或 report.json 路径")
    p_diff.add_argument(
        "--from-seq",
        type=int,
        default=None,
        metavar="N",
        help="基线 A 的历史版本号（audit_id 专用；缺省取当前报告，行为零变化）",
    )
    p_diff.add_argument(
        "--to-seq",
        type=int,
        default=None,
        metavar="N",
        help="当前 B 的历史版本号（audit_id 专用；缺省取当前报告，行为零变化）",
    )
    p_diff.add_argument(
        "--work-root",
        default=None,
        help="审计工作区根（按 audit_id 定位报告用，默认 .codeaudit）",
    )
    p_diff.set_defaults(func=cmd_diff)

    p_resume = sub.add_parser(
        "resume",
        help="续跑被中断的审计任务（复用已完成阶段的盘上产物，ingest/index/detect 可跳过）",
    )
    p_resume.add_argument("audit_id", help="要续跑的审计任务 ID")
    p_resume.add_argument(
        "--work-root",
        default=None,
        help="审计工作区根（定位 audits.db / 工作副本 / 报告目录，默认 .codeaudit）",
    )
    p_resume.set_defaults(func=cmd_resume)

    p_doctor = sub.add_parser(
        "doctor", help="环境自检（零 API Key）：Python/git/语言包/.env/配置文件/Docker"
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_init = sub.add_parser(
        "init", help="生成 .codeaudit.toml 注释模板骨架（目标已存在时拒绝覆盖）"
    )
    p_init.add_argument(
        "dir", nargs="?", default="", help="目标目录（缺省当前目录；文件名固定 .codeaudit.toml）"
    )
    p_init.set_defaults(func=cmd_init)

    p_serve = sub.add_parser("serve", help="启动 Web 服务（REST API + SSE 进度 + 演示页）")
    p_serve.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    p_serve.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    p_serve.add_argument(
        "--allow-insecure",
        action="store_true",
        help="（W22-D）显式豁免非回环绑定的 token 强制检查（自担风险）",
    )
    p_serve.add_argument(
        "--workers",
        type=int,
        default=1,
        help="worker 进程数（默认 1；>1 为实验特性，跨 worker 可见性依赖任务持久化）",
    )
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    """顶层入口：统一捕获异常，中文提示并返回退出码 1。

    ``--version``（顶层与 run 子命令）在这里提前截获：打印版本号后返回 0，
    不进入子命令调度（console script ``codeaudit`` 依赖 main 返回值作为退出码）。
    """
    tokens = list(sys.argv[1:]) if argv is None else list(argv)
    if "--version" in tokens:
        return _print_version()
    parser = build_parser()
    args = parser.parse_args(tokens)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n[中断] 用户取消操作。", file=sys.stderr)
        return 1
    except ImportError as exc:
        print(f"\n[错误] 功能模块尚未集成（{exc.name or exc}），请等待对应任务合入。", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 —— CLI 顶层兜底
        print(f"\n[错误] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
