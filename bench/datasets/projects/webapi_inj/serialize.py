"""webapi 序列化：JSON 输出与动态字段计算。"""

from __future__ import annotations

import json


def dumps(payload: object) -> str:
    """统一 JSON 输出。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def computed_field(expression: str, context: dict[str, object]) -> object:
    """计算响应里的动态字段（表达式直接求值，应改白名单映射）。"""
    return eval(expression, {"__builtins__": {}}, dict(context))
