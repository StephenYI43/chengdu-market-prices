"""Automatic real-data forecasting; all fitted artifacts and experiments stay local."""

import argparse
import csv
import fcntl
import json
import logging
import os
import threading
import time
import uuid
from datetime import UTC, datetime

from market_prices.forecast_store import (
    PROJECT,
    atomic_json,
    bundle_for,
    digest,
    fingerprint,
    root,
)
from market_prices.forecasting import train_forecasts
from market_prices.warehouse import open_snapshot

logger = logging.getLogger("price-forecast")


def state(status, **values):
    atomic_json(
        root() / "state.json",
        {"status": status, "updated_at": datetime.now(UTC).isoformat(), **values},
    )


def train_once(force=False):
    import mlflow

    with open_snapshot() as snapshot:
        dataset = snapshot.meta.model_dump(mode="json")
        source_run = dataset["run_id"]
        try:
            existing = bundle_for(source_run)
        except Exception:
            existing = None
        if existing and not force:
            return existing[0]
        weekly = snapshot.rows("""SELECT w.*,p.product_name,p.product_spec,p.unit
            FROM product_weekly w JOIN products p USING(product_id)
            ORDER BY quote_date,product_id""")
        products = snapshot.rows(
            "SELECT product_id,product_name,product_spec,unit,category FROM products "
            "ORDER BY category,product_name,product_spec"
        )
    pipeline_fingerprint = fingerprint()
    policy = json.loads((PROJECT / "config/forecast-policy.json").read_text())
    eligibility = json.loads((PROJECT / "config/curation-policy.json").read_text())["forecast"]
    model_run = "forecast-" + uuid.uuid4().hex
    folder = root() / "runs" / model_run
    folder.mkdir(parents=True)

    def progress(message):
        logger.info("%s %s", source_run, message)
        state("training", dataset_run_id=source_run, model_run_id=model_run, message=message)

    progress("读取真实周度序列，准备滚动训练")
    report, details, backtests, model, codes = train_forecasts(
        weekly, products, eligibility, policy, progress
    )
    import lightgbm

    report.update(
        dataset_run_id=source_run,
        model_run_id=model_run,
        dataset=dataset,
        created_at=datetime.now(UTC).isoformat(),
        pipeline_fingerprint=pipeline_fingerprint,
        lightgbm_version=lightgbm.__version__,
    )
    atomic_json(folder / "products.json", details)
    atomic_json(folder / "category-codes.json", codes)
    model.booster_.save_model(str(folder / "lightgbm.txt"))
    with (folder / "backtests.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(backtests[0]))
        writer.writeheader()
        writer.writerows(backtests)
    mlflow.set_tracking_uri(f"sqlite:///{(root() / 'mlflow.sqlite').as_posix()}")
    experiment_name = "chengdu-weekly-real-prices"
    existing_experiment = mlflow.get_experiment_by_name(experiment_name)
    experiment_id = (
        existing_experiment.experiment_id
        if existing_experiment
        else mlflow.create_experiment(
            experiment_name, artifact_location=(root() / "mlartifacts").as_uri()
        )
    )
    mlflow.set_experiment(experiment_id=experiment_id)
    with mlflow.start_run(run_name=model_run) as experiment:
        mlflow.log_params(
            {
                "dataset_run_id": source_run,
                "as_of": report["as_of"],
                "pipeline_fingerprint": pipeline_fingerprint,
                "horizon_weeks": policy["horizon_weeks"],
                **policy["lightgbm"],
            }
        )
        if report["macro_test_mape"] is not None:
            mlflow.log_metric("macro_test_mape", report["macro_test_mape"])
        mlflow.log_metrics(
            {
                "ready_products": report["ready_products"],
                "excluded_products": report["excluded_products"],
            }
        )
        # Plain text model avoids executable pickle loading in the API.
        mlflow.log_artifact(str(folder / "lightgbm.txt"))
        mlflow.log_artifact(str(folder / "backtests.csv"))
        mlflow.log_dict(policy, "forecast-policy.json")
        report["mlflow_run_id"] = experiment.info.run_id
    atomic_json(folder / "report.json", report)
    atomic_json(
        folder / "manifest.json",
        {
            "files": {
                name: digest(folder / name)
                for name in [
                    "report.json",
                    "products.json",
                    "category-codes.json",
                    "lightgbm.txt",
                    "backtests.csv",
                ]
            }
        },
    )
    if fingerprint() != pipeline_fingerprint:
        raise RuntimeError("训练期间配置或代码变化，未发布此模型")
    atomic_json(
        root() / "by-dataset" / f"{source_run}.json",
        {"model_run_id": model_run, "manifest_sha256": digest(folder / "manifest.json")},
    )
    state(
        "ready",
        dataset_run_id=source_run,
        model_run_id=model_run,
        message="模型训练与独立时段回测已完成",
        ready_products=report["ready_products"],
    )
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    root().mkdir(parents=True, exist_ok=True)
    with (root() / "worker.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop = threading.Event()

        def heartbeat():
            while not stop.is_set():
                atomic_json(
                    root() / "heartbeat.json", {"timestamp": time.time(), "pid": os.getpid()}
                )
                stop.wait(2)

        threading.Thread(target=heartbeat, daemon=True).start()
        try:
            while True:
                try:
                    report = train_once(args.force)
                    logger.info("Forecast ready: %s", report["model_run_id"])
                except Exception as error:
                    logger.exception("Forecast training failed")
                    state("failed", message="预测训练未完成", error=str(error)[:300])
                    if args.once:
                        raise
                if args.once:
                    return
                stop.wait(15)
        finally:
            stop.set()


if __name__ == "__main__":
    main()
