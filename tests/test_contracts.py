from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from market_prices.contracts import QuoteEvent, normalized_spec


def quote(**overrides):
    payload = dict(
        event_id="a" * 64,
        business_key="b" * 64,
        batch_id="test",
        mode="historical_replay",
        quote_date="2026-11-24",
        product_name="籼米",
        product_spec="二级，汉中",
        unit="元/500克",
        district="金牛",
        market="抚琴综合市场",
        price_raw="-",
        price=None,
        quote_status="missing",
        source_url="https://cddrc.chengdu.gov.cn/",
        source_file_sha256="c" * 64,
        ingested_at=datetime.now(UTC),
    )
    return QuoteEvent(**(payload | overrides))


def test_missing_quote_remains_null():
    assert quote().model_dump(mode="json")["price"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"price": "0", "quote_status": "quoted"},
        {"price": "2.6", "quote_status": "missing"},
        {"price": None, "quote_status": "quoted"},
        {"unit": "元/公斤"},
        {"ingested_at": datetime(2026, 11, 24)},
    ],
)
def test_reject_ambiguous_or_invalid_records(changes):
    with pytest.raises(ValidationError):
        quote(**changes)


def test_typographical_cleanup_preserves_actual_grade_changes():
    assert normalized_spec("二级， 汉中") == normalized_spec("二级，汉中")
    assert normalized_spec("精瘦肉") != normalized_spec("五花肉")


def test_decimal_round_trip():
    event = quote(price="2.60", price_raw="2.60", quote_status="quoted")
    assert QuoteEvent.model_validate_json(event.model_dump_json()).price == event.price
