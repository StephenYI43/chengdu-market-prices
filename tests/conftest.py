"""API fixtures use an unchanged real quote week, inside isolated pytest directories."""

import csv
import hashlib
import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from market_prices.api import app
from market_prices.contracts import normalized_spec, stable_id
from market_prices.curation import SOURCE_URL, entity_id, read_aliases, write_json

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def query_template(tmp_path_factory):
    root = tmp_path_factory.mktemp("real-query-template")
    folder = root / "warehouse/runs/test-snapshot"
    folder.mkdir(parents=True)
    source = ROOT / "data/samples/latest_week.csv"
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    aliases = {
        (r["product_name_raw"], r["product_spec_raw"], r["unit_raw"]): r
        for r in read_aliases(ROOT / "config/product-aliases.csv")
    }
    rows = []
    with source.open(encoding="utf-8-sig", newline="") as file:
        for number, row in enumerate(csv.DictReader(file), start=1):
            alias = aliases[(row["商品名称"], row["产品规格"], row["计量单位"])]
            rows.append(
                {
                    "quote_date": date.fromisoformat(row["日期"]),
                    "product_id": alias["product_id"],
                    "product_name": alias["product_name"],
                    "product_spec": alias["product_spec"],
                    "product_spec_raw": row["产品规格"],
                    "unit": row["计量单位"],
                    "category": alias["category"],
                    "district": row["区"],
                    "district_id": entity_id("district", row["区"]),
                    "market": row["市场"],
                    "market_id": entity_id("market", row["区"], row["市场"]),
                    "price": None if row["价格"] == "-" else Decimal(row["价格"]),
                    "price_raw": row["价格"],
                    "quote_status": "missing" if row["价格"] == "-" else "quoted",
                    "missing_reason": "source_no_quote" if row["价格"] == "-" else None,
                    "source_row_number": number,
                    "source_filename": source.name,
                    "source_record_id": stable_id([sha, str(number)]),
                    "business_key": stable_id(
                        [
                            row["日期"],
                            row["商品名称"],
                            normalized_spec(row["产品规格"]),
                            row["计量单位"],
                            row["区"],
                            row["市场"],
                        ]
                    ),
                }
            )
    with duckdb.connect(str(folder / "warehouse.duckdb")) as db:
        db.register("original", pa.Table.from_pylist(rows))
        db.execute(
            "CREATE TABLE quotes AS SELECT * EXCLUDE(price),"
            "price::DECIMAL(12,4) price,[]::VARCHAR[] review_flags FROM original"
        )
        db.execute("""CREATE TABLE products AS SELECT product_id,product_name,product_spec,unit,
            category,min(quote_date) first_observed_week,max(quote_date) last_observed_week,
            count(DISTINCT quote_date) observed_weeks FROM quotes GROUP BY ALL""")
        db.execute(
            "CREATE TABLE markets AS SELECT DISTINCT "
            "market_id,district_id,district,market FROM quotes"
        )
        db.execute("CREATE TABLE districts AS SELECT DISTINCT district_id,district FROM quotes")
        db.execute("CREATE TABLE calendar AS SELECT DISTINCT quote_date FROM quotes")
        db.execute(
            "CREATE TABLE forecast_profiles AS SELECT "
            "product_id,false eligible_baseline FROM products"
        )
        for name, group, panel in [
            ("product_weekly", "quote_date,product_id", 18),
            ("district_weekly", "quote_date,product_id,district", 3),
        ]:
            db.execute(f"""CREATE TABLE {name} AS SELECT {group},count(*) observed_market_count,
                count(price) quoted_market_count,count(*)-count(price) missing_market_count,
                {panel} expected_market_count,count(price)/{panel} market_coverage,
                round(avg(price),4)::DECIMAL(16,4) mean_price,
                median(price)::DECIMAL(16,4) median_price,min(price) min_price,max(price) max_price,
                max(price)-min(price) price_range,count(price)>1 ranking_available,
                NULL::DOUBLE common_market_wow_change,0 common_market_count
                FROM quotes GROUP BY {group}""")
        db.execute(
            "CREATE TABLE build_metadata AS SELECT 'test-snapshot' run_id,? source_file_sha256",
            [sha],
        )
    write_json(
        folder / "verification.json",
        {
            "passed": True,
            "run_id": "test-snapshot",
            "source_file_sha256": sha,
            "summary": {
                "source_rows": len(rows),
                "quoted_rows": sum(r["price"] is not None for r in rows),
                "missing_rows": sum(r["price"] is None for r in rows),
                "products": len({r["product_id"] for r in rows}),
                "markets": 18,
                "districts": 6,
                "periods": 1,
                "review_flagged_rows": 0,
                "eligible_forecast_series": 0,
                "date_start": "2026-11-24",
                "date_end": "2026-11-24",
            },
        },
    )
    write_json(folder / "stage.json", {"source_filename": source.name, "source_url": SOURCE_URL})
    write_json(
        root / "warehouse/current.json",
        {"run_id": "test-snapshot", "relative_path": "runs/test-snapshot", "verified": True},
    )
    return root


@pytest.fixture
def query_runtime(query_template, tmp_path, monkeypatch):
    shutil.copytree(query_template / "warehouse", tmp_path / "warehouse")
    monkeypatch.setenv("MARKET_RUNTIME_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def query_client(query_runtime):
    with TestClient(app) as client:
        yield client
