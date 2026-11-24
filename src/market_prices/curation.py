"""Deterministic identities, explicit aliases and as-of weekly series diagnostics."""

import csv
import json
import math
import re
from datetime import date
from pathlib import Path

from market_prices.contracts import normalized_spec, stable_id

RAW_COLUMNS = ["日期", "商品名称", "产品规格", "计量单位", "区", "市场", "价格"]
RAW_FIELDS = [
    "quote_date_raw",
    "product_name_raw",
    "product_spec_raw",
    "unit_raw",
    "district_raw",
    "market_raw",
    "price_raw",
]
SOURCE_LONG = "成都农贸市场价格_近两年_长表.csv"
SOURCE_WIDE = "成都农贸市场价格_近两年_宽表.csv"
SOURCE_URL = "https://cddrc.chengdu.gov.cn/cdsfzggw/scjg/pricelist.shtml"
UNIT_ROWS = [
    {"unit_id": "cny_per_500g", "unit": "元/500克", "basis_quantity": 500, "basis_unit": "克"},
    {"unit_id": "cny_per_bucket", "unit": "元/桶", "basis_quantity": 1, "basis_unit": "桶"},
    {"unit_id": "cny_per_bag", "unit": "元/袋", "basis_quantity": 1, "basis_unit": "袋"},
]


def entity_id(kind: str, *labels: str) -> str:
    return f"{kind}_" + stable_id([kind, *labels])


def run_folder(root: Path, run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id):
        raise ValueError("run-id must be 1-80 letters, numbers, underscores or hyphens")
    return root / "runs" / run_id


def read_aliases(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as file:
        aliases = list(csv.DictReader(file))
    seen = set()
    canonical = {}
    for row in aliases:
        key = tuple(row[f"{field}_raw"] for field in ("product_name", "product_spec", "unit"))
        if key in seen:
            raise ValueError(f"Duplicate alias: {key}")
        seen.add(key)
        if not all(row.values()):
            raise ValueError(f"Empty alias field: {key}")
        if (
            row["product_name_raw"].strip() != row["product_name"]
            or row["unit_raw"].strip() != row["unit"]
            or normalized_spec(row["product_spec_raw"]) != normalized_spec(row["product_spec"])
        ):
            raise ValueError(f"Semantic alias merge is forbidden: {key}")
        row["product_id"] = entity_id(
            "product", row["product_name"], normalized_spec(row["product_spec"]), row["unit"]
        )
        definition = tuple(
            row[field] for field in ("product_name", "product_spec", "unit", "category")
        )
        if canonical.setdefault(row["product_id"], definition) != definition:
            raise ValueError(f"Inconsistent canonical product definition: {key}")
    return aliases


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def forecast_profiles(weekly: list[dict], as_of: date, policy: dict) -> list[dict]:
    """Diagnostic shortlist only; no filled prices or model features are generated."""
    by_product = {}
    for row in weekly:
        day = date.fromisoformat(str(row["quote_date"]))
        if day > as_of:
            continue
        series = by_product.setdefault(row["product_id"], {})
        if day in series:
            raise ValueError("Duplicate product/week in forecast input")
        series[day] = row
    output = []
    for product_id, series in sorted(by_product.items()):
        first, last = min(series), max(series)
        span = (as_of - first).days // 7 + 1
        latest = series[last]
        panel = int(latest["expected_market_count"])
        required = math.ceil(panel * policy["min_market_coverage"])
        adequate = {
            day
            for day, row in series.items()
            if int(row["quoted_market_count"]) >= required and row["mean_price"] is not None
        }
        tail = 0
        for offset in range(span):
            if date.fromordinal(as_of.toordinal() - offset * 7) not in adequate:
                break
            tail += 1
        coverage = len(adequate) / span
        reasons = []
        if len(adequate) < policy["min_adequate_weeks"]:
            reasons.append("short_history")
        if coverage < policy["min_history_coverage"]:
            reasons.append("low_history_coverage")
        if tail < policy["min_recent_consecutive_weeks"]:
            reasons.append("recent_gaps")
        if as_of not in adequate:
            reasons.append("no_adequate_current_week")
        output.append(
            {
                "product_id": product_id,
                "product_name": latest["product_name"],
                "product_spec": latest["product_spec"],
                "unit": latest["unit"],
                "as_of": as_of.isoformat(),
                "first_observed_week": first.isoformat(),
                "last_observed_week": last.isoformat(),
                "observed_weeks": len(series),
                "weeks_with_any_price": sum(
                    row["mean_price"] is not None for row in series.values()
                ),
                "adequate_weeks": len(adequate),
                "expected_weeks_since_first": span,
                "absent_weeks_since_first": span - len(series),
                "history_coverage": coverage,
                "recent_consecutive_adequate_weeks": tail,
                "minimum_quoted_markets": required,
                "expected_market_count": panel,
                "eligible_baseline": not reasons,
                "annual_seasonality_candidate": tail >= policy["annual_seasonality_weeks"],
                "exclusion_reasons": reasons,
            }
        )
    return output
