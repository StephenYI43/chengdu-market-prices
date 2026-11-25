"""Independently reconcile Spark outputs against original CSVs using Python and DuckDB."""

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import duckdb
import pyarrow as pa

from market_prices.contracts import normalized_spec, stable_id
from market_prices.curation import (
    RAW_COLUMNS,
    RAW_FIELDS,
    SOURCE_LONG,
    SOURCE_WIDE,
    UNIT_ROWS,
    entity_id,
    read_aliases,
    run_folder,
    write_json,
)
from market_prices.job_store import publish_snapshot

TABLES = {
    "raw_quotes": "ods",
    "quotes": "dwd/quotes",
    "products": "dim/products",
    "product_aliases": "dim/product_aliases",
    "markets": "dim/markets",
    "districts": "dim/districts",
    "units": "dim/units",
    "calendar": "dim/calendar",
    "quarantined_quotes": "quarantine/quotes",
    "duplicate_quotes": "audit/duplicates",
    "review_flags": "audit/review_flags",
    "spec_changes": "audit/spec_changes",
    "product_weekly": "dws/product_weekly",
    "district_weekly": "dws/district_weekly",
    "forecast_profiles": "mart/forecast_profiles",
}
FOUR_PLACES = Decimal("0.0001")


def records(db, query):
    result = db.execute(query)
    names = [column[0] for column in result.description]
    return [dict(zip(names, row)) for row in result.fetchall()]


def scalar(db, query):
    # DuckDB 2 requires the DB-API result to be drained before a relation query.
    return db.execute(query).fetchall()[0][0]


def close(a, b, tolerance=1e-7):
    if a is None or b is None:
        return a is None and b is None
    return math.isclose(float(a), float(b), abs_tol=tolerance, rel_tol=tolerance)


def aggregate(rows):
    prices = [r["price"] for r in rows if r["price"] is not None]
    return {
        "observed_market_count": len(rows),
        "quoted_market_count": len(prices),
        "missing_market_count": len(rows) - len(prices),
        "mean_price": (sum(prices) / len(prices)).quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)
        if prices
        else None,
        "median_price": statistics.median(prices).quantize(FOUR_PLACES) if prices else None,
        "min_price": min(prices) if prices else None,
        "max_price": max(prices) if prices else None,
        "price_range": max(prices) - min(prices) if prices else None,
        "stddev_price": statistics.pstdev(prices) if prices else None,
        "ranking_available": len(prices) >= 2,
    }


def csv_export(path, rows):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (list, dict))
                    else value
                    for key, value in row.items()
                }
            )


def verify(db, folder, root, source_dir):
    stage = json.loads((folder / "stage.json").read_text(encoding="utf-8"))
    spark_result = json.loads((folder / "spark-result.json").read_text(encoding="utf-8"))
    policy = json.loads((root / "config/curation-policy.json").read_text(encoding="utf-8"))
    source = source_dir / SOURCE_LONG
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    with source.open(encoding="utf-8-sig", newline="") as file:
        source_rows = list(csv.DictReader(file))
    alias_rows = read_aliases(root / "config/product-aliases.csv")
    aliases = {
        tuple(r[f"{f}_raw"] for f in ("product_name", "product_spec", "unit")): r
        for r in alias_rows
    }
    units = {r["unit"]: r["unit_id"] for r in UNIT_ROWS}
    expected = {}
    for number, row in enumerate(source_rows, start=1):
        alias = aliases[(row["商品名称"], row["产品规格"], row["计量单位"])]
        business_key = stable_id(
            [
                row["日期"],
                row["商品名称"],
                normalized_spec(row["产品规格"]),
                row["计量单位"],
                row["区"],
                row["市场"],
            ]
        )
        expected[number] = {
            **dict(zip(RAW_FIELDS, (row[c] for c in RAW_COLUMNS))),
            **{
                key: alias[key]
                for key in ("product_id", "product_name", "product_spec", "unit", "category")
            },
            "quote_date": date.fromisoformat(row["日期"]),
            "price": None if row["价格"] == "-" else Decimal(row["价格"]),
            "quote_status": "missing" if row["价格"] == "-" else "quoted",
            "missing_reason": "source_no_quote" if row["价格"] == "-" else None,
            "district": row["区"],
            "market": row["市场"],
            "district_id": entity_id("district", row["区"]),
            "market_id": entity_id("market", row["区"], row["市场"]),
            "unit_id": units[row["计量单位"]],
            "source_row_number": number,
            "source_record_id": stable_id([sha, str(number)]),
            "source_file_sha256": sha,
            "source_filename": source.name,
            "source_url": stage["source_url"],
            "business_key": business_key,
            "event_id": stable_id([sha, business_key, row["价格"]]),
            "quote_year": date.fromisoformat(row["日期"]).year,
        }
    actual = records(db, "SELECT * FROM quotes ORDER BY source_row_number")
    raw = records(db, "SELECT * FROM raw_quotes ORDER BY source_row_number")
    city_groups, district_groups = defaultdict(list), defaultdict(list)
    point_prices = {}
    for row in expected.values():
        city_groups[(row["product_id"], row["quote_date"])].append(row)
        district_groups[(row["product_id"], row["quote_date"], row["district_id"])].append(row)
        point_prices[(row["product_id"], row["quote_date"], row["market_id"])] = row["price"]
    city_stats = {key: aggregate(rows) for key, rows in city_groups.items()}
    district_stats = {key: aggregate(rows) for key, rows in district_groups.items()}
    weekly = records(db, "SELECT * FROM product_weekly ORDER BY product_id, quote_date")
    district_weekly = records(db, "SELECT * FROM district_weekly")
    calendar = sorted({r["quote_date"] for r in expected.values()})
    as_of = calendar[-1]
    with (root / "config/markets.csv").open(encoding="utf-8", newline="") as file:
        configured_markets = list(csv.DictReader(file))
    panel = len(configured_markets)
    district_panels = Counter(r["district"] for r in configured_markets)
    checks = {
        "source_hash_unchanged": sha == stage["source_file_sha256"],
        "wide_hash_unchanged": hashlib.sha256((source_dir / SOURCE_WIDE).read_bytes()).hexdigest()
        == stage["wide_file_sha256"],
        "wide_long_exact_match": stage["wide_long_exact_match"],
        "configuration_hashes_unchanged": all(
            hashlib.sha256((root / "config" / filename).read_bytes()).hexdigest() == stage[key]
            for filename, key in [
                ("product-aliases.csv", "alias_file_sha256"),
                ("curation-policy.json", "policy_file_sha256"),
                ("markets.csv", "market_file_sha256"),
            ]
        ),
        "all_source_rows_accounted_for": len(actual)
        == len(source_rows)
        == spark_result["source_rows"]
        == stage["source_rows"],
        "raw_strings_and_record_numbers_match": len(raw) == len(expected)
        and all(
            r["source_row_number"] == number
            and all(r[field] == expected[number][field] for field in RAW_FIELDS)
            for number, r in enumerate(raw, start=1)
        ),
        "all_clean_fields_match_original": len(actual) == len(expected)
        and all(
            r["source_row_number"] in expected
            and all(r[field] == value for field, value in expected[r["source_row_number"]].items())
            for r in actual
        ),
        "no_rejected_or_duplicate_rows": scalar(db, "SELECT count(*) FROM quarantined_quotes") == 0
        and scalar(db, "SELECT count(*) FROM duplicate_quotes") == 0,
        "business_and_record_ids_unique": scalar(
            db,
            "SELECT count(*)=count(DISTINCT business_key) "
            "AND count(*)=count(DISTINCT source_record_id) FROM quotes",
        ),
        "missing_values_preserved": scalar(
            db,
            "SELECT count(*) FROM quotes WHERE price IS NULL AND quote_status='missing' "
            "AND missing_reason='source_no_quote'",
        )
        == stage["source_missing_rows"],
        "calendar_is_complete_weekly": all(
            (b - a).days == 7 for a, b in zip(calendar, calendar[1:])
        )
        and all(d.weekday() == 1 for d in calendar),
        "dimension_membership_and_keys": scalar(
            db,
            """SELECT count(*) FROM quotes q
            LEFT JOIN products p ON q.product_id=p.product_id
            LEFT JOIN markets m ON q.market_id=m.market_id
            LEFT JOIN units u ON q.unit_id=u.unit_id
            LEFT JOIN districts d ON q.district_id=d.district_id
            WHERE p.product_id IS NULL OR m.market_id IS NULL
              OR u.unit_id IS NULL OR d.district_id IS NULL""",
        )
        == 0,
        "product_catalog_matches_source": {
            r["product_id"] for r in records(db, "SELECT * FROM products")
        }
        == {r["product_id"] for r in expected.values()},
        "market_catalog_matches_registry": {
            (r["district"], r["market"]) for r in records(db, "SELECT * FROM markets")
        }
        == {(r["district"], r["market"]) for r in configured_markets},
        "aliases_match_explicit_mapping": scalar(db, "SELECT count(*) FROM product_aliases")
        == len(alias_rows),
    }

    def aggregate_matches(row, expected_group):
        return all(
            close(row[k], v) if k == "stddev_price" else row[k] == v
            for k, v in expected_group.items()
        )

    checks["all_city_weekly_aggregates_match"] = len(weekly) == len(city_stats) and all(
        (r["product_id"], r["quote_date"]) in city_stats
        and aggregate_matches(r, city_stats[(r["product_id"], r["quote_date"])])
        and r["expected_market_count"] == panel
        and close(r["market_coverage"], r["quoted_market_count"] / panel)
        for r in weekly
    )
    checks["all_district_weekly_aggregates_match"] = len(district_weekly) == len(
        district_stats
    ) and all(
        aggregate_matches(r, district_stats[(r["product_id"], r["quote_date"], r["district_id"])])
        and r["expected_market_count"] == district_panels[r["district"]]
        and close(r["market_coverage"], r["quoted_market_count"] / district_panels[r["district"]])
        for r in district_weekly
    )
    wow_ok, common_ok = True, True
    previous = {}
    for row in weekly:
        key = (row["product_id"], row["quote_date"])
        current = city_stats[key]["mean_price"]
        prev = previous.get(row["product_id"])
        expected_wow = (
            current / prev[1] - 1
            if prev and (row["quote_date"] - prev[0]).days == 7 and prev[1] and current
            else None
        )
        wow_ok = wow_ok and close(row["wow_change"], expected_wow)
        previous[row["product_id"]] = (row["quote_date"], current)
        pairs = [
            (
                r["price"],
                point_prices.get(
                    (r["product_id"], r["quote_date"] - timedelta(days=7), r["market_id"])
                ),
            )
            for r in city_groups[key]
        ]
        pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
        rate = sum(a for a, _ in pairs) / sum(b for _, b in pairs) - 1 if pairs else None
        common_ok = (
            common_ok
            and row["common_market_count"] == len(pairs)
            and close(row["common_market_wow_change"], rate)
        )
    checks["week_changes_do_not_bridge_gaps_or_specs"] = wow_ok
    checks["same_market_week_changes_match_source"] = common_ok
    flags = policy["review_flags"]
    expected_flags = {}
    for number, row in expected.items():
        value = row["price"]
        prev = point_prices.get(
            (row["product_id"], row["quote_date"] - timedelta(days=7), row["market_id"])
        )
        median = city_stats[(row["product_id"], row["quote_date"])]["median_price"]
        codes = []
        if (
            value is not None
            and prev is not None
            and abs(value / prev - 1) >= Decimal(str(flags["absolute_weekly_change_ratio"]))
        ):
            codes.append("large_weekly_change")
        if (
            value is not None
            and median
            and value / median >= Decimal(str(flags["cross_market_high_ratio"]))
        ):
            codes.append("high_vs_peer_median")
        if (
            value is not None
            and median
            and value / median <= Decimal(str(flags["cross_market_low_ratio"]))
        ):
            codes.append("low_vs_peer_median")
        if codes:
            expected_flags[number] = codes
    actual_flags = {r["source_row_number"]: r["review_flags"] for r in actual if r["review_flags"]}
    checks["review_flags_match_without_changing_prices"] = actual_flags == expected_flags
    checks["review_audit_matches_clean_records"] = {
        r["source_row_number"]: r["review_flags"] for r in records(db, "SELECT * FROM review_flags")
    } == expected_flags

    fixture_path = root / "data/samples/day2-weekly-excerpt.jsonl"
    fixture = [json.loads(line) for line in fixture_path.read_text(encoding="utf-8").splitlines()]
    checks["real_history_test_excerpt_matches_source"] = all(
        (r["product_id"], date.fromisoformat(r["quote_date"])) in city_stats
        and str(city_stats[(r["product_id"], date.fromisoformat(r["quote_date"]))]["mean_price"])
        == r["mean_price"]
        and city_stats[(r["product_id"], date.fromisoformat(r["quote_date"]))][
            "quoted_market_count"
        ]
        == r["quoted_market_count"]
        for r in fixture
    )

    independent_weekly = []
    for (pid, day), stats in city_stats.items():
        source_row = city_groups[(pid, day)][0]
        independent_weekly.append(
            {
                "product_id": pid,
                "quote_date": day,
                "product_name": source_row["product_name"],
                "product_spec": source_row["product_spec"],
                "unit": source_row["unit"],
                "expected_market_count": panel,
                **stats,
            }
        )
    db.register("independent_weekly", pa.Table.from_pylist(independent_weekly))
    db.register("independent_calendar", pa.table({"quote_date": calendar}))
    rule = policy["forecast"]
    required = math.ceil(panel * rule["min_market_coverage"])
    sql_profiles = records(
        db,
        f"""
        WITH starts AS (
          SELECT product_id,min(quote_date) first_week FROM independent_weekly GROUP BY product_id
        ),
        grid AS (
          SELECT s.product_id,c.quote_date,w.mean_price,w.quoted_market_count,
                 w.product_id IS NOT NULL observed,
                 coalesce(w.quoted_market_count>={required}
                   AND w.mean_price IS NOT NULL,false) adequate
          FROM starts s CROSS JOIN independent_calendar c LEFT JOIN independent_weekly w
            ON s.product_id=w.product_id AND c.quote_date=w.quote_date
          WHERE c.quote_date>=s.first_week
        ), counts AS (
          SELECT product_id,count(*) span,count(*) FILTER(WHERE observed) observed_weeks,
            count(*) FILTER(WHERE mean_price IS NOT NULL) priced_weeks,
            count(*) FILTER(WHERE adequate) good_weeks,
            max(quote_date) FILTER(WHERE NOT adequate) last_bad FROM grid GROUP BY product_id
        ) SELECT *,CASE WHEN last_bad IS NULL THEN span
          ELSE date_diff('day',last_bad,DATE '{as_of}')//7 END tail FROM counts
    """,
    )
    profiles = {r["product_id"]: r for r in records(db, "SELECT * FROM forecast_profiles")}
    profiles_ok = len(sql_profiles) == len(profiles)
    for row in sql_profiles:
        p = profiles[row["product_id"]]
        coverage = row["good_weeks"] / row["span"]
        eligible = (
            row["good_weeks"] >= rule["min_adequate_weeks"]
            and coverage >= rule["min_history_coverage"]
            and row["tail"] >= rule["min_recent_consecutive_weeks"]
        )
        profiles_ok = profiles_ok and (
            p["observed_weeks"] == row["observed_weeks"]
            and p["expected_weeks_since_first"] == row["span"]
            and p["weeks_with_any_price"] == row["priced_weeks"]
            and p["adequate_weeks"] == row["good_weeks"]
            and p["recent_consecutive_adequate_weeks"] == row["tail"]
            and close(p["history_coverage"], coverage)
            and p["eligible_baseline"] == eligible
            and p["annual_seasonality_candidate"]
            == (row["tail"] >= rule["annual_seasonality_weeks"])
        )
    checks["forecast_profiles_match_independent_calendar_sql"] = profiles_ok
    spec_history = defaultdict(lambda: defaultdict(set))
    for row in expected.values():
        spec_history[row["product_name"]][row["quote_date"]].add(
            (row["product_id"], row["product_spec"], row["unit"])
        )
    expected_changes = set()
    for name, history in spec_history.items():
        days = sorted(history)
        for prev, cur in zip(days, days[1:]):
            if (cur - prev).days == 7 and history[cur] != history[prev]:
                expected_changes.add((name, cur))
    changes = records(db, "SELECT * FROM spec_changes")
    checks["semantic_specification_changes_preserved"] = {
        (r["product_name"], r["quote_date"]) for r in changes
    } == expected_changes
    summary = {
        "source_rows": len(source_rows),
        "clean_rows": len(actual),
        "quoted_rows": sum(r["price"] is not None for r in actual),
        "missing_rows": sum(r["price"] is None for r in actual),
        "products": len({key[0] for key in city_stats}),
        "raw_aliases": len(alias_rows),
        "markets": panel,
        "districts": len(district_panels),
        "periods": len(calendar),
        "date_start": calendar[0],
        "date_end": as_of,
        "product_weekly_rows": len(weekly),
        "district_weekly_rows": len(district_weekly),
        "review_flagged_rows": len(expected_flags),
        "review_flags_by_type": dict(
            Counter(code for codes in expected_flags.values() for code in codes)
        ),
        "spec_change_events": len(changes),
        "eligible_forecast_series": sum(p["eligible_baseline"] for p in profiles.values()),
        "annual_seasonality_candidates": sum(
            p["annual_seasonality_candidate"] for p in profiles.values()
        ),
    }
    return checks, summary, weekly, list(profiles.values())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder = run_folder(root / "runtime/warehouse", args.run_id)
    catalog = folder / "warehouse.duckdb"
    if catalog.exists():
        raise FileExistsError("Verified catalog is immutable; use a new run ID")
    with duckdb.connect() as db:
        for table, path in TABLES.items():
            glob = (folder / path / "**/*.parquet").as_posix().replace("'", "''")
            db.execute(
                f"CREATE VIEW {table} AS SELECT * "
                f"FROM read_parquet('{glob}',hive_partitioning=true)"
            )
        checks, summary, weekly, profiles = verify(db, folder, root, args.source_dir)
        stage = json.loads((folder / "stage.json").read_text(encoding="utf-8"))
        report = {
            "passed": all(checks.values()),
            "run_id": args.run_id,
            "pipeline_version": "day2-v1",
            "completed_at": datetime.now(UTC).isoformat(),
            "source_file_sha256": stage["source_file_sha256"],
            "alias_file_sha256": stage["alias_file_sha256"],
            "policy_file_sha256": stage["policy_file_sha256"],
            "market_file_sha256": stage["market_file_sha256"],
            "summary": summary,
            "checks": checks,
            "note": "质量提示保留原价；未补价、插值或训练预测模型。候选清单仅对本次截止期有效。",
        }
        write_json(folder / "verification.json", report)
        evidence = root / "artifacts/day2" / args.run_id
        write_json(evidence / "verification.json", report)
        if not report["passed"]:
            raise AssertionError(
                f"Verification failed: {[key for key, value in checks.items() if not value]}"
            )
        with duckdb.connect(str(catalog)) as disk:
            for table in TABLES:
                arrow = db.sql(f"SELECT * FROM {table}").to_arrow_table()
                disk.register("source_table", arrow)
                disk.execute(f"CREATE TABLE {table} AS SELECT * FROM source_table")
                disk.unregister("source_table")
            disk.execute(
                "CREATE TABLE build_metadata AS SELECT ? AS run_id, ? AS source_file_sha256",
                [args.run_id, stage["source_file_sha256"]],
            )
        csv_export(
            evidence / "forecast_profiles.csv",
            sorted(
                profiles,
                key=lambda r: (not r["eligible_baseline"], r["product_name"], r["product_spec"]),
            ),
        )
        csv_export(
            evidence / "latest_product_weekly.csv",
            sorted(
                [r for r in weekly if str(r["quote_date"]) == str(summary["date_end"])],
                key=lambda r: (r["category"], r["product_name"], r["product_spec"]),
            ),
        )
        csv_export(
            evidence / "review_flags.csv",
            records(
                db,
                """SELECT source_row_number,quote_date,product_name,product_spec,unit,district,
                market,price,previous_price,market_wow_change,peer_median_ratio,review_flags
                FROM review_flags ORDER BY quote_date,product_name,market""",
            ),
        )
        csv_export(
            evidence / "product_aliases.csv",
            records(db, "SELECT * FROM product_aliases ORDER BY product_name,product_spec_raw"),
        )
        publish_snapshot(
            root / "runtime",
            args.run_id,
            stage["source_file_sha256"],
            stage.get("expected_parent_run_id"),
        )
        print(json.dumps(report, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
