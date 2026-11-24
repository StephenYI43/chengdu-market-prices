"""Day-one API skeleton. Analytical endpoints are documented, not fabricated."""

import json
import os
from pathlib import Path

from confluent_kafka.admin import AdminClient
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from market_prices.contracts import QuoteEvent, Readiness

app = FastAPI(
    title="成都农贸市场价格平台",
    version="0.1.0",
    description="第一天：运行状态、数据契约和真实样例链路验证。分析与预测接口待后续实现。",
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
