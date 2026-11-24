"""Stage immutable source strings and row provenance; cross-check the supplied wide CSV."""

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from market_prices.contracts import normalized_spec, stable_id
from market_prices.curation import (
    RAW_COLUMNS,
    RAW_FIELDS,
    SOURCE_LONG,
    SOURCE_URL,
    SOURCE_WIDE,
    read_aliases,
    run_folder,
    write_json,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder = run_folder(root / "runtime/warehouse", args.run_id)
    folder.mkdir(parents=True, exist_ok=False)
    source, wide_source = args.source_dir / SOURCE_LONG, args.source_dir / SOURCE_WIDE
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    read_aliases(root / "config/product-aliases.csv")
    with source.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames != RAW_COLUMNS:
            raise ValueError(f"Unexpected long CSV headers: {reader.fieldnames}")
        rows = list(reader)
    raw_counter = Counter()
    staged = []
    for number, row in enumerate(rows, start=1):
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f"Malformed CSV record {number}")
        raw_counter[tuple(row[column] for column in RAW_COLUMNS)] += 1
        payload = dict(zip(RAW_FIELDS, (row[column] for column in RAW_COLUMNS)))
        payload.update(
            {
                "source_row_number": number,
                "source_record_id": stable_id([sha, str(number)]),
                "source_file_sha256": sha,
                "source_filename": source.name,
                "source_url": SOURCE_URL,
                "business_key": stable_id(
                    [
                        row["日期"].strip(),
                        row["商品名称"].strip(),
                        normalized_spec(row["产品规格"]),
                        row["计量单位"].strip(),
                        row["区"].strip(),
                        row["市场"].strip(),
                    ]
                ),
                "event_id": stable_id(
                    [
                        sha,
                        stable_id(
                            [
                                row["日期"].strip(),
                                row["商品名称"].strip(),
                                normalized_spec(row["产品规格"]),
                                row["计量单位"].strip(),
                                row["区"].strip(),
                                row["市场"].strip(),
                            ]
                        ),
                        row["价格"].strip(),
                    ]
                ),
            }
        )
        staged.append(payload)
    wide_counter = Counter()
    with wide_source.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        headers = reader.fieldnames
        if not headers or headers[:4] != RAW_COLUMNS[:4] or len(headers) != len(set(headers)):
            raise ValueError("Unexpected wide CSV headers")
        wide_rows = 0
        for row in reader:
            wide_rows += 1
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Malformed wide CSV record {wide_rows}")
            for column in headers[4:]:
                district, market = column.split("·", 1)
                wide_counter[
                    tuple(row[c] for c in RAW_COLUMNS[:4]) + (district, market, row[column])
                ] += 1
    if raw_counter != wide_counter:
        raise ValueError("Long and wide CSV values or multiplicities disagree")
    if not staged:
        raise ValueError("Source contains no records")
    (folder / "ods").mkdir()
    pq.write_table(
        pa.Table.from_pylist(staged), folder / "ods/quotes_raw.parquet", compression="zstd"
    )
    info = {
        "run_id": args.run_id,
        "pipeline_version": "day2-v1",
        "started_at": datetime.now(UTC).isoformat(),
        "source_file_sha256": sha,
        "wide_file_sha256": hashlib.sha256(wide_source.read_bytes()).hexdigest(),
        "alias_file_sha256": hashlib.sha256(
            (root / "config/product-aliases.csv").read_bytes()
        ).hexdigest(),
        "policy_file_sha256": hashlib.sha256(
            (root / "config/curation-policy.json").read_bytes()
        ).hexdigest(),
        "market_file_sha256": hashlib.sha256(
            (root / "config/markets.csv").read_bytes()
        ).hexdigest(),
        "source_filename": source.name,
        "source_rows": len(staged),
        "source_missing_rows": sum(row["价格"].strip() == "-" for row in rows),
        "wide_rows": wide_rows,
        "wide_long_exact_match": True,
        "source_url": SOURCE_URL,
    }
    write_json(folder / "stage.json", info)
    print(json.dumps(info, ensure_ascii=False))


if __name__ == "__main__":
    main()
