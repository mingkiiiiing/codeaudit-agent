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

def _inj_urlopen_no_timeout_1(url):
    import urllib.request

    with urllib.request.urlopen(url) as resp:
        return resp.read()

def _inj_str_concat_loop_2(rows):
    html = ""
    for row in rows:
        html = html + "<li>" + str(row) + "</li>"
    return html

def _inj_hardcoded_secret_3():
    access_token = "sk-live-9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"
    return access_token
