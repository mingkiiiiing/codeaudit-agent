"""webapi 应用装配与入口。"""

from __future__ import annotations

from webapi.db import connect
from webapi.handlers import board_payload
from webapi.settings import DEBUG

MAX_REQUEST_BODY = 1048576


def build_app(db_path: str = "webapi.db") -> dict[str, object]:
    """组装应用上下文（演示返回 dict 容器）。"""
    return {"conn": connect(db_path), "debug": DEBUG}


def main() -> int:
    """跑通一次看板读取路径。"""
    app = build_app()
    payload = board_payload(app["conn"], "board-1")
    # TODO: 接入真实 HTTP 框架前先补齐集成测试
    print(f"[webapi] payload keys: {sorted(payload)}")
    return 0 if "error" not in payload else 1


if __name__ == "__main__":
    raise SystemExit(main())
