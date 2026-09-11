"""datatools 命令行入口。"""

from __future__ import annotations

from datatools.etl import run_pipeline


def main(argv: list[str] | None = None) -> int:
    """入口：source target 两个位置参数。"""
    argv = list(argv or ["data/input.csv", "out/facts.csv"])
    report = run_pipeline(argv[0], argv[1])
    return 0 if report.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
