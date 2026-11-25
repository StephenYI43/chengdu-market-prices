"""Historical analysis API and same-origin local web application."""

import json
import os
from pathlib import Path

from confluent_kafka.admin import AdminClient
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from market_prices.alert_api import router as alert_router
from market_prices.contracts import QuoteEvent, Readiness
from market_prices.forecast_api import router as forecast_router
from market_prices.import_api import router as import_router
from market_prices.query_api import router
from market_prices.system_api import router as system_router
from market_prices.warehouse import DataError

app = FastAPI(
    title="成都农贸市场价格平台",
    version="0.6.0",
    description="真实价格分析、文件增量导入、持续处理、周度预测，以及可追溯的预警与复核。",
)

app.include_router(router)
app.include_router(import_router)
app.include_router(forecast_router)
app.include_router(alert_router)
app.include_router(system_router)


@app.exception_handler(DataError)
def data_error(_request, error: DataError):
    return JSONResponse(
        status_code=error.status, content={"detail": {"code": error.code, "message": error.message}}
    )


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "alive", "version": app.version}


@app.get("/health/ready", response_model=Readiness)
def ready():
    try:
        metadata = AdminClient(
            {"bootstrap.servers": os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092")}
        ).list_topics(timeout=5)
        if not metadata.brokers:
            raise RuntimeError("No Kafka brokers returned")
        return Readiness(status="ready", kafka=True)
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content=Readiness(status="not_ready", kafka=False, detail=str(exc)).model_dump(),
        )


@app.get("/api/v1/metadata")
def metadata() -> dict:
    path = Path(__file__).resolve().parents[2] / "data/samples/manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


@app.post("/api/v1/contracts/validate-quote", response_model=QuoteEvent)
def validate_quote(event: QuoteEvent) -> QuoteEvent:
    """Validate only; this endpoint does not publish or insert records."""
    return event


web_directory = Path(
    os.getenv("MARKET_WEB_DIR", Path(__file__).resolve().parents[2] / "frontend/dist")
)
if web_directory.is_dir():
    app.mount("/", StaticFiles(directory=web_directory, html=True), name="web")
