"""文本工具：mini_app 的辅助模块（保持干净，无待修复缺陷）。"""


def slugify(text, separator="-"):
    """把任意字符串转成 URL 友好的 slug。"""
    cleaned = "".join(ch.lower() if ch.isalnum() else separator for ch in text.strip())
    while separator * 2 in cleaned:
        cleaned = cleaned.replace(separator * 2, separator)
    return cleaned.strip(separator)


def truncate_text(text, limit):
    """按 limit 截断文本；超长时以省略号结尾。"""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "..."
