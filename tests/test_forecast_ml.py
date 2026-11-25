"""Optional native-ML causality test against unchanged real weekly observations."""

import csv
import json
from pathlib import Path

import pytest

pytest.importorskip("lightgbm")
import lightgbm as lgb
import numpy as np

from market_prices.forecasting import as_date, features, fit_predict, series_index


def test_native_model_ignores_future_prices_and_can_be_reloaded(tmp_path):
    root = Path(__file__).resolve().parents[1]
    with (root / "data/samples/weekly_means.csv").open(encoding="utf-8-sig", newline="") as file:
        weekly = [
            {
                **r,
                "mean_price": float(r["mean_price"]) if r["mean_price"] else None,
                "quoted_market_count": int(r["quoted_market_count"]),
                "expected_market_count": int(r["expected_market_count"]),
            }
            for r in csv.DictReader(file)
        ]
    policy = json.loads((root / "config/forecast-policy.json").read_text())
    eligibility = json.loads((root / "config/curation-policy.json").read_text())["forecast"]
    cutoff = as_date("2026-08-11")
    changed = [
        {**r, "mean_price": 10000} if as_date(r["quote_date"]) > cutoff else r for r in weekly
    ]
    index = series_index(weekly, 0.8)
    predictions, audit, model, codes, profiles = fit_predict(
        weekly, index, cutoff, eligibility, policy
    )
    altered = fit_predict(changed, series_index(changed, 0.8), cutoff, eligibility, policy)
    assert predictions == altered[0]
    assert audit == altered[1] and profiles == altered[4]
    model.booster_.save_model(str(tmp_path / "model.txt"))
    restored = lgb.Booster(model_file=str(tmp_path / "model.txt"))
    pid = next(iter(codes))
    vector = np.array([features(index[pid], cutoff, 1, codes[pid])])
    assert np.allclose(restored.predict(vector), model.booster_.predict(vector), atol=0, rtol=0)
