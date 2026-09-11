"""CodeAudit Agent 命令行入口。

用法：
  python cli.py run <source_path> [--out DIR] [--work-root DIR] [--lang python ...]
                   [--fix] [--tests] [--no-llm] [--json]
                   [--review-mode simple|tools] [--no-verify]
                   [--fix-max N] [--testgen-max N]
  python cli.py index <source_path> [--work-root DIR]
  python cli.py report <report_json> [--format md|html] [--out PATH]
  python cli.py serve [--host 127.0.0.1] [--port 8000]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from audit.config import AuditConfig
from audit.orchestrator.pipeline import run_audit_simple  # 模块级导入，便于测试注入替身


def _build_run_config(args: argparse.Namespace) -> AuditConfig:
    """根据 CLI 参数构造 AuditConfig（环境变量由 from_env 处理）。"""
    overrides: dict[str, object] = {
        "do_fix": args.fix,
        "do_tests": args.tests,
        "enable_llm_review": not args.no_llm,
        "enable_verify": not args.no_verify,
        "review_mode": args.review_mode,
    }
    if args.out:
        overrides["out_dir"] = args.out
    if args.work_root:
        overrides["work_root"] = args.work_root
    if args.lang:
        overrides["languages"] = list(args.lang)
    if args.fix_max is not None:
        overrides["fix_max_patches"] = args.fix_max
    if args.testgen_max is not None:
        overrides["testgen_max_functions"] = args.testgen_max
    return AuditConfig.from_env(source_path=args.source_path, **overrides)


def _print_summary(report, paths: dict[str, str]) -> None:
    """终端打印审计摘要。"""
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
    print("==============================")


def cmd_run(args: argparse.Namespace) -> int:
    """run 子命令：执行完整七阶段审计。"""
    if not Path(args.source_path).exists():
        raise FileNotFoundError(f"源路径不存在：{args.source_path}")
    config = _build_run_config(args)
    report = asyncio.run(run_audit_simple(config))
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_summary(report, _report_paths(config))
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
    store.build()
    stats = store.stats()
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py", description="代码库级智能审计与重构 Agent（GLM-5.3 Flash）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="执行完整审计并生成报告")
    p_run.add_argument("source_path", help="待审计项目路径或 zip")
    p_run.add_argument("--out", default="", help="报告输出目录（默认 <work-root>/reports）")
    p_run.add_argument("--work-root", default=".codeaudit", help="工作区根目录")
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
    """顶层入口：统一捕获异常，中文提示并返回退出码 1。"""
    parser = build_parser()
    args = parser.parse_args(argv)
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
