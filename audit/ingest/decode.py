"""源码解码容错（utf-8 → gbk → charset-normalizer → replace）。

Stage1 接入面对的是"任意来源"的代码包，编码不可假设。
本模块提供统一的解码入口，保证任何字节序列都能产出可用的文本。
"""

from __future__ import annotations

from pathlib import Path

# 依次尝试的确定编码：utf-8-sig 兼容带 BOM 与不带 BOM 两种情况
_DETERMINISTIC_ENCODINGS = ("utf-8-sig", "utf-8", "gbk")


def decode_source_bytes(data: bytes) -> str:
    """把源文件字节流解码为文本。

    策略：utf-8 → gbk → charset-normalizer 猜测 → utf-8 errors=replace。
    永不抛 UnicodeDecodeError。
    """
    if not data:
        return ""
    for enc in _DETERMINISTIC_ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(data).best()
        if best is not None:
            return str(best)
    except Exception:  # pragma: no cover - charset-normalizer 异常时兜底
        pass
    return data.decode("utf-8", errors="replace")


def read_text_smart(path: Path) -> str:
    """读取文件并解码为文本（解码容错版 read_text）。"""
    return decode_source_bytes(path.read_bytes())
