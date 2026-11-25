"""Unchanged real quote weeks; review writes only use isolated pytest runtimes."""

import csv
import hashlib
import io
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pyarrow as pa
import pytest

from market_prices.alerts import derive_events, rules
from market_prices.warehouse import Snapshot

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 24)


@pytest.fixture
def real_two_weeks():
    path = ROOT / "data/samples/review_two_weeks.csv"
    provenance = json.loads(
        (ROOT / "data/samples/review_two_weeks.provenance.json").read_text(encoding="utf-8")
    )
    assert hashlib.sha256(path.read_bytes()).hexdigest() == provenance["sample_sha256"]
    with path.open(encoding="utf-8-sig", newline="") as file:
        rows = [
            {
                **r,
                "quote_date": date.fromisoformat(r["quote_date"]),
                "price": Decimal(r["price"]) if r["price"] else None,
                "source_row_number": int(r["source_row_number"]),
            }
            for r in csv.DictReader(file)
        ]
    with duckdb.connect() as db:
        db.register("source", pa.Table.from_pylist(rows))
        db.execute("CREATE TABLE quotes AS SELECT * FROM source")
        db.execute(
            "CREATE TABLE products AS SELECT DISTINCT "
            "product_id,product_name,product_spec,unit FROM quotes"
        )
        db.execute("CREATE TABLE markets AS SELECT DISTINCT market_id,district,market FROM quotes")
        yield SimpleNamespace(
            rows=lambda sql, params: Snapshot.rows(SimpleNamespace(db=db), sql, params), db=db
        )


def test_real_latest_week_changes_use_prior_week_outside_filter(real_two_weeks):
    events = derive_events(real_two_weeks, DAY, DAY, rules())
    assert len(events) == 3
    changes = [e for e in events if e["rule"] == "large_weekly_change"]
    assert {e["product_name"] for e in changes} == {"黄瓜", "茄子"}
    assert (
        next(e for e in changes if e["product_name"] == "黄瓜")["evidence"]["change_ratio"] == 0.5
    )
    assert all(e["evidence"]["previous_quote_date"] == "2026-11-17" for e in changes)
    missing = next(e for e in events if e["rule"] == "source_no_quote")
    assert missing["price_raw"] == "-" and missing["evidence"]["price"] is None
    assert len({e["id"] for e in events}) == 3


def test_inclusive_threshold_and_gap_handling(real_two_weeks):
    config = {**rules(), "weekly_change_ratio": 0.5}
    events = derive_events(real_two_weeks, DAY, DAY, config)
    assert len([e for e in events if e["rule"] == "large_weekly_change"]) == 1
    events = derive_events(real_two_weeks, DAY, DAY, {**config, "weekly_change_ratio": 0.50001})
    assert [e["rule"] for e in events] == ["source_no_quote"]
    real_two_weeks.db.execute("DELETE FROM quotes WHERE quote_date=DATE '2026-11-17'")
    events = derive_events(real_two_weeks, DAY, DAY, rules())
    assert [e["rule"] for e in events] == ["source_no_quote"]


def test_market_gaps_do_not_invent_seasonal_quotes(real_two_weeks):
    db = real_two_weeks.db
    db.execute(
        "DELETE FROM quotes WHERE quote_date=DATE '2026-11-24' "
        "AND product_name='黄瓜' AND market='大学路室内农贸市场'"
    )
    db.execute("DELETE FROM quotes WHERE quote_date=DATE '2026-11-24' AND product_name='鸡蛋'")
    events = derive_events(real_two_weeks, DAY, DAY, rules())
    absent = [e for e in events if e["rule"] == "market_not_observed"]
    assert len(absent) == 1 and absent[0]["product_name"] == "黄瓜"
    assert absent[0]["source_record_id"] is None
    assert not any(e["product_name"] == "鸡蛋" for e in events)


def latest(client):
    response = client.get("/api/v1/alerts")
    assert response.status_code == 200
    return response.json()


def action(result, event, **extra):
    return {
        "run_id": result["meta"]["run_id"],
        "quote_date": event["quote_date"],
        "policy_sha256": result["rules"]["policy_sha256"],
        "reviewed": True,
        "note": "真实样例核对",
        "expected_version": 0,
        **extra,
    }


def test_review_idempotence_concurrency_undo_and_source_preservation(query_client, query_runtime):
    path = query_runtime / "warehouse/runs/test-snapshot/warehouse.duckdb"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = latest(query_client)
    assert result["total"] == 1
    event = result["items"][0]
    assert event["rule"] == "source_no_quote"
    url = f"/api/v1/alerts/{event['id']}/review"
    payload = action(result, event)
    first = query_client.patch(url, json=payload)
    assert first.status_code == 200 and first.json()["review"]["reviewed"] is True
    assert query_client.patch(url, json=payload).json() == first.json()
    detail_url = f"/api/v1/alerts/{event['id']}?quote_date={event['quote_date']}"
    detail = query_client.get(detail_url).json()
    assert len(detail["history"]) == 1
    assert detail["event"]["review"]["note"] == "真实样例核对"
    assert query_client.get("/api/v1/alerts?status=open").json()["total"] == 0
    assert query_client.get("/api/v1/alerts?status=reviewed").json()["total"] == 1
    assert query_client.patch(url, json=action(result, event, note="并发过期")).status_code == 409
    undone = query_client.patch(
        url, json=action(result, event, reviewed=False, note="", expected_version=1)
    )
    assert undone.status_code == 200
    assert len(query_client.get(detail_url).json()["history"]) == 2
    assert latest(query_client)["summary"]["open_events"] == 1
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_validation_and_csv_note_safety(query_client):
    result = latest(query_client)
    event = result["items"][0]
    for query in [
        "page=0",
        "page_size=101",
        "rule=unknown",
        "status=unknown",
        "date_from=2026-11-25&date_to=2026-11-24",
        "run_id=../escape",
    ]:
        assert query_client.get("/api/v1/alerts?" + query).status_code == 422
    assert query_client.get("/api/v1/alerts?product_id=unknown").status_code == 404
    assert query_client.get(f"/api/v1/alerts/{'0' * 64}?quote_date=2026-11-24").status_code == 404
    url = f"/api/v1/alerts/{event['id']}/review"
    assert (
        query_client.patch(url, json=action(result, event, policy_sha256="0" * 64)).status_code
        == 409
    )
    assert query_client.patch(url, json=action(result, event, note="x" * 501)).status_code == 422
    assert (
        query_client.patch(
            url, json=action(result, event, note='=HYPERLINK("example")')
        ).status_code
        == 200
    )
    response = query_client.get("/api/v1/alerts/export.csv")
    assert response.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert len(rows) == 1 and rows[0]["price_raw"] == "-"
    assert rows[0]["review_note"].startswith("'=HYPERLINK")


def test_stable_identity_excludes_provenance_and_includes_policy(real_two_weeks):
    events = derive_events(real_two_weeks, DAY, DAY, rules())
    real_two_weeks.db.execute("UPDATE quotes SET source_filename='copied-real-file.csv'")
    same = derive_events(real_two_weeks, DAY, DAY, rules())
    assert [e["id"] for e in same] == [e["id"] for e in events]
    changed = derive_events(real_two_weeks, DAY, DAY, {**rules(), "policy_sha256": "0" * 64})
    assert set(e["id"] for e in changed).isdisjoint(e["id"] for e in events)


def test_runtime_diagnostics_survive_unavailable_warehouse(query_client, tmp_path, monkeypatch):
    from market_prices import system_api

    monkeypatch.setenv("MARKET_RUNTIME_DIR", str(tmp_path / "unavailable"))
    monkeypatch.setattr(
        system_api,
        "AdminClient",
        lambda _config: SimpleNamespace(
            list_topics=lambda **_options: SimpleNamespace(brokers={1: "unit-test-protocol-stub"})
        ),
    )
    response = query_client.get("/api/v1/system/status")
    assert response.status_code == 200
    status = response.json()
    assert status["dataset"] is None and status["issues"]
    assert not status["stream"]["online"]
    assert status["checked_at"].endswith("+08:00")
