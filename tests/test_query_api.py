import csv
import io
import json

import duckdb
import pytest

from market_prices.warehouse import open_snapshot


def rice_id(client):
    return client.get("/api/v1/overview").json()["default_product_id"]


def test_overview_reads_verified_real_week(query_client):
    response = query_client.get("/api/v1/overview")
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["run_id"] == "test-snapshot"
    assert body["meta"]["data_mode"] == "historical_snapshot"
    assert body["totals"]["source_rows"] == 738
    assert body["totals"]["quoted_rows"] == 737
    assert body["totals"]["missing_rows"] == 1
    assert body["quote_dates"] == ["2026-11-24"]


def test_catalog_search_and_market_scopes(query_client):
    products = query_client.get("/api/v1/products", params={"q": "猪肉"}).json()["items"]
    assert len(products) == 4
    assert {p["product_spec"] for p in products} == {"五花肉", "去骨一级腿肉", "大排", "精瘦肉"}
    assert (
        len(query_client.get("/api/v1/markets", params={"district": "金牛"}).json()["items"]) == 3
    )
    assert query_client.get("/api/v1/products", params={"q": "%' OR true --"}).json()["items"] == []


def test_price_pagination_is_stable_and_nonoverlapping(query_client):
    params = {"product_id": rice_id(query_client), "page_size": 5}
    first = query_client.get("/api/v1/prices", params=params).json()
    second = query_client.get("/api/v1/prices", params={**params, "page": 2}).json()
    assert first["total"] == 18 and len(first["items"]) == 5
    assert len(second["items"]) == 5
    assert not (
        {r["source_record_id"] for r in first["items"]}
        & {r["source_record_id"] for r in second["items"]}
    )


def test_missing_quote_is_null_and_original_dash_is_preserved(query_client):
    body = query_client.get(
        "/api/v1/prices", params={"product_id": rice_id(query_client), "quote_status": "missing"}
    ).json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["price"] is None and row["price_raw"] == "-"
    assert row["market"] == "抚琴综合市场" and row["source_row_number"] == 2
    assert row["missing_reason"] == "source_no_quote"


def test_decimal_prices_and_tied_ranks(query_client):
    body = query_client.get(
        "/api/v1/comparisons", params={"product_id": rice_id(query_client)}
    ).json()
    assert body["summary"]["mean_price"] == "2.8000"
    assert body["summary"]["quoted_market_count"] == 17
    assert body["summary"]["missing_market_count"] == 1
    assert len(body["items"]) == 18
    quoted = [r for r in body["items"] if r["price"] is not None]
    assert quoted[0]["price"] == "2.6000" and quoted[0]["rank"] == 1
    assert len({r["rank"] for r in quoted if r["price"] == "2.7000"}) == 1
    assert body["items"][-1]["rank"] is None


def test_district_and_single_market_scope(query_client):
    pid = rice_id(query_client)
    district = query_client.get(
        "/api/v1/comparisons", params={"product_id": pid, "district": "金牛"}
    ).json()
    assert district["summary"]["mean_price"] == "2.6500"
    assert district["summary"]["expected_market_count"] == 3
    market = next(r for r in district["items"] if r["price"] is not None)
    one = query_client.get(
        "/api/v1/comparisons", params={"product_id": pid, "market_id": market["market_id"]}
    ).json()
    assert one["summary"]["expected_market_count"] == 1
    assert one["summary"]["ranking_available"] is False
    assert one["items"][0]["rank"] is None


def test_date_without_records_does_not_create_zero_prices(query_client):
    body = query_client.get(
        "/api/v1/comparisons",
        params={"product_id": rice_id(query_client), "quote_date": "2026-11-17"},
    ).json()
    assert body["summary"]["mean_price"] is None
    assert body["summary"]["absent_market_count"] == 18
    assert body["summary"]["missing_market_count"] == 0
    assert all(r["price"] is None and r["quote_status"] == "not_observed" for r in body["items"])


def test_trends_respect_bounds_and_do_not_invent_previous_week(query_client):
    pid = rice_id(query_client)
    body = query_client.get("/api/v1/trends", params={"product_id": pid}).json()
    assert len(body["items"]) == 1
    assert body["items"][0]["mean_price"] == "2.8000"
    assert body["items"][0]["wow_change"] is None
    assert (
        query_client.get(
            "/api/v1/trends", params={"product_id": pid, "date_to": "2026-11-17"}
        ).json()["items"]
        == []
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"page": 0},
        {"page_size": 101},
        {"sort": "price; DROP TABLE quotes"},
        {"date_from": "bad-date"},
        {"date_from": "2026-11-25", "date_to": "2026-11-24"},
    ],
)
def test_invalid_query_returns_422(query_client, extra):
    response = query_client.get(
        "/api/v1/prices", params={"product_id": rice_id(query_client), **extra}
    )
    assert response.status_code == 422


def test_unknown_product_and_inconsistent_market_are_rejected(query_client):
    assert query_client.get("/api/v1/prices", params={"product_id": "unknown"}).status_code == 404
    market = query_client.get("/api/v1/markets", params={"district": "成华"}).json()["items"][0]
    mismatch = query_client.get(
        "/api/v1/comparisons",
        params={
            "product_id": rice_id(query_client),
            "district": "金牛",
            "market_id": market["market_id"],
        },
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["detail"]["code"] == "inconsistent_scope"


def test_export_matches_filter_and_keeps_missing_values(query_client):
    response = query_client.get(
        "/api/v1/prices/export.csv",
        params={"product_id": rice_id(query_client), "quote_status": "missing"},
    )
    assert response.status_code == 200 and response.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert len(rows) == 1 and rows[0]["原始价格"] == "-" and rows[0]["解析价格"] == ""
    assert response.headers["x-dataset-run-id"] == "test-snapshot"


def test_missing_warehouse_returns_actionable_503(query_client, query_runtime):
    (query_runtime / "warehouse/current.json").write_text("{}", encoding="utf-8")
    response = query_client.get("/api/v1/overview")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "warehouse_unavailable"
    assert query_client.get("/health/live").status_code == 200


def test_unverified_or_mismatched_snapshot_cannot_be_read(query_client, query_runtime):
    report = query_runtime / "warehouse/runs/test-snapshot/verification.json"
    value = json.loads(report.read_text(encoding="utf-8"))
    value["source_file_sha256"] = "0" * 64
    report.write_text(json.dumps(value), encoding="utf-8")
    assert query_client.get("/api/v1/overview").status_code == 503


def test_pinned_batch_does_not_follow_changed_current_pointer(query_client, query_runtime):
    pointer = query_runtime / "warehouse/current.json"
    pointer.write_text(
        json.dumps({"run_id": "absent", "relative_path": "runs/absent", "verified": True}),
        encoding="utf-8",
    )
    assert query_client.get("/api/v1/overview").status_code == 503
    response = query_client.get("/api/v1/overview", params={"run_id": "test-snapshot"})
    assert response.status_code == 200 and response.json()["meta"]["run_id"] == "test-snapshot"
    assert query_client.get("/api/v1/overview", params={"run_id": "absent"}).status_code == 404


def test_snapshot_paths_and_database_are_read_only(query_client, query_runtime):
    assert query_client.get("/api/v1/overview", params={"run_id": "../outside"}).status_code == 422
    with open_snapshot("test-snapshot") as snapshot:
        with pytest.raises(duckdb.Error):
            snapshot.db.execute("CREATE TABLE should_not_exist(value INTEGER)")
