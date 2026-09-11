"""CI 失败调试（临时）：把 pytest 输出尾部开成 GitHub Issue，便于匿名读取。

用法：python scripts/ci_debug_issue.py /tmp/pytest_dbg.txt
依赖环境变量：TOKEN（github.token）、REPO（owner/name）、RUNID（run id）。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request


def main() -> int:
    log_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/pytest_dbg.txt"
    try:
        body = open(log_path, encoding="utf-8", errors="replace").read()
    except OSError as exc:
        print("read log failed:", exc)
        return 1
    payload = json.dumps(
        {
            "title": f"CI debug: run {os.environ.get('RUNID', '?')} (temporary)",
            "body": "```text\n" + body + "\n```",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['REPO']}/issues",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {os.environ.get('TOKEN', '')}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        print("issue status:", resp.status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
