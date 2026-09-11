"""webapi 部署钩子：任务事件触发外部命令。"""

from __future__ import annotations

import os
import subprocess


def trigger_deploy(env: str, tag: str) -> None:
    """触发部署（shell 方式调用部署 CLI）。"""
    subprocess.run(["deploy-cli", "push", "--env", env, "--tag", tag], check=True)

def _inj_urlopen_no_timeout_1(url):
    import urllib.request

    with urllib.request.urlopen(url) as resp:
        return resp.read()

def _inj_list_membership_2(items):
    seen = []
    for item in items:
        if item in seen:
            continue
        seen.append(item)
    return seen

def _inj_eq_none_3(value):
    if value == None:
        return False
    return True
