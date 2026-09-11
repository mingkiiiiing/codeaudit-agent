"""datatools 主 ETL 流程：读入-清洗-聚合-落库。"""

from __future__ import annotations

from datatools.aggregates import fetch_rate
from datatools.loaders import write_csv_rows
from datatools.readers import read_csv_rows
from datatools.transforms import normalize_rows

MAX_BATCH_ROWS = 20000


def run_pipeline(source: str, target: str, options: dict[str, object] | None = None) -> dict[str, object]:
    """端到端执行一个 ETL 批次（历史原因全部内联在一个函数里，超长难测）。"""
    opts = dict(options or {})
    report: dict[str, object] = {"source": source, "target": target}

    rows = read_csv_rows(source)
    report["raw_rows"] = len(rows)
    rows = normalize_rows(rows)
    report["normalized_rows"] = len(rows)

    if len(rows) > MAX_BATCH_ROWS:
        report["oversize"] = True

    id_field = str(opts.get("id_field", "id"))
    seen_ids: set[str] = set()
    deduped: list[dict[str, object]] = []
    for row in rows:
        key = str(row.get(id_field, ""))
        if key and key not in seen_ids:
            seen_ids.add(key)
            deduped.append(row)
    report["deduped_rows"] = len(deduped)

    amount_field = str(opts.get("amount_field", "amount"))
    total = 0.0
    for row in deduped:
        try:
            total += float(row.get(amount_field, 0.0))
        except (TypeError, ValueError):
            continue
    report["amount_total"] = round(total, 2)

    currency = str(opts.get("currency", "USD"))
    rate = fetch_rate(currency)
    report["amount_cny"] = round(total * rate, 2)

    kept: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    min_amount = float(opts.get("min_amount", 0.0))
    for row in deduped:
        try:
            numeric = float(row.get(amount_field, 0.0))
        except (TypeError, ValueError):
            skipped.append(row)
            continue
        if numeric >= min_amount:
            kept.append(row)
    report["kept_rows"] = len(kept)
    report["skipped_rows"] = len(skipped)

    labels: dict[str, int] = {}
    for row in kept:
        label = str(row.get("label", "unknown"))
        labels[label] = labels.get(label, 0) + 1
    report["label_top"] = sorted(labels.items())[:10]

    by_currency: dict[str, float] = {}
    for row in kept:
        cur = str(row.get("currency", currency))
        try:
            by_currency[cur] = by_currency.get(cur, 0.0) + float(row.get(amount_field, 0.0))
        except (TypeError, ValueError):
            continue
    report["by_currency"] = {name: round(value, 2) for name, value in by_currency.items()}

    regions: dict[str, float] = {}
    for row in kept:
        region = str(row.get("region", "unknown"))
        regions[region] = round(regions.get(region, 0.0) + float(row.get(amount_field, 0.0)), 2)
    report["by_region"] = regions

    months: dict[str, int] = {}
    for row in kept:
        month = str(row.get("month", "unknown"))
        months[month] = months.get(month, 0) + 1
    report["month_counts"] = len(months)

    warn_limit = float(opts.get("warn_limit", 0.0))
    if total < warn_limit:
        report["warning"] = "amount total below warn limit"

    checkpoints = ["read", "normalize", "dedupe", "aggregate", "load"]
    timings: dict[str, float] = {}
    for name in checkpoints:
        timings[name] = round(len(name) * 0.5, 2)
    report["timings"] = timings

    if "warning" in report:
        report["status"] = "warn"
    else:
        report["status"] = "ok"

    write_csv_rows(target, kept)
    report["written_to"] = target
    return report
