"""Reconcile the running HTTP application with the independently opened Day-2 catalog."""

import argparse
import csv
import io
import json
import math
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

import duckdb

from market_prices.curation import write_json


def rows(db, sql, params=None):
    result = db.execute(sql, params or [])
    keys = [c[0] for c in result.description]
    return [dict(zip(keys, values)) for values in result.fetchall()]


def equal_price(value, expected):
    return (
        value is None
        and expected is None
        or value is not None
        and expected is not None
        and Decimal(value) == expected
    )


def equal_rate(value, expected):
    if value is None or expected is None:
        return value is None and expected is None
    return math.isclose(float(value), float(expected), abs_tol=1e-7, rel_tol=1e-7)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18880")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    pointer = json.loads((root / "runtime/warehouse/current.json").read_text(encoding="utf-8"))
    run_id = pointer["run_id"]
    folder = root / "runtime/warehouse" / pointer["relative_path"]
    checks = {}

    def request(path, params=None, status=200, binary=False):
        url = args.base_url.rstrip("/") + path
        if params:
            url += "?" + urlencode(params)
        try:
            response = urlopen(url, timeout=15)
        except HTTPError as error:
            response = error
        with response:
            payload = response.read()
            assert response.status == status, (
                f"{path}: HTTP {response.status}, expected {status}: {payload[:200]}"
            )
            return payload if binary else json.loads(payload)

    def api(path, params=None, status=200):
        return request("/api/v1/" + path, {"run_id": run_id, **(params or {})}, status)

    with duckdb.connect(str(folder / "warehouse.duckdb"), read_only=True) as db:
        products = rows(db, "SELECT * FROM products")
        latest = db.execute("SELECT max(quote_date) FROM calendar").fetchone()[0]
        expected_trends = {
            p["product_id"]: rows(
                db,
                "SELECT * FROM product_weekly WHERE product_id=? ORDER BY quote_date",
                [p["product_id"]],
            )
            for p in products
        }
        expected_latest = {
            p["product_id"]: rows(
                db,
                "SELECT * FROM quotes WHERE product_id=? AND quote_date=?",
                [p["product_id"], latest],
            )
            for p in products
        }
        overview = api("overview")
        checks["verified_snapshot_and_source_match"] = (
            overview["meta"]["run_id"] == run_id
            and overview["meta"]["source_file_sha256"]
            == db.execute("SELECT source_file_sha256 FROM build_metadata").fetchone()[0]
        )
        checks["overview_counts_match_catalog"] = (
            overview["totals"]["source_rows"]
            == db.execute("SELECT count(*) FROM quotes").fetchone()[0]
            and overview["totals"]["quoted_rows"]
            == db.execute("SELECT count(price) FROM quotes").fetchone()[0]
        )
        catalog = api("products")["items"]
        checks["all_product_specifications_and_units_match"] = {
            (r["product_id"], r["product_name"], r["product_spec"], r["unit"]) for r in catalog
        } == {(r["product_id"], r["product_name"], r["product_spec"], r["unit"]) for r in products}
        checks["all_market_memberships_match"] = {
            (r["market_id"], r["district"], r["market"]) for r in api("markets")["items"]
        } == {
            (r["market_id"], r["district"], r["market"]) for r in rows(db, "SELECT * FROM markets")
        }

        def product_checks(product):
            pid = product["product_id"]
            trend = api("trends", {"product_id": pid})
            expected = expected_trends[pid]
            trend_ok = len(trend["items"]) == len(expected) and all(
                actual["quote_date"] == str(wanted["quote_date"])
                and all(
                    equal_price(actual[field], wanted[field])
                    for field in [
                        "mean_price",
                        "median_price",
                        "min_price",
                        "max_price",
                        "price_range",
                    ]
                )
                and actual["quoted_market_count"] == wanted["quoted_market_count"]
                and equal_rate(
                    actual["common_market_wow_change"], wanted["common_market_wow_change"]
                )
                and actual["common_market_count"] == wanted["common_market_count"]
                for actual, wanted in zip(trend["items"], expected)
            )
            price = api(
                "prices",
                {"product_id": pid, "date_from": latest, "date_to": latest, "page_size": 100},
            )
            expected_prices = {r["source_record_id"]: r for r in expected_latest[pid]}
            price_ok = (
                price["total"] == len(expected_prices)
                and len(price["items"]) == len(expected_prices)
                and all(
                    r["source_record_id"] in expected_prices
                    and equal_price(r["price"], expected_prices[r["source_record_id"]]["price"])
                    and all(
                        r[field] == expected_prices[r["source_record_id"]][field]
                        for field in [
                            "price_raw",
                            "quote_status",
                            "product_spec_raw",
                            "unit",
                            "source_row_number",
                            "market",
                        ]
                    )
                    for r in price["items"]
                )
            )
            comparison = api("comparisons", {"product_id": pid, "quote_date": latest})
            values = [r["price"] for r in expected_prices.values() if r["price"] is not None]
            quote_by_market = {r["market_id"]: r for r in expected_prices.values()}
            comparison_ok = len(comparison["items"]) == 18 and comparison["summary"][
                "quoted_market_count"
            ] == len(values)
            for item in comparison["items"]:
                original = quote_by_market.get(item["market_id"])
                status = original["quote_status"] if original else "not_observed"
                value = original["price"] if original else None
                rank = (
                    1 + sum(v < value for v in values)
                    if value is not None and len(values) > 1
                    else None
                )
                comparison_ok = (
                    comparison_ok
                    and item["quote_status"] == status
                    and equal_price(item["price"], value)
                    and item["rank"] == rank
                )
            return (
                trend_ok,
                price_ok,
                comparison_ok,
                len(expected),
                len(expected_prices),
                trend["meta"]["run_id"]
                == price["meta"]["run_id"]
                == comparison["meta"]["run_id"]
                == run_id,
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(product_checks, products))
        checks["all_product_trends_match_verified_weekly_data"] = all(r[0] for r in results)
        checks["all_latest_quote_rows_match_original_values"] = all(r[1] for r in results)
        checks["all_latest_market_prices_statuses_and_tied_ranks_match"] = all(
            r[2] for r in results
        )
        checks["concurrent_queries_stay_on_same_snapshot"] = all(r[5] for r in results)
        rice = next(p for p in products if p["product_name"] == "籼米")
        pid = rice["product_id"]
        district = api("comparisons", {"product_id": pid, "district": "金牛"})
        checks["district_price_and_coverage_match"] = (
            district["summary"]["mean_price"] == "2.6500"
            and district["summary"]["quoted_market_count"] == 2
            and district["summary"]["expected_market_count"] == 3
        )
        point = api(
            "trends",
            {"product_id": pid, "district": "金牛", "date_from": latest, "date_to": latest},
        )["items"][0]
        checks["narrow_date_window_keeps_previous_week_context"] = (
            equal_rate(point["common_market_wow_change"], district["common_market_wow_change"])
            and point["common_market_count"] == district["common_market_count"]
            and point["wow_change"] is not None
        )
        missing = next(r for r in district["items"] if r["quote_status"] == "missing")
        single = api("comparisons", {"product_id": pid, "market_id": missing["market_id"]})
        checks["single_missing_market_does_not_become_zero"] = (
            single["summary"]["mean_price"] is None
            and single["summary"]["missing_market_count"] == 1
            and single["summary"]["ranking_available"] is False
        )
        first = api("prices", {"product_id": pid, "page_size": 20})
        second = api("prices", {"product_id": pid, "page_size": 20, "page": 2})
        checks["pagination_has_no_overlap"] = len(first["items"]) == len(
            second["items"]
        ) == 20 and not (
            {r["source_record_id"] for r in first["items"]}
            & {r["source_record_id"] for r in second["items"]}
        )
        ordered = api("prices", {"product_id": pid, "sort": "price_asc", "page_size": 100})
        values = [Decimal(r["price"]) for r in ordered["items"] if r["price"] is not None]
        checks["price_sort_is_numeric"] = values == sorted(values)
        tomato = next(p for p in products if p["product_name"] == "西红柿")
        flagged = api(
            "prices", {"product_id": tomato["product_id"], "review_only": "true", "page_size": 100}
        )
        expected_flags = db.execute(
            "SELECT count(*) FROM quotes WHERE product_id=? AND len(review_flags)>0",
            [tomato["product_id"]],
        ).fetchone()[0]
        checks["review_filter_uses_existing_flags"] = flagged["total"] == expected_flags and all(
            r["review_flags"] for r in flagged["items"]
        )
        csv_bytes = request(
            "/api/v1/prices/export.csv",
            {"run_id": run_id, "product_id": pid, "district": "金牛", "quote_status": "missing"},
            binary=True,
        )
        exported = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
        expected_missing = db.execute(
            "SELECT count(*) FROM quotes WHERE product_id=? "
            "AND district='金牛' AND quote_status='missing'",
            [pid],
        ).fetchone()[0]
        checks["csv_export_matches_all_filtered_records"] = (
            csv_bytes.startswith(b"\xef\xbb\xbf")
            and len(exported) == expected_missing
            and all(r["原始价格"] == "-" and r["解析价格"] == "" for r in exported)
        )
        checks["unknown_product_returns_404"] = (
            api("prices", {"product_id": "unknown"}, 404)["detail"]["code"] == "product_not_found"
        )
        checks["invalid_date_range_returns_422"] = (
            api(
                "trends",
                {"product_id": pid, "date_from": "2026-11-24", "date_to": "2026-11-17"},
                422,
            )["detail"]["code"]
            == "invalid_date_range"
        )
        checks["invalid_pagination_returns_422"] = "detail" in api(
            "prices", {"product_id": pid, "page_size": 1000}, 422
        )
        html = request("/", binary=True)
        checks["same_origin_frontend_is_served"] = (
            b'<div id="root">' in html and b"/assets/" in html
        )
        current = json.loads((root / "runtime/warehouse/current.json").read_text(encoding="utf-8"))
        checks["active_warehouse_pointer_unchanged"] = current == pointer
    report = {
        "passed": all(checks.values()),
        "run_id": run_id,
        "api_url": args.base_url,
        "products_verified": len(products),
        "weekly_points_verified": sum(r[3] for r in results),
        "latest_source_rows_verified": sum(r[4] for r in results),
        "checks": checks,
        "note": "HTTP结果与独立打开的已验收数据库核对；价格保持原值，未生成预测值。",
    }
    write_json(root / "artifacts/day3/api-verification.json", report)
    print(json.dumps(report, ensure_ascii=False))
    if not report["passed"]:
        raise AssertionError(
            f"Failed checks: {[name for name, passed in checks.items() if not passed]}"
        )


if __name__ == "__main__":
    main()
