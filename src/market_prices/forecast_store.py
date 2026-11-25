"""Immutable forecast bundles, coherent dataset binding and a separate training heartbeat."""

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from market_prices.curation import run_folder
from market_prices.job_store import runtime_root
from market_prices.warehouse import DataError

PROJECT = Path(__file__).resolve().parents[2]


def root():
    return runtime_root() / "forecasts"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fingerprint():
    return hashlib.sha256(
        "".join(
            digest(PROJECT / name)
            for name in [
                "config/forecast-policy.json",
                "config/curation-policy.json",
                "src/market_prices/forecasting.py",
                "scripts/forecast_worker.py",
                "uv.lock",
            ]
        ).encode()
    ).hexdigest()


def atomic_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    os.replace(temporary, path)


def service_status():
    def read(name):
        try:
            return json.loads((root() / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    beat, state = read("heartbeat.json"), read("state.json")
    return {"online": time.time() - beat.get("timestamp", 0) < 30, **state}


def bundle_for(dataset_run_id):
    run_folder(root(), dataset_run_id)  # same traversal restrictions as warehouse IDs
    pointer_path = root() / "by-dataset" / f"{dataset_run_id}.json"
    if not pointer_path.exists():
        return None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        folder = run_folder(root(), pointer["model_run_id"])
        if digest(folder / "manifest.json") != pointer["manifest_sha256"]:
            raise ValueError("Forecast manifest mismatch")
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        for name, expected in manifest["files"].items():
            if Path(name).name != name or digest(folder / name) != expected:
                raise ValueError("Forecast artifact mismatch")
        report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
        if report["dataset_run_id"] != dataset_run_id:
            raise ValueError("Forecast dataset mismatch")
        if report["pipeline_fingerprint"] != fingerprint():
            return None
        products = json.loads((folder / "products.json").read_text(encoding="utf-8"))
        return report, products, folder
    except (OSError, ValueError, KeyError) as error:
        raise DataError(
            503, "forecast_artifact_invalid", "预测文件核对失败，等待服务重新训练"
        ) from error
