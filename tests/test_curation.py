import csv
import json
from datetime import date
from pathlib import Path

import pytest

from market_prices.curation import forecast_profiles, read_aliases, run_folder

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def weekly():
    path = ROOT / "data/samples/day2-weekly-excerpt.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def policy():
    return json.loads((ROOT / "config/curation-policy.json").read_text(encoding="utf-8"))[
        "forecast"
    ]


def test_aliases_merge_typography_and_keep_real_specifications():
    aliases = read_aliases(ROOT / "config/product-aliases.csv")
    assert len(aliases) == 53
    assert len({r["product_id"] for r in aliases}) == 44
    assert len({r["product_id"] for r in aliases if r["product_name"] == "大蒜"}) == 1
    assert len({r["product_id"] for r in aliases if r["product_name"] == "猪肉"}) == 4
    assert len({r["product_id"] for r in aliases if r["product_name"] == "调和油"}) == 2


def test_semantic_alias_merge_rejected(tmp_path):
    aliases = read_aliases(ROOT / "config/product-aliases.csv")
    oil = [r for r in aliases if r["product_name"] == "调和油"]
    invalid = {key: value for key, value in oil[0].items() if key != "product_id"}
    invalid["product_spec"] = oil[1]["product_spec"]
    path = tmp_path / "aliases.csv"
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(invalid))
        writer.writeheader()
        writer.writerow(invalid)
    with pytest.raises(ValueError, match="Semantic alias"):
        read_aliases(path)


@pytest.mark.parametrize("run_id", ["../escape", "..", "/absolute", "a/b", "a\\b", ""])
def test_run_paths_cannot_escape_warehouse(tmp_path, run_id):
    with pytest.raises(ValueError, match="run-id"):
        run_folder(tmp_path, run_id)


def test_52_week_boundary_uses_real_history(weekly, policy):
    rice = sorted((r for r in weekly if r["product_name"] == "籼米"), key=lambda r: r["quote_date"])
    before = forecast_profiles(rice, date.fromisoformat(rice[50]["quote_date"]), policy)[0]
    after = forecast_profiles(rice, date.fromisoformat(rice[51]["quote_date"]), policy)[0]
    assert before["adequate_weeks"] == 51 and not before["eligible_baseline"]
    assert after["adequate_weeks"] == 52 and after["eligible_baseline"]


def test_historical_cutoff_cannot_see_future_rows_or_new_products(weekly, policy):
    as_of = date(2025, 11, 18)
    prefix = [r for r in weekly if date.fromisoformat(r["quote_date"]) <= as_of]
    assert forecast_profiles(weekly, as_of, policy) == forecast_profiles(prefix, as_of, policy)
    assert all(r["product_name"] != "鲜牛肉" for r in forecast_profiles(weekly, as_of, policy))


def test_seasonal_gaps_and_seven_week_products_are_excluded(weekly, policy):
    profiles = forecast_profiles(weekly, date(2026, 11, 24), policy)
    seasonal = next(r for r in profiles if r["product_name"] == "空心菜")
    beef = next(r for r in profiles if r["product_name"] == "鲜牛肉")
    assert seasonal["observed_weeks"] == 60
    assert seasonal["recent_consecutive_adequate_weeks"] == 0
    assert seasonal["absent_weeks_since_first"] > 0
    assert not seasonal["eligible_baseline"]
    assert beef["observed_weeks"] == 7 and "short_history" in beef["exclusion_reasons"]
    oil = [r for r in profiles if r["product_name"] == "调和油"]
    assert sorted(r["observed_weeks"] for r in oil) == [7, 98]
    assert not any(r["eligible_baseline"] for r in oil)


def test_latest_market_coverage_boundary_is_15_of_18(weekly, policy):
    rice = [dict(r) for r in weekly if r["product_name"] == "籼米"]
    latest = max(rice, key=lambda r: r["quote_date"])
    latest["quoted_market_count"] = 15
    assert forecast_profiles(rice, date(2026, 11, 24), policy)[0]["eligible_baseline"]
    latest["quoted_market_count"] = 14
    result = forecast_profiles(rice, date(2026, 11, 24), policy)[0]
    assert not result["eligible_baseline"]
    assert "no_adequate_current_week" in result["exclusion_reasons"]


def test_annual_seasonality_requires_104_consecutive_weeks(weekly, policy):
    rice = sorted((r for r in weekly if r["product_name"] == "籼米"), key=lambda r: r["quote_date"])
    before = forecast_profiles(rice, date.fromisoformat(rice[102]["quote_date"]), policy)[0]
    after = forecast_profiles(rice, date.fromisoformat(rice[103]["quote_date"]), policy)[0]
    assert not before["annual_seasonality_candidate"]
    assert after["annual_seasonality_candidate"]


def test_duplicate_week_is_rejected(weekly, policy):
    with pytest.raises(ValueError, match="Duplicate product/week"):
        forecast_profiles([*weekly, weekly[0]], date(2026, 11, 24), policy)
