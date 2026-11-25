"""Missing/stale models must not be represented as fresh prices."""

import hashlib


def test_no_forecast_for_untrained_dataset_and_no_history_mutation(query_client, query_runtime):
    path = query_runtime / "warehouse/runs/test-snapshot/warehouse.duckdb"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    response = query_client.get("/api/v1/forecasts/catalog")
    assert response.status_code == 200
    assert response.json()["status"] == "not_ready"
    assert response.json()["items"] == []
    product = query_client.get("/api/v1/products").json()["items"][0]["product_id"]
    response = query_client.get("/api/v1/forecasts", params={"product_id": product})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "forecast_not_ready"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_forecast_paths_reject_traversal_and_unknown_dataset(query_client):
    assert (
        query_client.get("/api/v1/forecasts/catalog", params={"run_id": "../escape"}).status_code
        == 422
    )
    assert (
        query_client.get("/api/v1/forecasts/catalog", params={"run_id": "absent-run"}).status_code
        == 404
    )
