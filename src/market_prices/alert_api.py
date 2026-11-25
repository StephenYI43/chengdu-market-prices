"""Pinned quality review queries, source evidence, CSV and reversible review actions."""

import csv
import io
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Response
from pydantic import BaseModel, Field

from market_prices.alert_store import ReviewStore
from market_prices.alerts import (
    derive_events,
    event_summary,
    rules,
    select_events,
    validate_window,
)
from market_prices.query_api import Data
from market_prices.warehouse import DataError, open_snapshot

router = APIRouter(prefix="/api/v1/alerts", tags=["预警与复核"])
Rule = Literal[
    "large_weekly_change",
    "high_vs_peer_median",
    "low_vs_peer_median",
    "source_no_quote",
    "market_not_observed",
    "low_market_coverage",
]
ReviewStatus = Literal["open", "reviewed"]
Identifier = Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")]


def scoped(
    data,
    date_from=None,
    date_to=None,
    product_id=None,
    district=None,
    market_id=None,
    rule=None,
    status=None,
):
    start, end = validate_window(data, date_from, date_to)
    if product_id:
        data.product(product_id)
    data.scope(district, market_id)
    config = rules()
    events = select_events(
        derive_events(data, start, end, config), product_id, district, market_id, rule
    )
    events = ReviewStore().decorate(events)
    summary = event_summary(events)
    if status:
        events = [e for e in events if e["review"]["reviewed"] == (status == "reviewed")]
    return events, {
        "meta": data.meta,
        "rules": config,
        "date_from": start,
        "date_to": end,
        "summary": summary,
    }


@router.get("")
def alerts(
    data: Data,
    date_from: date | None = None,
    date_to: date | None = None,
    product_id: Annotated[str | None, Query(max_length=96)] = None,
    district: Annotated[str | None, Query(max_length=40)] = None,
    market_id: Annotated[str | None, Query(max_length=96)] = None,
    rule: Rule | None = None,
    status: ReviewStatus | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    events, context = scoped(
        data, date_from, date_to, product_id, district, market_id, rule, status
    )
    return {
        **context,
        "total": len(events),
        "page": page,
        "page_size": page_size,
        "items": events[(page - 1) * page_size : page * page_size],
    }


@router.get("/export.csv")
def export_alerts(
    data: Data,
    date_from: date | None = None,
    date_to: date | None = None,
    product_id: Annotated[str | None, Query(max_length=96)] = None,
    district: Annotated[str | None, Query(max_length=40)] = None,
    market_id: Annotated[str | None, Query(max_length=96)] = None,
    rule: Rule | None = None,
    status: ReviewStatus | None = None,
):
    events, context = scoped(
        data, date_from, date_to, product_id, district, market_id, rule, status
    )
    output = io.StringIO(newline="")
    fields = [
        "event_id",
        "dataset_run_id",
        "rule",
        "rule_name",
        "quote_date",
        "product_name",
        "product_spec",
        "unit",
        "district",
        "market",
        "price_raw",
        "scope",
        "reviewed",
        "review_note",
        "source_filename",
        "source_row_number",
        "source_record_id",
        "evidence",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    import json

    for event in events:
        row = {key: event.get(key) for key in fields}
        row.update(
            event_id=event["id"],
            dataset_run_id=context["meta"].run_id,
            reviewed=event["review"]["reviewed"],
            review_note=event["review"]["note"],
            evidence=json.dumps(event["evidence"], ensure_ascii=False),
        )
        # Notes are free text; prevent spreadsheet applications from executing them as formulas.
        row = {
            key: "'" + value
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "@"))
            else value
            for key, value in row.items()
        }
        if isinstance(row["review_note"], str) and row["review_note"].lstrip().startswith("-"):
            row["review_note"] = "'" + row["review_note"]
        writer.writerow(row)
    return Response(
        "\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="price-review-events.csv"'},
    )


def find_event(data, event_id, quote_date):
    config = rules()
    found = next(
        (e for e in derive_events(data, quote_date, quote_date, config) if e["id"] == event_id),
        None,
    )
    if not found:
        raise DataError(404, "alert_not_found", "该报价期未找到这条预警，请刷新列表")
    return found, config


@router.get("/{event_id}")
def alert_detail(data: Data, event_id: Identifier, quote_date: date):
    event, config = find_event(data, event_id, quote_date)
    store = ReviewStore()
    return {
        "meta": data.meta,
        "rules": config,
        "event": store.decorate([event])[0],
        "history": store.history(event_id),
    }


class ReviewAction(BaseModel):
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
    quote_date: date
    policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewed: bool
    note: str = Field(default="", max_length=500)
    expected_version: int = Field(ge=0)


@router.patch("/{event_id}/review")
def review_event(event_id: Identifier, action: ReviewAction):
    with open_snapshot(action.run_id) as snapshot:
        config = rules()
        if action.policy_sha256 != config["policy_sha256"]:
            raise DataError(409, "rules_changed", "规则版本已变化，请刷新后复核")
        event, _config = find_event(snapshot, event_id, action.quote_date)
        review = ReviewStore().update(
            event,
            snapshot.meta.run_id,
            action.reviewed,
            action.note.strip(),
            action.expected_version,
        )
        return {"event_id": event_id, "review": review}
