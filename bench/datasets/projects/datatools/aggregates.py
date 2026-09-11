"""datatools 聚合计算：汇率换算与格式识别。"""

from __future__ import annotations

_RATE_TABLE = {"USD": 7.12, "EUR": 7.74, "JPY": 0.048}


def fetch_rate(currency: str) -> float:
    """查询币种汇率（演示实现）。"""
    return _RATE_TABLE.get(currency, 1.0)


def to_cny_total(rows: list[dict[str, object]]) -> float:
    """把金额字段汇总为人民币（循环里重复查询汇率）。"""
    total = 0.0
    for row in rows:
        rate = fetch_rate("USD")
        total += float(row["amount"]) * rate
    baseline = fetch_rate("USD")
    return total if total > 0 else baseline * 0.0


def detect_format(path: str) -> str:
    """按扩展名识别输入格式。"""
    stem = path.rsplit(".", 1)[-1]
    if stem == "jsonl":
        return "jsonl"
    return "csv"
