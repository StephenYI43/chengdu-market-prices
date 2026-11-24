import csv
import hashlib
import json
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_real_sample_integrity_and_missing_semantics():
    folder = ROOT / "data/samples"
    metadata = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    sample = folder / "latest_week.csv"
    assert hashlib.sha256(sample.read_bytes()).hexdigest() == metadata["sample_file_sha256"]
    with sample.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == metadata["sample_rows"]
    assert {row["日期"] for row in rows} == {metadata["sample_quote_date"]}
    assert sum(row["价格"] == "-" for row in rows) == metadata["sample_missing"]
    assert all(Decimal(row["价格"]) > 0 for row in rows if row["价格"] != "-")
    assert len({tuple(row.values()) for row in rows}) == len(rows)
