"""Stage1 接入包：目录 / zip → 工作副本 + FileManifest。"""

from audit.ingest.core import ingest
from audit.ingest.decode import decode_source_bytes, read_text_smart
from audit.ingest.filters import GitignoreSet, should_ignore

__all__ = [
    "ingest",
    "decode_source_bytes",
    "read_text_smart",
    "GitignoreSet",
    "should_ignore",
]
