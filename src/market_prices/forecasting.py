"""Direct weekly forecasts with as-of selection, purged validation and rolling evaluation."""

import math
import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta

from market_prices.curation import forecast_profiles

MODELS = {"naive": "沿用上周价", "mean4": "近4周均价", "lightgbm": "LightGBM"}
FEATURES = [
    "product_code",
    "horizon",
    "week_sin",
    "week_cos",
    "lag1_ratio",
    "lag2_ratio",
    "lag4_ratio",
    "lag8_ratio",
    "lag13_ratio",
    "mean4_ratio",
    "mean13_ratio",
    "std4_ratio",
    "coverage",
]


def as_date(value):
    return date.fromisoformat(str(value))


def split_origins(dates, policy):
    horizon = policy["horizon_weeks"]
    test = dates[-horizon - policy["test_origins"] : -horizon]
    if not test:
        raise ValueError("Not enough real weekly history for holdout evaluation")
    first_test = dates.index(test[0])
    # Validation's LAST target must already be known at the FIRST test origin.
    last_validation = first_test - horizon
    validation = dates[last_validation - policy["validation_origins"] + 1 : last_validation + 1]
    if len(validation) != policy["validation_origins"] or validation[0] < dates[51]:
        raise ValueError("需要更多真实历史：至少52周训练期及独立验证、回测区间")
    assert validation[-1] + timedelta(weeks=horizon) <= test[0]
    return validation, test


def series_index(weekly, coverage):
    series = defaultdict(dict)
    for row in weekly:
        day = as_date(row["quote_date"])
        if day in series[row["product_id"]]:
            raise ValueError("Duplicate product/week")
        price = row["mean_price"]
        adequate = (
            price is not None
            and float(price) > 0
            and row["quoted_market_count"] >= math.ceil(row["expected_market_count"] * coverage)
        )
        series[row["product_id"]][day] = (
            float(price) if adequate else None,
            row["quoted_market_count"] / row["expected_market_count"],
        )
    return dict(series)


def features(series, origin, horizon, product_code):
    values = [series.get(origin - timedelta(weeks=i), (None, 0))[0] for i in range(13)]
    if any(value is None or value <= 0 for value in values):
        return None
    last = values[0]
    target = origin + timedelta(weeks=horizon)
    angle = 2 * math.pi * target.isocalendar().week / 52.1775
    return [
        product_code,
        horizon,
        math.sin(angle),
        math.cos(angle),
        *[values[i] / last for i in [0, 1, 3, 7, 12]],
        statistics.mean(values[:4]) / last,
        statistics.mean(values) / last,
        statistics.pstdev(values[:4]) / last,
        series[origin][1],
    ]


def training_samples(index, codes, cutoff, horizon):
    x, y, audit = [], [], []
    for product_id, code in codes.items():
        series = index[product_id]
        for origin in sorted(series):
            if origin > cutoff:
                continue
            for step in range(1, horizon + 1):
                target = origin + timedelta(weeks=step)
                actual = series.get(target, (None, 0))[0]
                if target > cutoff or actual is None:
                    continue
                vector = features(series, origin, step, code)
                if vector is not None:
                    x.append(vector)
                    y.append(actual / series[origin][0] - 1)
                    audit.append((origin, target))
    return x, y, audit


def fit_predict(weekly, index, cutoff, eligibility, policy):
    import lightgbm as lgb
    import numpy as np

    profiles = forecast_profiles(weekly, cutoff, eligibility)
    eligible = [r for r in profiles if r["eligible_baseline"]]
    codes = {r["product_id"]: n for n, r in enumerate(eligible)}
    x, y, samples = training_samples(index, codes, cutoff, policy["horizon_weeks"])
    if not x:
        raise ValueError("No eligible training samples at cutoff")
    model = lgb.LGBMRegressor(objective="regression_l1", **policy["lightgbm"])
    model.fit(np.array(x), np.array(y), feature_name=FEATURES, categorical_feature=[0])
    vectors, keys, rows = [], [], []
    for profile in eligible:
        pid = profile["product_id"]
        s = index[pid]
        last = s[cutoff][0]
        for step in range(1, policy["horizon_weeks"] + 1):
            vector = features(s, cutoff, step, codes[pid])
            if vector is None:
                continue
            vectors.append(vector)
            keys.append((pid, step, last))
    predicted = model.booster_.predict(np.array(vectors)) if vectors else []
    for (pid, step, last), change in zip(keys, predicted):
        target = cutoff + timedelta(weeks=step)
        predictions = {
            "naive": last,
            "mean4": statistics.mean(index[pid][cutoff - timedelta(weeks=i)][0] for i in range(4)),
            "lightgbm": max(0.0001, last * (1 + float(change))),
        }
        for name, value in predictions.items():
            rows.append(
                {
                    "product_id": pid,
                    "origin": cutoff.isoformat(),
                    "target_date": target.isoformat(),
                    "horizon": step,
                    "model": name,
                    "predicted": value,
                }
            )
    audit = {
        "origin": cutoff.isoformat(),
        "training_rows": len(samples),
        "max_train_feature_date": max(a for a, _ in samples).isoformat(),
        "max_train_target_date": max(b for _, b in samples).isoformat(),
        "eligible_products": len(eligible),
    }
    return rows, audit, model, codes, profiles


def metrics(rows):
    if not rows:
        return None
    errors = [r["predicted"] - r["actual"] for r in rows]
    return {
        "n": len(rows),
        "mae": statistics.mean(abs(e) for e in errors),
        "rmse": math.sqrt(statistics.mean(e * e for e in errors)),
        "mape": statistics.mean(abs(e) / r["actual"] for r, e in zip(rows, errors)),
        "wape": sum(abs(e) for e in errors) / sum(r["actual"] for r in rows),
    }


def choose_model(validation, minimum_origins, tolerance):
    scores = {name: metrics([r for r in validation if r["model"] == name]) for name in MODELS}
    if len({r["origin"] for r in validation}) < minimum_origins:
        return "naive", scores, "insufficient_validation"
    best = min(MODELS, key=lambda name: scores[name]["mae"])
    if scores["naive"]["mae"] <= scores[best]["mae"] * (1 + tolerance):
        best = "naive"
    return best, scores, "validation_mae"


def reference_error(rows, quantile, minimum):
    errors = sorted(abs(r["predicted"] - r["actual"]) for r in rows)
    if len(errors) < minimum:
        return None
    rank = min(len(errors), math.ceil((len(errors) + 1) * quantile))
    return errors[rank - 1]


def train_forecasts(weekly, products, eligibility, policy, progress=None):
    """Only weekly source observations enter training; results never modify that source."""
    dates = sorted({as_date(r["quote_date"]) for r in weekly})
    validation, test = split_origins(dates, policy)
    index = series_index(weekly, eligibility["min_market_coverage"])
    backtests, audits = [], []
    for split, origins in [("validation", validation), ("test", test)]:
        for n, origin in enumerate(origins, 1):
            if progress:
                progress(f"{split} {n}/{len(origins)} · {origin}")
            rows, audit, _, _, _ = fit_predict(weekly, index, origin, eligibility, policy)
            audits.append({**audit, "split": split})
            for row in rows:
                actual = index[row["product_id"]].get(as_date(row["target_date"]), (None, 0))[0]
                if actual is not None:
                    backtests.append({**row, "actual": actual, "split": split})
    future, audit, model, codes, profiles = fit_predict(
        weekly, index, dates[-1], eligibility, policy
    )
    audits.append({**audit, "split": "final"})
    by_product = {}
    for product in products:
        pid = product["product_id"]
        profile = next((r for r in profiles if r["product_id"] == pid), None)
        history = [
            {
                "quote_date": r["quote_date"],
                "mean_price": r["mean_price"],
                "quoted_market_count": r["quoted_market_count"],
            }
            for r in weekly
            if r["product_id"] == pid
        ][-26:]
        result = {
            "product": product,
            "profile": profile,
            "history": history,
            "status": "excluded",
            "forecasts": [],
            "evaluation": None,
        }
        candidates = [r for r in backtests if r["product_id"] == pid]
        val = [r for r in candidates if r["split"] == "validation"]
        champion, scores, reason = choose_model(
            val, policy["minimum_validation_origins"], policy["prefer_naive_within_ratio"]
        )
        if profile and profile["eligible_baseline"]:
            result.update(
                status="ready",
                selected_model=champion,
                selection_reason=reason,
                validation_scores=scores,
            )
            for row in future:
                if row["product_id"] != pid or row["model"] != champion:
                    continue
                radius = reference_error(
                    [r for r in val if r["model"] == champion and r["horizon"] == row["horizon"]],
                    policy["reference_band_quantile"],
                    policy["minimum_validation_origins"],
                )
                result["forecasts"].append(
                    {
                        "target_date": row["target_date"],
                        "horizon": row["horizon"],
                        "predicted": f"{row['predicted']:.4f}",
                        "lower": f"{max(0, row['predicted'] - radius):.4f}"
                        if radius is not None
                        else None,
                        "upper": f"{row['predicted'] + radius:.4f}" if radius is not None else None,
                        "reference_samples": len(
                            [
                                r
                                for r in val
                                if r["model"] == champion and r["horizon"] == row["horizon"]
                            ]
                        ),
                    }
                )
            evaluation = [r for r in candidates if r["split"] == "test"]
            result["evaluation"] = {
                "models": {
                    name: metrics([r for r in evaluation if r["model"] == name]) for name in MODELS
                },
                "selected_by_horizon": {
                    str(h): metrics(
                        [r for r in evaluation if r["model"] == champion and r["horizon"] == h]
                    )
                    for h in range(1, policy["horizon_weeks"] + 1)
                },
                "selected": metrics([r for r in evaluation if r["model"] == champion]),
                "rows": [r for r in evaluation if r["model"] == champion],
            }
        by_product[pid] = result
    ready = [r for r in by_product.values() if r["status"] == "ready"]
    selected_metrics = [r["evaluation"]["selected"] for r in ready if r["evaluation"]["selected"]]
    report = {
        "as_of": dates[-1].isoformat(),
        "horizon_weeks": policy["horizon_weeks"],
        "ready_products": len(ready),
        "excluded_products": len(products) - len(ready),
        "selection_counts": dict(Counter(r["selected_model"] for r in ready)),
        "macro_test_mape": statistics.mean(r["mape"] for r in selected_metrics)
        if selected_metrics
        else None,
        "validation_origins": [d.isoformat() for d in validation],
        "test_origins": [d.isoformat() for d in test],
        "selection_available_at": (
            validation[-1] + timedelta(weeks=policy["horizon_weeks"])
        ).isoformat(),
        "fold_audit": audits,
        "model_names": MODELS,
        "features": FEATURES,
        "policy": policy,
        "semantics": "predictions_of_weekly_equal_weight_market_mean_not_observed_quotes",
        "reference_band": "80% validation absolute-error reference; not guaranteed future coverage",
    }
    return report, by_product, backtests, model, codes
