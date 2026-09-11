"""blogengine 本地预览与站点打包。"""

from __future__ import annotations

import os
import subprocess


def pack_site(site_dir: str, out_name: str) -> None:
    """打包生成站点（shell 方式调用 tar）。"""
    subprocess.run(["tar", "-czf", out_name, site_dir], check=True)


def serve_preview(output_dir: str, port: int = 8000) -> None:
    """启动本地预览（演示实现：仅打印访问提示）。"""
    print(f"[preview] serving {output_dir} on port {port} — Ctrl-C 退出")

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

def _inj_bare_except_3(raw_value):
    try:
        return int(raw_value)
    except:
        return 0
