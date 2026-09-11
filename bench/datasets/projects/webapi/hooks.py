"""webapi 部署钩子：任务事件触发外部命令。"""

from __future__ import annotations

import os
import subprocess


def trigger_deploy(env: str, tag: str) -> None:
    """触发部署（shell 方式调用部署 CLI）。"""
    subprocess.run(["deploy-cli", "push", "--env", env, "--tag", tag], check=True)
