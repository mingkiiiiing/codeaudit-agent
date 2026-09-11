"""CodeAudit Agent 命令行入口。

用法：
  python cli.py run <source_path> [--out DIR] [--work-root DIR] [--lang python ...]
                   [--fix] [--tests] [--no-llm] [--json]
                   [--review-mode simple|tools] [--no-verify]
                   [--fix-max N] [--testgen-max N]
                   [--format json|md|html|sarif] [--check] [--fail-on critical|high|medium|low]
                   [--baseline PATH] [--report-baseline PATH] [--diff REF] [--config PATH]
                   [--version]
  python cli.py index <source_path> [--work-root DIR]
  python cli.py report <report_json> [--format md|html] [--out PATH]
  python cli.py serve [--host 127.0.0.1] [--port 8000]

退出码约定（docs/09 §3）：0 完成/门禁通过 ｜ 1 运行错误 ｜ 2 bench 保留 ｜ 3 --check 门禁失败。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from audit.config import AuditConfig
from audit.orchestrator.pipeline import run_audit_simple  # 模块级导入，便于测试注入替身
from audit.models import AuditReport

# 门禁严重级从高到低（阈值语义：命中 >= 阈值级别的问题即失败）
_GATE_ORDER: tuple[str, ...] = ("critical", "high", "medium", "low")


def _print_version() -> int:
    """打印 "codeaudit-agent <版本>"（版本单源：audit.__version__），返回退出码 0。"""
    import audit

    print(f"codeaudit-agent {audit.__version__}")
    return 0


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
    # 契约 v1.4：门禁阈值 / 基线 / 增量（字段行为由流水线与门禁实现，这里只透传）
    if args.fail_on is not None:
        overrides["fail_on_severity"] = args.fail_on
    if args.baseline is not None:
        overrides["baseline_path"] = args.baseline
    if args.report_baseline is not None:
        overrides["report_baseline_out"] = args.report_baseline
    if args.diff is not None:
        overrides["diff_ref"] = args.diff
    return AuditConfig.from_sources(cli_overrides=overrides, config_file=args.config or "")


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
    detail = (
        f"阈值 {threshold}：critical {counts['critical']} ｜ high {counts['high']} ｜ "
        f"medium {counts['medium']} ｜ low {counts['low']}；"
        f"问题总数 {total}；基线抑制 {suppressed}"
    )
    if hit > 0:
        print(f"\n[门禁] 未通过：发现 {hit} 个 >= 阈值的问题——{detail}（退出码 3）", file=sys.stderr)
        return 3
    print(f"\n[门禁] 通过：未发现 >= 阈值的问题——{detail}", file=sys.stderr)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """run 子命令：执行完整七阶段审计；可选 SARIF 产物与 CI 门禁。"""
    if not Path(args.source_path).exists():
        raise FileNotFoundError(f"源路径不存在：{args.source_path}")
    config = _build_run_config(args)
    for warning in config.config_warnings:
        print(f"[配置警告] {warning}", file=sys.stderr)
    report = asyncio.run(run_audit_simple(config))
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


def cmd_serve(args: argparse.Namespace) -> int:
    """serve 子命令：启动 FastAPI 服务（SSE 进度 + Web 演示页）。"""
    import uvicorn

    from server.app import create_app

    print("========== CodeAudit Agent 服务 ==========")
    print(f"监听地址：http://{args.host}:{args.port}")
    print("演示页： /　｜　创建任务： POST /api/audits　｜　按 Ctrl+C 停止")
    print("==========================================")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
    return 0


def _prog_name() -> str:
    """按 sys.argv[0] 推导 help/usage 中的程序名（R3-7）。

    - 源码直跑 ``python cli.py`` → "cli.py"；
    - console script 安装态（``codeaudit ...``）→ "codeaudit"（Windows 的 .exe 去后缀）。
    """
    raw = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
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
    p_run.add_argument("--json", action="store_true", help="以 JSON 输出完整报告")
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

    p_serve = sub.add_parser("serve", help="启动 Web 服务（REST API + SSE 进度 + 演示页）")
    p_serve.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    p_serve.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
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
