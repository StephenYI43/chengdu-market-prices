"""Observable local services and source business dates, without invented live prices."""

import os
from collections import Counter
from datetime import datetime, timedelta, timezone

from confluent_kafka.admin import AdminClient
from fastapi import APIRouter

from market_prices.forecast_store import bundle_for, service_status
from market_prices.job_store import JobStore, public_job
from market_prices.warehouse import DataError, open_snapshot

router = APIRouter(prefix="/api/v1/system", tags=["运行状态"])


@router.get("/status")
def system_status():
    checked = datetime.now(timezone(timedelta(hours=8)))
    dataset, model, errors = None, None, []
    try:
        with open_snapshot() as snapshot:
            dataset = {
                "meta": snapshot.meta.model_dump(mode="json"),
                "totals": snapshot.report["summary"],
            }
        try:
            bundle = bundle_for(dataset["meta"]["run_id"])
            if bundle:
                model = {
                    key: bundle[0][key]
                    for key in ["model_run_id", "as_of", "ready_products", "created_at"]
                }
        except DataError as error:
            errors.append(error.message)
    except DataError as error:
        errors.append(error.message)
    kafka = {"online": False, "brokers": 0}
    try:
        metadata = AdminClient(
            {"bootstrap.servers": os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092")}
        ).list_topics(timeout=3)
        kafka = {"online": bool(metadata.brokers), "brokers": len(metadata.brokers)}
    except Exception:
        kafka["message"] = "暂时无法连接Kafka"
    jobs = JobStore()
    recent = jobs.jobs(20)
    cadence = None
    if dataset:
        as_of = datetime.fromisoformat(dataset["meta"]["as_of"]).date()
        next_week = as_of + timedelta(days=7)
        cadence = {
            "as_of": as_of.isoformat(),
            "next_expected_week": next_week.isoformat(),
            "age_days": (checked.date() - as_of).days,
            "may_need_update": checked.date() > next_week,
            "basis": "weekly_source_dates_not_a_claim_of_website_publication",
        }
    return {
        "checked_at": checked.isoformat(),
        "kafka": kafka,
        "stream": jobs.worker_status(),
        "forecast_service": service_status(),
        "dataset": dataset,
        "current_model": model,
        "source_cadence": cadence,
        "recent_job_counts": dict(Counter(j["status"] for j in recent)),
        "recent_jobs": [public_job(j) for j in recent[:5]],
        "issues": errors,
    }
