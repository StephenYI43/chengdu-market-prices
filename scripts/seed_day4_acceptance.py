"""Isolated acceptance fixture made only by withholding unchanged real historical rows.

Host: python scripts/seed_day4_acceptance.py --prepare
Container: spark-submit --master local[2] scripts/seed_day4_acceptance.py
Never edits the production warehouse or the original source files.
"""

import argparse
import csv
import json
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from market_prices.curation import RAW_COLUMNS, RAW_FIELDS, write_json


def prepare(project):
    runtime = project / "runtime"
    target = runtime / "day4-acceptance"
    if (target / "warehouse/current.json").exists():
        raise RuntimeError("An acceptance snapshot already exists; use that fixture")
    pointer = json.loads((runtime / "warehouse/current.json").read_text(encoding="utf-8"))
    source = runtime / "warehouse" / pointer["relative_path"] / "warehouse.duckdb"
    target.mkdir(parents=True, exist_ok=True)
    artifact = project / "artifacts/day4-acceptance"
    artifact.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(source), read_only=True) as db:
        latest = db.execute("SELECT max(quote_date_raw) FROM raw_quotes").fetchone()[0]
        older = db.execute(
            "SELECT source_record_id FROM raw_quotes WHERE quote_date_raw<? "
            "ORDER BY quote_date_raw,source_record_id LIMIT 1",
            [latest],
        ).fetchone()[0]
        condition = "quote_date_raw=? OR source_record_id=?"
        held = db.execute(
            f"SELECT * FROM raw_quotes WHERE {condition}", [latest, older]
        ).to_arrow_table()
        base = db.execute(
            f"SELECT * FROM raw_quotes WHERE NOT ({condition})", [latest, older]
        ).to_arrow_table()
    pq.write_table(base, target / "seed.parquet", compression="zstd")
    with (artifact / "real-history-holdout.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as file:
        writer = csv.DictWriter(file, fieldnames=RAW_COLUMNS)
        writer.writeheader()
        for row in held.to_pylist():
            writer.writerow(dict(zip(RAW_COLUMNS, (row[key] for key in RAW_FIELDS))))
    result = {
        "source_run_id": pointer["run_id"],
        "production_rows": len(base) + len(held),
        "baseline_rows": len(base),
        "held_out_real_rows": len(held),
        "latest_source_week": latest,
        "fixture_method": "withhold_latest_week_plus_one_older_real_record_without_value_changes",
    }
    write_json(artifact / "fixture.json", result)
    print(json.dumps(result, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--run-id", default="day4-real-holdout-base")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    if args.prepare:
        prepare(project)
        return
    from build_incremental import build_snapshot
    from pyspark.sql import SparkSession

    runtime = project / "runtime/day4-acceptance"
    spark = (
        SparkSession.builder.appName("day4-real-history-holdout")
        .config("spark.sql.shuffle.partitions", "2")
        .config("spark.sql.session.timeZone", "Asia/Shanghai")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("WARN")
    try:
        build_snapshot(spark, runtime, pq.read_table(runtime / "seed.parquet"), args.run_id, None)
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
