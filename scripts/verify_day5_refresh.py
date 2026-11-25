"""Import withheld REAL records into the isolated fixture and observe automatic retraining."""

import hashlib
import json
import time
from pathlib import Path

import httpx

from market_prices.curation import write_json

ROOT = Path(__file__).resolve().parents[1]


def wait_for(client, path, predicate, timeout=600):
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        response = client.get(path)
        response.raise_for_status()
        result = response.json()
        current = result.get("status")
        if current != previous:
            print(f"{path}: {current}", flush=True)
            previous = current
        if predicate(result):
            return result
        if current == "failed":
            raise RuntimeError(result)
        time.sleep(2)
    raise TimeoutError(path)


def main():
    pointer = ROOT / "runtime/warehouse/current.json"
    before = pointer.read_bytes()
    p = json.loads(before)
    db = ROOT / "runtime/warehouse" / p["relative_path"] / "warehouse.duckdb"
    before_hash = hashlib.sha256(db.read_bytes()).hexdigest()
    with httpx.Client(base_url="http://127.0.0.1:18882", timeout=60, trust_env=False) as client:
        baseline = wait_for(client, "/api/v1/forecasts/catalog", lambda r: r["status"] == "ready")
        assert baseline["report"]["as_of"] == "2026-11-17"
        pid = next(r["product"]["product_id"] for r in baseline["items"] if r["status"] == "ready")
        old_run = baseline["meta"]["run_id"]
        response = client.post(
            "/api/v1/imports/preview",
            files={
                "file": (
                    "real-history-holdout.csv",
                    (ROOT / "artifacts/day4-acceptance/real-history-holdout.csv").read_bytes(),
                )
            },
        )
        response.raise_for_status()
        preview = response.json()
        assert preview["counts"]["new"] == 739 and preview["can_confirm"]
        job = client.post("/api/v1/imports", json={"preview_id": preview["preview_id"]}).json()
        complete = wait_for(
            client, f"/api/v1/jobs/{job['id']}", lambda r: r["status"] == "succeeded"
        )
        waiting = client.get("/api/v1/forecasts/catalog").json()
        unavailable = client.get("/api/v1/forecasts", params={"product_id": pid})
        old = client.get("/api/v1/forecasts", params={"product_id": pid, "run_id": old_run}).json()
        final = wait_for(client, "/api/v1/forecasts/catalog", lambda r: r["status"] == "ready")
        stale = client.get(
            "/api/v1/forecasts",
            params={"product_id": pid, "model_run_id": baseline["report"]["model_run_id"]},
        )
        checks = {
            "real_739_rows_imported": complete["inserted_rows"] == 739,
            "new_dataset_waits_for_its_own_model": waiting["status"] == "not_ready"
            and unavailable.status_code == 503,
            "old_pinned_model_remains_readable": old["as_of"] == "2026-11-17"
            and old["meta"]["run_id"] == old_run,
            "automatic_retraining_without_manual_command": final["report"]["model_run_id"]
            != baseline["report"]["model_run_id"]
            and final["report"]["as_of"] == "2026-11-24",
            "new_model_bound_to_new_verified_dataset": final["report"]["dataset_run_id"]
            == complete["result_run_id"]
            and final["meta"]["run_id"] == complete["result_run_id"],
            "previous_model_cannot_be_passed_as_latest": stale.status_code == 409,
            "production_pointer_and_database_unchanged": pointer.read_bytes() == before
            and hashlib.sha256(db.read_bytes()).hexdigest() == before_hash,
        }
        report = {
            "passed": all(checks.values()),
            "checks": checks,
            "baseline_model": baseline["report"]["model_run_id"],
            "final_model": final["report"]["model_run_id"],
            "job_id": job["id"],
            "real_imported_rows": complete["inserted_rows"],
            "ready_products": final["report"]["ready_products"],
        }
        write_json(ROOT / "artifacts/day5/refresh-verification.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        assert report["passed"], checks


if __name__ == "__main__":
    main()
