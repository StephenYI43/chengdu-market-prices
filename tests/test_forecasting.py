"""Regression checks use actual derived source weeks, never fabricated market records."""

import csv
import json
from datetime import timedelta
from pathlib import Path

import pytest

from market_prices.curation import forecast_profiles
from market_prices.forecasting import (
    as_date,
    choose_model,
    features,
    metrics,
    reference_error,
    series_index,
    split_origins,
    training_samples,
)

ROOT = Path(__file__).resolve().parents[1]
POLICY = json.loads((ROOT / "config/forecast-policy.json").read_text())
ELIGIBILITY = json.loads((ROOT / "config/curation-policy.json").read_text())["forecast"]


@pytest.fixture(scope="module")
def weekly_real():
    with (ROOT / "data/samples/weekly_means.csv").open(encoding="utf-8-sig", newline="") as file:
        return [
            {
                **row,
                "mean_price": float(row["mean_price"]) if row["mean_price"] else None,
                "quoted_market_count": int(row["quoted_market_count"]),
                "expected_market_count": int(row["expected_market_count"]),
            }
            for row in csv.DictReader(file)
        ]


def test_real_split_purges_all_future_validation_labels(weekly_real):
    dates = sorted({as_date(r["quote_date"]) for r in weekly_real})
    validation, test = split_origins(dates, POLICY)
    assert len(validation) == len(test) == 12
    assert validation[-1] + timedelta(weeks=4) <= test[0]
    assert test[-1] + timedelta(weeks=4) == dates[-1]
    assert set(validation).isdisjoint(test)


def test_future_price_changes_cannot_change_training_data_or_eligibility(weekly_real):
    dates = sorted({as_date(r["quote_date"]) for r in weekly_real})
    cutoff = dates[80]
    altered = [
        {**r, "mean_price": 10000} if as_date(r["quote_date"]) > cutoff else dict(r)
        for r in weekly_real
    ]
    profile = forecast_profiles(weekly_real, cutoff, ELIGIBILITY)
    assert forecast_profiles(altered, cutoff, ELIGIBILITY) == profile
    codes = {r["product_id"]: n for n, r in enumerate(profile) if r["eligible_baseline"]}
    samples = training_samples(series_index(weekly_real, 0.8), codes, cutoff, 4)
    changed_samples = training_samples(series_index(altered, 0.8), codes, cutoff, 4)
    assert samples == changed_samples
    assert samples[0] and all(origin < target <= cutoff for origin, target in samples[2])


def test_real_missing_week_does_not_become_zero_or_filled(weekly_real):
    index = series_index(weekly_real, 0.8)
    row = next(r for r in weekly_real if r["quoted_market_count"] < 15)
    day = as_date(row["quote_date"])
    assert index[row["product_id"]][day][0] is None
    assert features(index[row["product_id"]], day, 1, 0) is None


def test_actual_asof_shortlist_keeps_seasonal_and_short_history_excluded(weekly_real):
    profiles = forecast_profiles(weekly_real, as_date("2026-11-24"), ELIGIBILITY)
    assert len(profiles) == 44
    assert sum(p["eligible_baseline"] for p in profiles) == 37
    assert len([p for p in profiles if not p["eligible_baseline"]]) == 7


def test_naive_ties_and_empty_validation_are_conservative(weekly_real):
    rows = [r for r in weekly_real if r["mean_price"]]
    actual = rows[-1]["mean_price"]
    val = [
        {"origin": row["quote_date"], "actual": actual, "predicted": actual, "model": name}
        for row in rows[-12:]
        for name in ["naive", "mean4", "lightgbm"]
    ]
    # Repeated dates do not falsely count as independent validation origins.
    assert choose_model(val, 8, 0.02)[0] == "naive"
    assert choose_model([], 8, 0.02) == (
        "naive",
        {"naive": None, "mean4": None, "lightgbm": None},
        "insufficient_validation",
    )
    assert metrics([]) is None
    assert reference_error(val[:3], 0.8, 8) is None


def test_insufficient_history_is_rejected(weekly_real):
    dates = sorted({as_date(r["quote_date"]) for r in weekly_real})[:60]
    with pytest.raises(ValueError):
        split_origins(dates, POLICY)
