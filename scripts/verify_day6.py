"""Independent real-quote rule reconciliation, review persistence and source protection."""

import csv
import hashlib
import io
import json
import os
import statistics
import subprocess
import time
from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx

from market_prices.alerts import derive_events, rules
from market_prices.curation import write_json
from market_prices.warehouse import open_snapshot

ROOT = Path(__file__).resolve().parents[1]


def cross_snapshot_identity():
    """Use the genuine Day-5 reimport fixture if retained; never edit either snapshot."""
    isolated = ROOT / "runtime/day5-acceptance"
    if not (isolated / "warehouse/current.json").exists():
        return None
    with open_snapshot() as current:
        source_run = current.meta.run_id
        source_events = derive_events(current, current.meta.as_of, current.meta.as_of, rules())
    previous = os.environ.get("MARKET_RUNTIME_DIR")
    try:
        os.environ["MARKET_RUNTIME_DIR"] = str(isolated)
        with open_snapshot() as imported:
            comparison_run = imported.meta.run_id
            imported_events = derive_events(
                imported, imported.meta.as_of, imported.meta.as_of, rules()
            )
    finally:
        if previous is None:
            os.environ.pop("MARKET_RUNTIME_DIR", None)
        else:
            os.environ["MARKET_RUNTIME_DIR"] = previous
    return (
        source_run != comparison_run
        and {e["id"] for e in source_events} == {e["id"] for e in imported_events}
        and {e["source_record_id"] for e in source_events}
        != {e["source_record_id"] for e in imported_events}
    )


def main():
    pointer = ROOT / "runtime/warehouse/current.json"
    before_pointer = pointer.read_bytes()
    current = json.loads(before_pointer)
    database = ROOT / "runtime/warehouse" / current["relative_path"] / "warehouse.duckdb"
    before_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    with open_snapshot() as snapshot:
        meta = snapshot.meta.model_dump(mode="json")
        quotes = snapshot.rows("""SELECT product_id,quote_date,market_id,district,price,price_raw,
            source_record_id,source_row_number FROM quotes""")
        markets = snapshot.rows("SELECT * FROM markets")
    index = {(r["product_id"], r["quote_date"], r["market_id"]): r for r in quotes}
    weeks = defaultdict(list)
    for row in quotes:
        weeks[row["product_id"], row["quote_date"]].append(row)
    expected = []
    for (pid, day), group in weeks.items():
        prices = [row["price"] for row in group if row["price"] is not None]
        median = statistics.median(prices) if prices else None
        for row in group:
            price = row["price"]
            previous = index.get((pid, day - timedelta(days=7), row["market_id"]))
            if price is None:
                expected.append(("source_no_quote", pid, str(day), row["market_id"]))
            else:
                if previous and previous["price"] is not None and previous["price"] > 0:
                    if abs(price - previous["price"]) >= previous["price"] * Decimal("0.3"):
                        expected.append(("large_weekly_change", pid, str(day), row["market_id"]))
                if median and price >= median * 2:
                    expected.append(("high_vs_peer_median", pid, str(day), row["market_id"]))
                if median and price <= median / 2:
                    expected.append(("low_vs_peer_median", pid, str(day), row["market_id"]))
        if len(prices) < len(markets) * Decimal("0.8"):
            expected.append(("low_market_coverage", pid, str(day), None))
        observed = {row["market_id"] for row in group}
        expected.extend(
            ("market_not_observed", pid, str(day), market["market_id"])
            for market in markets
            if market["market_id"] not in observed
        )
    checks, all_events = {}, []
    with httpx.Client(base_url="http://127.0.0.1:18880", timeout=60, trust_env=False) as client:
        params = {
            "run_id": meta["run_id"],
            "date_from": meta["date_start"],
            "date_to": meta["as_of"],
            "page_size": 100,
        }
        first = client.get("/api/v1/alerts", params=params).json()
        for page in range(1, (first["total"] + 99) // 100 + 1):
            response = client.get("/api/v1/alerts", params={**params, "page": page})
            response.raise_for_status()
            all_events.extend(response.json()["items"])
        actual = [(e["rule"], e["product_id"], e["quote_date"], e["market_id"]) for e in all_events]
        checks["all_rule_hits_match_independent_decimal_and_market_panel_oracle"] = Counter(
            actual
        ) == Counter(expected)
        checks["pagination_has_no_duplicate_event_ids"] = len({e["id"] for e in all_events}) == len(
            all_events
        )
        original_by_source = {q["source_record_id"]: q for q in quotes}
        checks["source_record_numbers_and_raw_prices_preserved"] = all(
            e["source_record_id"] is None
            or e["price_raw"] == original_by_source[e["source_record_id"]]["price_raw"]
            and e["source_row_number"]
            == original_by_source[e["source_record_id"]]["source_row_number"]
            for e in all_events
        )
        checks["null_prices_not_relabelled_as_zero"] = all(
            e["price_raw"] == "-" and e["evidence"]["price"] is None
            for e in all_events
            if e["rule"] == "source_no_quote"
        )
        exported = client.get("/api/v1/alerts/export.csv", params=params)
        csv_rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
        checks["unpaginated_export_matches_all_filtered_events"] = len(csv_rows) == len(
            all_events
        ) and {r["event_id"] for r in csv_rows} == {e["id"] for e in all_events}
        latest = client.get("/api/v1/alerts").json()
        checks["latest_week_defaults_to_actual_source_date"] = (
            latest["date_from"] == latest["date_to"] == meta["as_of"] and latest["total"] == 3
        )
        district = next(e["district"] for e in all_events if e["district"])
        scoped = client.get("/api/v1/alerts", params={**params, "district": district}).json()
        checks["district_filter_does_not_include_city_coverage_events"] = scoped["total"] == len(
            [e for e in all_events if e["district"] == district]
        ) and all(e["scope"] == "market" for e in scoped["items"])
        checks["invalid_filters_rejected"] = all(
            client.get("/api/v1/alerts", params=p).status_code == code
            for p, code in [
                ({"page": 0}, 422),
                ({"rule": "wrong"}, 422),
                ({"product_id": "unknown"}, 404),
                ({"date_from": "2026-11-25", "date_to": "2026-11-24"}, 422),
            ]
        )
        event = latest["items"][0]
        detail_url = f"/api/v1/alerts/{event['id']}"
        query = {"run_id": meta["run_id"], "quote_date": event["quote_date"]}
        original = client.get(detail_url, params=query).json()
        review = original["event"]["review"]
        payload = {
            **query,
            "policy_sha256": latest["rules"]["policy_sha256"],
            "reviewed": True,
            "note": "第六天验收：已核对真实来源与规则依据，保留原价",
            "expected_version": review["version"],
        }
        applied = client.patch(detail_url + "/review", json=payload)
        applied.raise_for_status()
        first_review = applied.json()["review"]
        repeat = client.patch(detail_url + "/review", json=payload).json()
        checks["same_review_submission_is_idempotent"] = repeat["review"] == first_review
        conflict = client.patch(
            detail_url + "/review", json={**payload, "note": "过期版本不应覆盖"}
        )
        checks["stale_note_edit_returns_conflict"] = conflict.status_code == 409
        subprocess.run(
            ["docker", "compose", "restart", "api"], cwd=ROOT, check=True, capture_output=True
        )
        for _attempt in range(60):
            try:
                response = client.get(detail_url, params=query)
                if response.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(1)
        after_restart = response.json()
        checks["review_survives_actual_api_restart"] = (
            after_restart["event"]["review"] == first_review
        )
        checks["review_history_retains_source_evidence"] = any(
            h["note"] == payload["note"]
            and h["event"]["source_record_id"] == event["source_record_id"]
            for h in after_restart["history"]
        )
        restored = client.patch(
            detail_url + "/review",
            json={
                **payload,
                "reviewed": review["reviewed"],
                "note": review["note"],
                "expected_version": first_review["version"],
            },
        )
        restored.raise_for_status()
        checks["review_state_restored_after_acceptance"] = (
            restored.json()["review"]["reviewed"] == review["reviewed"]
            and restored.json()["review"]["note"] == review["note"]
        )
        status = client.get("/api/v1/system/status").json()
        checks["runtime_status_reports_real_services_and_matching_model"] = (
            status["kafka"]["online"]
            and status["stream"]["online"]
            and status["forecast_service"]["online"]
            and status["dataset"]["meta"]["run_id"] == meta["run_id"]
            and status["current_model"]["as_of"] == meta["as_of"]
        )
        checks["cadence_uses_business_date_without_claiming_new_publication"] = (
            status["source_cadence"]["next_expected_week"] == "2026-12-01"
            and status["source_cadence"]["basis"]
            == "weekly_source_dates_not_a_claim_of_website_publication"
        )
    checks["original_warehouse_and_pointer_unchanged"] = (
        pointer.read_bytes() == before_pointer
        and hashlib.sha256(database.read_bytes()).hexdigest() == before_hash
    )
    same_findings = cross_snapshot_identity()
    if same_findings is not None:
        checks["real_reimport_snapshot_keeps_same_finding_ids_despite_new_source_records"] = (
            same_findings
        )
    result = {
        "passed": all(checks.values()),
        "run_id": meta["run_id"],
        "latest_source_week": meta["as_of"],
        "history_events": len(all_events),
        "latest_week_events": latest["total"],
        "rule_counts": dict(Counter(e["rule"] for e in all_events)),
        "checks": checks,
        "note": "Review restored; actual acceptance actions retained in separate history.",
    }
    write_json(ROOT / "artifacts/day6/verification.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    assert result["passed"], checks


if __name__ == "__main__":
    main()
