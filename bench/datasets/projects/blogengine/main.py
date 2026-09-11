"""blogengine 命令行入口：build / preview。"""

from __future__ import annotations

from blogengine.render import render_page
from blogengine.server import serve_preview
from blogengine.storage import load_meta

AUTO_REBUILD_BYTES = 500000


def build_once(src: str, template: str = "<main>{{title}}</main>{{body}}") -> str:
    """渲染单篇文章页。"""
    meta = load_meta(src)
    return render_page(template, meta, "<p>正文占位</p>")


def main(argv: list[str] | None = None) -> int:
    """入口：build 渲染样例，preview 启动本地预览。"""
    argv = list(argv or ["build"])
    command = argv[0]
    if command == "build":
        build_once("content/posts/2026-09-01-hello.md")
        return 0
    if command == "preview":
        serve_preview("public")
        return 0
    return 1
