from fastapi.testclient import TestClient

from market_prices.api import app


def test_liveness_and_openapi_contract():
    with TestClient(app) as client:
        assert client.get("/health/live").json()["status"] == "alive"
        spec = client.get("/openapi.json").json()
        request_schema = spec["paths"]["/api/v1/contracts/validate-quote"]["post"]["requestBody"][
            "content"
        ]["application/json"]["schema"]
        component = request_schema["$ref"].rsplit("/", 1)[-1]
        assert "price" in spec["components"]["schemas"][component]["properties"]
        assert "/api/v1/contracts/validate-quote" in spec["paths"]
        assert "/api/v1/forecasts" in spec["paths"]
        assert "/api/v1/forecasts/catalog" in spec["paths"]
