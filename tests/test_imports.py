"""Real CSV fixtures; corrupted inputs below are validation tests, never imported prices."""

import csv
import hashlib
import io
import json
from pathlib import Path

import duckdb
import pytest

from market_prices.curation import RAW_COLUMNS
from market_prices.import_service import payload_digest, read_candidates
from market_prices.job_store import JobStore, publish_snapshot
from market_prices.warehouse import DataError

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/samples/latest_week.csv"


def csv_bytes(rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=RAW_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def original_rows():
    return list(csv.DictReader(io.StringIO(SOURCE.read_text(encoding="utf-8-sig"))))


def preview(client, content=None):
    return client.post(
        "/api/v1/imports/preview",
        files={"file": ("real.csv", SOURCE.read_bytes() if content is None else content)},
    )


def confirm(client, result):
    return client.post("/api/v1/imports", json={"preview_id": result["preview_id"]})


def test_all_real_duplicates_no_write_idempotent(query_client, query_runtime):
    pointer = (query_runtime / "warehouse/current.json").read_bytes()
    db_path = query_runtime / "warehouse/runs/test-snapshot/warehouse.duckdb"
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    result = preview(query_client).json()
    assert result["source_rows"] == 738
    assert result["counts"] == dict(
        new=0, existing_duplicate=738, file_duplicate=0, conflict=0, invalid=0
    )
    assert query_client.get("/api/v1/jobs").json()["items"] == []
    first = confirm(query_client, result)
    assert first.status_code == 202
    job = first.json()
    assert job["status"] == "no_op" and job["skipped_rows"] == 738
    assert "expected_digest" not in job
    assert confirm(query_client, result).json() == job
    assert len(query_client.get("/api/v1/jobs").json()["items"]) == 1
    assert (query_runtime / "warehouse/current.json").read_bytes() == pointer
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before


def test_real_holdout_queues_only_missing_row_and_replay_is_idempotent(query_client, query_runtime):
    with duckdb.connect(str(query_runtime / "warehouse/runs/test-snapshot/warehouse.duckdb")) as db:
        db.execute("DELETE FROM quotes WHERE source_row_number=1")
    result = preview(query_client).json()
    assert result["counts"]["new"] == 1
    assert result["counts"]["existing_duplicate"] == 737
    job = confirm(query_client, result).json()
    assert job["status"] == "queued" and job["expected_rows"] == 1
    store = JobStore()
    candidates = read_candidates(store, result["preview_id"])
    assert candidates[0]["price_raw"] == original_rows()[0]["价格"]
    claimed = store.claim_publish("test-owner")
    assert claimed["id"] == job["id"]
    assert store.claim_publish("test-owner") is None
    messages = [{"kind": "quote", "job_id": job["id"], "record": candidates[0]}]
    messages.append({"kind": "seal", "job_id": job["id"], "count": 1})
    store.ingest(messages)
    store.ingest(messages)
    assert store.job(job["id"])["received_rows"] == 1
    assert (
        payload_digest(store.received_records(job["id"])) == store.job(job["id"])["expected_digest"]
    )
    assert len(store.ready_jobs()) == 1
    store.update_job(job["id"], {"status": "building"})
    store.recover()
    assert store.job(job["id"])["status"] == "streaming"
    assert store.job(job["id"])["recovery_count"] == 1
    ids = [event["id"] for event in store.events()]
    assert ids == sorted(set(ids))
    assert all(event["id"] > ids[1] for event in store.events(ids[1]))


def test_duplicate_rows_count_and_conflict_blocks_entire_file(query_client):
    rows = original_rows()[:2]
    result = preview(query_client, csv_bytes(rows + rows[:1])).json()
    assert result["counts"]["existing_duplicate"] == 2
    assert result["counts"]["file_duplicate"] == 1
    rows[0]["价格"] = "999.1234"  # deliberately corrupt input, blocked before confirmation
    result = preview(query_client, csv_bytes(rows)).json()
    assert result["counts"]["conflict"] == 1
    assert not result["can_confirm"]
    assert confirm(query_client, result).status_code == 422
    assert query_client.get("/api/v1/jobs").json()["items"] == []


@pytest.mark.parametrize(
    "column,value",
    [
        ("日期", "2026-11-25"),
        ("商品名称", "未登记商品"),
        ("价格", "0"),
        ("价格", "NaN"),
        ("区", "未登记区"),
        ("计量单位", ""),
    ],
)
def test_invalid_record_blocks_confirmation(query_client, column, value):
    rows = original_rows()[:1]
    rows[0][column] = value
    response = preview(query_client, csv_bytes(rows))
    assert response.status_code == 200
    result = response.json()
    assert result["counts"]["invalid"] == 1 and not result["can_confirm"]
    assert confirm(query_client, result).status_code == 422


@pytest.mark.parametrize("content", [b"wrong,headers\na,b", b"", b"a,a\n1,2"])
def test_malformed_file_rejected(query_client, content):
    assert preview(query_client, content).status_code in {413, 422}


def test_formula_and_bad_extension_rejected(query_client):
    rows = original_rows()[:1]
    rows[0]["价格"] = "=1+1"
    assert preview(query_client, csv_bytes(rows)).status_code == 422
    response = query_client.post("/api/v1/imports/preview", files={"file": ("a.exe", b"x")})
    assert response.status_code == 415
    assert (
        query_client.get("/api/v1/stream/events", headers={"Last-Event-ID": "bad"}).status_code
        == 422
    )
    assert query_client.get("/api/v1/jobs/../../oops").status_code in {404, 422}


def test_upload_integrity_and_stale_preview(query_client, query_runtime):
    result = preview(query_client).json()
    upload = query_runtime / "imports/uploads" / result["preview_id"] / "original.csv"
    upload.write_bytes(upload.read_bytes() + b"\n")
    assert confirm(query_client, result).status_code == 409
    result = preview(query_client).json()
    path = query_runtime / "warehouse/current.json"
    current = json.loads(path.read_text())
    current["run_id"] = "other-run"
    path.write_text(json.dumps(current))
    assert confirm(query_client, result).status_code in {409, 503}


def test_publication_compare_parent_and_worker_lease(query_runtime):
    store = JobStore()
    assert store.worker_lease("first", "online")
    assert not store.worker_lease("second", "online")
    assert store.worker_status()["online"]
    with pytest.raises(DataError) as error:
        publish_snapshot(query_runtime, "stale", "hash", "wrong-parent")
    assert error.value.status == 409
    assert (
        json.loads((query_runtime / "warehouse/current.json").read_text())["run_id"]
        == "test-snapshot"
    )
