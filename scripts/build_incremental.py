"""Rebuild a verified immutable snapshot from accepted raw records, retaining every old quote."""

import hashlib
from collections import defaultdict
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from batch_day2 import process
from verify_day2 import TABLES, aggregate, close, records, scalar

from market_prices.curation import (
    RAW_FIELDS,
    SOURCE_URL,
    read_aliases,
    run_folder,
    write_json,
)
from market_prices.import_records import config_hashes
from market_prices.job_store import now, publish_snapshot


def build_snapshot(
    spark, runtime, raw_table, run_id, parent_run_id, store=None, job_id=None, publish=True
):
    project = Path(__file__).resolve().parents[1]
    folder = run_folder(Path(runtime) / "warehouse", run_id)
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "ods").mkdir()
    pq.write_table(raw_table, folder / "ods/quotes_raw.parquet", compression="zstd")
    sources = {}
    for row in raw_table.select(
        ["source_file_sha256", "source_filename", "source_url"]
    ).to_pylist():
        key = row["source_file_sha256"]
        sources.setdefault(key, {**row, "accepted_records": 0})["accepted_records"] += 1
    manifest = {
        "parent_run_id": parent_run_id,
        "source_files": sorted(sources.values(), key=lambda r: r["source_file_sha256"]),
        "accepted_records": len(raw_table),
        "scope": "accepted_real_source_records",
    }
    write_json(folder / "source-manifest.json", manifest)
    digest = hashlib.sha256((folder / "source-manifest.json").read_bytes()).hexdigest()
    stage = {
        "run_id": run_id,
        "pipeline_version": "day4-v1",
        "started_at": now(),
        "source_file_sha256": digest,
        "source_filename": "source-manifest.json",
        "source_kind": "manifest",
        "source_files_count": len(sources),
        "source_url": SOURCE_URL,
        "parent_run_id": parent_run_id,
        "source_rows": len(raw_table),
        **config_hashes(project / "config"),
    }
    write_json(folder / "stage.json", stage)
    try:
        process(spark, folder, project)
    finally:
        spark.catalog.clearCache()
    if store and job_id:
        store.update_job(
            job_id, {"status": "verifying", "message": "正在核对历史保留、全部报价和周度汇总"}
        )
    with duckdb.connect() as db:
        for name, path in TABLES.items():
            glob = (folder / path / "**/*.parquet").as_posix().replace("'", "''")
            db.execute(
                f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{glob}',hive_partitioning=true)"
            )
        db.register("input_raw", raw_table)
        db.register(
            "alias_input",
            pa.Table.from_pylist(read_aliases(project / "config/product-aliases.csv")),
        )
        db.execute("""CREATE VIEW expected AS SELECT r.*,a.product_id,a.product_name,a.product_spec,
            a.unit,a.category,try_cast(r.quote_date_raw AS DATE) quote_date,
            CASE WHEN r.price_raw='-' THEN NULL ELSE r.price_raw::DECIMAL(12,4) END price,
            CASE WHEN r.price_raw='-' THEN 'missing' ELSE 'quoted' END quote_status
            FROM input_raw r LEFT JOIN alias_input a
            USING(product_name_raw,product_spec_raw,unit_raw)""")
        compare_fields = [
            *RAW_FIELDS,
            "source_row_number",
            "source_file_sha256",
            "source_filename",
            "source_url",
            "business_key",
            "event_id",
            "product_id",
            "product_name",
            "product_spec",
            "unit",
            "quote_date",
            "price",
            "quote_status",
        ]
        differing = " OR ".join(f"e.{field} IS DISTINCT FROM q.{field}" for field in compare_fields)
        checks = {
            "all_raw_records_retained": scalar(db, "SELECT count(*) FROM raw_quotes")
            == len(raw_table),
            "all_original_and_new_quote_fields_match": scalar(
                db,
                f"""SELECT count(*) FROM expected e
                FULL OUTER JOIN quotes q USING(source_record_id) WHERE e.source_record_id IS NULL
                OR q.source_record_id IS NULL OR {differing}""",
            )
            == 0,
            "unique_business_and_source_record_keys": scalar(
                db,
                "SELECT count(*)=count(DISTINCT business_key) "
                "AND count(*)=count(DISTINCT source_record_id) FROM quotes",
            ),
            "no_rejected_or_conflicting_records": scalar(
                db, "SELECT count(*) FROM quarantined_quotes"
            )
            == 0
            and scalar(db, "SELECT count(*) FROM duplicate_quotes") == 0,
            "no_unmapped_specifications": scalar(
                db, "SELECT count(*) FROM expected WHERE product_id IS NULL"
            )
            == 0,
        }
        expected_rows = records(db, "SELECT * FROM expected")
        city, district = defaultdict(list), defaultdict(list)
        for row in expected_rows:
            city[(row["product_id"], row["quote_date"])].append(row)
            district[(row["product_id"], row["quote_date"], row["district_raw"])].append(row)

        def aggregates_match(actual, groups, group_fields):
            if len(actual) != len(groups):
                return False
            for row in actual:
                key = tuple(row[field] for field in group_fields)
                if key not in groups:
                    return False
                expected = aggregate(groups[key])
                if any(
                    not close(row[k], v) if k == "stddev_price" else row[k] != v
                    for k, v in expected.items()
                ):
                    return False
            return True

        checks["all_city_aggregates_match_independent_decimal_calculation"] = aggregates_match(
            records(db, "SELECT * FROM product_weekly"), city, ["product_id", "quote_date"]
        )
        checks["all_district_aggregates_match_independent_decimal_calculation"] = aggregates_match(
            records(db, "SELECT * FROM district_weekly"),
            district,
            ["product_id", "quote_date", "district"],
        )
        checks["null_quotes_preserved"] = scalar(
            db, "SELECT count(*) FROM quotes WHERE price IS NULL"
        ) == sum(r["price_raw"] == "-" for r in expected_rows)
        dates = sorted({r["quote_date"] for r in expected_rows})
        checks["weekly_calendar_preserved"] = all(
            (b - a).days == 7 for a, b in zip(dates, dates[1:])
        )
        checks["market_foreign_keys_valid"] = (
            scalar(
                db,
                "SELECT count(*) FROM quotes q LEFT JOIN markets m USING(market_id) "
                "WHERE m.market_id IS NULL",
            )
            == 0
        )
        summary = {
            "source_rows": len(expected_rows),
            "clean_rows": scalar(db, "SELECT count(*) FROM quotes"),
            "quoted_rows": scalar(db, "SELECT count(price) FROM quotes"),
            "missing_rows": scalar(db, "SELECT count(*) FROM quotes WHERE price IS NULL"),
            "products": scalar(db, "SELECT count(*) FROM products"),
            "raw_aliases": scalar(db, "SELECT count(*) FROM product_aliases"),
            "markets": scalar(db, "SELECT count(*) FROM markets"),
            "districts": scalar(db, "SELECT count(*) FROM districts"),
            "periods": len(dates),
            "date_start": dates[0],
            "date_end": dates[-1],
            "product_weekly_rows": len(city),
            "district_weekly_rows": len(district),
            "review_flagged_rows": scalar(db, "SELECT count(*) FROM review_flags"),
            "eligible_forecast_series": scalar(
                db, "SELECT count(*) FROM forecast_profiles WHERE eligible_baseline"
            ),
            "source_files_count": len(sources),
        }
        report = {
            "passed": all(checks.values()),
            "run_id": run_id,
            "pipeline_version": "day4-v1",
            "completed_at": now(),
            "parent_run_id": parent_run_id,
            "source_file_sha256": digest,
            "summary": summary,
            "checks": checks,
            **config_hashes(project / "config"),
        }
        write_json(folder / "verification.json", report)
        if not report["passed"]:
            raise AssertionError(
                f"Incremental reconciliation failed: {[k for k, v in checks.items() if not v]}"
            )
        with duckdb.connect(str(folder / "warehouse.duckdb")) as disk:
            for table in TABLES:
                disk.register("source_rows", db.sql(f"SELECT * FROM {table}").to_arrow_table())
                disk.execute(f"CREATE TABLE {table} AS SELECT * FROM source_rows")
                disk.unregister("source_rows")
            disk.execute(
                "CREATE TABLE build_metadata AS SELECT ? run_id,? source_file_sha256",
                [run_id, digest],
            )
    if publish:
        publish_snapshot(runtime, run_id, digest, parent_run_id)
    return report
