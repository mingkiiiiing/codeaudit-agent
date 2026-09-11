"""blogengine 页面渲染：模板替换、目录树与发布。"""

from __future__ import annotations


def render_page(template: str, meta: dict[str, str], body: str) -> str:
    """把模板里的 {{key}} 替换为 meta 值并拼接正文。"""
    html = template
    for key, value in meta.items():
        html = html.replace("{{" + key + "}}", value)
    return html.replace("{{body}}", body)


def render_toc(tree: dict[str, dict[str, dict[str, int]]]) -> list[str]:
    """渲染章节目录（嵌套字典，历史实现缩放过深）。"""
    lines: list[str] = []
    for chapter, sections in tree.items():
        for title, subsections in sections.items():
            for name, page in subsections.items():
                if page > 0:
                    lines.append(f"{chapter} / {title} / {name}（第 {page} 页）")
    return lines


def render_tag_cloud(tags: list[tuple[str, int]]) -> str:
    """渲染标签云（循环内字符串拼接）。"""
    html = ""
    for tag, weight in tags:
        html = html + f'<span class="w{weight}">{tag}</span>'
    return html


def publish_all(slugs: list[str], out_dir: str) -> int:
    """逐篇发布页面（循环体内 open/close，应复用句柄或批量写出）。"""
    done = 0
    for slug in slugs:
        target = f"{out_dir}/{slug}/index.html"
        fh = open(target, "w", encoding="utf-8")
        fh.write(render_page("<main>{{body}}</main>", {}, f"post {slug}"))
        fh.close()
        done += 1
    return done
