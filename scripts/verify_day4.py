"""Real-data acceptance against isolated :18882, including an actual worker restart.

Requires seed_day4_acceptance.py, then the acceptance Compose profile.
The production warehouse is only read and hashed, never modified.
"""

import csv
import hashlib
import io
import json
import subprocess
import time
from pathlib import Path

import duckdb
import httpx

from market_prices.curation import RAW_COLUMNS, RAW_FIELDS, write_json

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:18882"


def current_db(runtime):
    pointer = json.loads((runtime / "warehouse/current.json").read_text(encoding="utf-8"))
    return runtime / "warehouse" / pointer["relative_path"] / "warehouse.duckdb"


def signature(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    production = ROOT / "runtime"
    isolated = production / "day4-acceptance"
    artifact = ROOT / "artifacts/day4-acceptance"
    fixture = json.loads((artifact / "fixture.json").read_text(encoding="utf-8"))
    production_pointer = (production / "warehouse/current.json").read_bytes()
    production_db = current_db(production)
    original_hash = signature(production_db)
    baseline_db = current_db(isolated)
    baseline_hash = signature(baseline_db)
    report = {"fixture": fixture, "checks": {}, "observed_states": []}
    checks = report["checks"]
    checks["unpublished_rows_not_reported_as_inserted"] = True
    with httpx.Client(base_url=BASE, timeout=60, trust_env=False) as client:
        response = client.post(
            "/api/v1/imports/preview",
            files={
                "file": (
                    "real-history-holdout.csv",
                    (artifact / "real-history-holdout.csv").read_bytes(),
                )
            },
        )
        response.raise_for_status()
        preview = response.json()
        checks["only_unchanged_held_out_real_rows_are_new"] = (
            preview["counts"]["new"] == fixture["held_out_real_rows"]
            and preview["can_confirm"]
            and sum(preview["counts"].values()) == preview["source_rows"]
        )
        assert checks["only_unchanged_held_out_real_rows_are_new"], preview
        response = client.post("/api/v1/imports", json={"preview_id": preview["preview_id"]})
        response.raise_for_status()
        job = response.json()
        job_id = job["id"]
        report["job_id"] = job_id
        checks["confirmation_is_idempotent"] = (
            client.post("/api/v1/imports", json={"preview_id": preview["preview_id"]}).json()["id"]
            == job_id
        )
        restarted = False
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            job = client.get(f"/api/v1/jobs/{job_id}").json()
            if job["status"] in {"building", "verifying"}:
                checks["unpublished_rows_not_reported_as_inserted"] &= job["inserted_rows"] == 0
            if not report["observed_states"] or report["observed_states"][-1] != job["status"]:
                report["observed_states"].append(job["status"])
                print(f"Actual import state: {job['status']}", flush=True)
            if job["status"] in {"building", "verifying"} and not restarted:
                subprocess.run(
                    [
                        "docker",
                        "compose",
                        "--profile",
                        "acceptance",
                        "restart",
                        "stream-acceptance",
                    ],
                    cwd=ROOT,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                restarted = True
                print("Restarted the isolated worker during materialization", flush=True)
            if job["status"] in {"succeeded", "no_op", "failed"}:
                break
            time.sleep(2)
        report["completed_job"] = job
        checks["actual_worker_restart_recovered"] = restarted and job["recovery_count"] >= 1
        checks["stream_received_every_real_quote_once"] = (
            job["status"] == "succeeded"
            and job["received_rows"] == fixture["held_out_real_rows"]
            and job["inserted_rows"] == fixture["held_out_real_rows"]
        )
        assert checks["stream_received_every_real_quote_once"], job
        final_db = current_db(isolated)
        report["result_run_id"] = final_db.parent.name
        with duckdb.connect(str(production_db), read_only=True) as original:
            fields = ["business_key", *RAW_FIELDS, "product_id", "price", "quote_status"]
            expected = original.execute(
                f"SELECT {','.join(fields)} FROM quotes ORDER BY business_key"
            ).fetchall()
        with duckdb.connect(str(final_db), read_only=True) as final:
            actual = final.execute(
                f"SELECT {','.join(fields)} FROM quotes ORDER BY business_key"
            ).fetchall()
            checks["restored_full_real_business_ledger_exactly"] = actual == expected
            escaped_baseline = baseline_db.as_posix().replace("'", "''")
            final.execute(f"ATTACH '{escaped_baseline}' AS baseline (READ_ONLY)")
            checks["every_preexisting_raw_field_and_provenance_retained"] = (
                final.execute(
                    "SELECT count(*) FROM (SELECT * FROM baseline.raw_quotes "
                    "EXCEPT SELECT * FROM raw_quotes)"
                ).fetchone()[0]
                == 0
            )
            checks["one_row_per_business_key"] = final.execute(
                "SELECT count(*)=count(DISTINCT business_key) FROM quotes"
            ).fetchone()[0]
            report["final_counts"] = dict(
                zip(
                    ["rows", "prices", "missing"],
                    final.execute(
                        "SELECT count(*),count(price),count(*)-count(price) FROM quotes"
                    ).fetchone(),
                )
            )
        previous_run = final_db.parent.name
        repeat = client.post(
            "/api/v1/imports/preview",
            files={
                "file": (
                    "real-history-holdout.csv",
                    (artifact / "real-history-holdout.csv").read_bytes(),
                )
            },
        ).json()
        repeated = client.post("/api/v1/imports", json={"preview_id": repeat["preview_id"]}).json()
        checks["reimport_is_no_op_without_new_snapshot"] = (
            repeat["counts"]["new"] == 0
            and repeat["counts"]["existing_duplicate"] == 739
            and repeated["status"] == "no_op"
            and current_db(isolated).parent.name == previous_run
        )
        seq = client.get("/api/v1/imports/service").json()["last_event_id"]
        with client.stream(
            "GET", "/api/v1/stream/events?after=0", headers={"Last-Event-ID": str(seq - 1)}
        ) as stream:
            checks["sse_content_type"] = stream.headers["content-type"].startswith(
                "text/event-stream"
            )
            for line in stream.iter_lines():
                if line.startswith("id: "):
                    checks["sse_last_event_id_resumes_after_cursor"] = int(line[4:]) == seq
                    break
        # Deliberately damaged input must be rejected, never materialized as data.
        row = next(
            csv.DictReader(
                io.StringIO((artifact / "real-history-holdout.csv").read_text(encoding="utf-8-sig"))
            )
        )
        row["价格"] = "999.1234"
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=RAW_COLUMNS)
        writer.writeheader()
        writer.writerow(row)
        conflict = client.post(
            "/api/v1/imports/preview",
            files={"file": ("invalid-for-rejection.csv", output.getvalue().encode("utf-8-sig"))},
        ).json()
        rejected = client.post("/api/v1/imports", json={"preview_id": conflict["preview_id"]})
        checks["conflicting_price_blocked_without_overwrite"] = (
            conflict["counts"]["conflict"] == 1
            and rejected.status_code == 422
            and current_db(isolated).parent.name == previous_run
        )
        # The latest previously pinned baseline remains queryable after publication.
        old = client.get("/api/v1/overview", params={"run_id": baseline_db.parent.name})
        checks["old_snapshot_remains_readable"] = (
            old.status_code == 200
            and old.json()["totals"]["source_rows"] == fixture["baseline_rows"]
        )
    checks["production_pointer_unchanged"] = (
        production / "warehouse/current.json"
    ).read_bytes() == production_pointer
    checks["production_database_unchanged"] = signature(production_db) == original_hash
    checks["acceptance_parent_database_unchanged"] = signature(baseline_db) == baseline_hash
    verification = json.loads((final_db.parent / "verification.json").read_text(encoding="utf-8"))
    checks["independent_full_snapshot_checks_passed"] = verification["passed"]
    report["snapshot_checks"] = verification["checks"]
    report["passed"] = all(checks.values())
    write_json(artifact / "verification.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    assert report["passed"], checks


if __name__ == "__main__":
    main()
