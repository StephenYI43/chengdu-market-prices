"""Independent metric, source binding, baseline and HTTP reconciliation of actual forecasts."""

import csv
import hashlib
import io
import json
import math
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import httpx

from market_prices.curation import write_json
from market_prices.forecast_store import bundle_for
from market_prices.warehouse import open_snapshot

ROOT = Path(__file__).resolve().parents[1]


def independent_score(rows):
    errors = [float(r["predicted"]) - float(r["actual"]) for r in rows]
    n = len(rows)
    return {
        "n": n,
        "mae": sum(abs(e) for e in errors) / n,
        "rmse": math.sqrt(sum(e * e for e in errors) / n),
        "mape": sum(abs(e) / float(r["actual"]) for e, r in zip(errors, rows)) / n,
        "wape": sum(abs(e) for e in errors) / sum(float(r["actual"]) for r in rows),
    }


def main():
    runtime = ROOT / "runtime"
    pointer = (runtime / "warehouse/current.json").read_bytes()
    with open_snapshot() as snapshot:
        dataset = snapshot.meta.run_id
        real = snapshot.rows("SELECT * FROM product_weekly ORDER BY quote_date")
        by_week = {(r["product_id"], str(r["quote_date"])): r for r in real}
    database = runtime / "warehouse/runs" / dataset / "warehouse.duckdb"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    report, products, folder = bundle_for(dataset)
    with (folder / "backtests.csv").open(encoding="utf-8-sig", newline="") as file:
        backtests = list(csv.DictReader(file))
    checks = {}
    checks["all_25_models_train_only_on_available_targets"] = len(
        report["fold_audit"]
    ) == 25 and all(
        r["max_train_feature_date"] <= r["origin"] and r["max_train_target_date"] <= r["origin"]
        for r in report["fold_audit"]
    )
    checks["selection_truth_available_before_first_holdout_forecast"] = report[
        "selection_available_at"
    ] <= min(report["test_origins"]) and date.fromisoformat(
        max(report["validation_origins"])
    ) + timedelta(weeks=4) <= date.fromisoformat(min(report["test_origins"]))
    checks["all_backtest_truths_equal_actual_adequate_market_means"] = all(
        math.isclose(
            float(row["actual"]),
            float(by_week[(row["product_id"], row["target_date"])]["mean_price"]),
            abs_tol=1e-9,
        )
        and by_week[(row["product_id"], row["target_date"])]["quoted_market_count"] >= 15
        for row in backtests
    )
    checks["every_forecast_after_cutoff_with_weekly_horizon"] = all(
        date.fromisoformat(row["target_date"])
        == date.fromisoformat(report["as_of"]) + timedelta(weeks=row["horizon"])
        for p in products.values()
        for row in p["forecasts"]
    )
    checks["no_forecasts_for_seven_excluded_specifications"] = len(
        [p for p in products.values() if p["status"] == "excluded"]
    ) == 7 and all(not p["forecasts"] for p in products.values() if p["status"] == "excluded")
    grouped = defaultdict(list)
    for row in backtests:
        grouped[row["product_id"], row["split"], row["model"]].append(row)
    scores_valid, selection_valid, baselines_valid, bands_valid = True, True, True, True
    macro, baseline_macro = [], []
    worst = []
    for pid, product in products.items():
        if product["status"] != "ready":
            continue
        scores = {}
        for model in report["model_names"]:
            val = independent_score(grouped[pid, "validation", model])
            test = independent_score(grouped[pid, "test", model])
            scores[model] = val
            for expected, actual in [
                (val, product["validation_scores"][model]),
                (test, product["evaluation"]["models"][model]),
            ]:
                scores_valid &= all(
                    math.isclose(expected[k], actual[k], abs_tol=1e-10) for k in expected
                )
        selected = min(scores, key=lambda m: scores[m]["mae"])
        if scores["naive"]["mae"] <= scores[selected]["mae"] * 1.02:
            selected = "naive"
        selection_valid &= selected == product["selected_model"]
        latest = date.fromisoformat(report["as_of"])
        last = float(by_week[pid, str(latest)]["mean_price"])
        moving = (
            sum(
                float(by_week[pid, str(latest - timedelta(weeks=i))]["mean_price"])
                for i in range(4)
            )
            / 4
        )
        for row in product["forecasts"]:
            if selected != "lightgbm":
                expected = last if selected == "naive" else moving
                baselines_valid &= abs(expected - float(row["predicted"])) <= 0.00005001
            errors = sorted(
                abs(float(r["predicted"]) - float(r["actual"]))
                for r in grouped[pid, "validation", selected]
                if int(r["horizon"]) == row["horizon"]
            )
            radius = errors[min(len(errors), math.ceil((len(errors) + 1) * 0.8)) - 1]
            # Published prices and endpoints are rounded independently to 4 decimals.
            bands_valid &= (
                abs((float(row["upper"]) - float(row["predicted"])) - radius) <= 0.0001001
            )
        selected_score = independent_score(grouped[pid, "test", selected])
        macro.append(selected_score["mape"])
        baseline_macro.append(product["evaluation"]["models"]["naive"]["mape"])
        worst.append(
            {
                "product": product["product"]["product_name"],
                "spec": product["product"]["product_spec"],
                "model": selected,
                "test_mape": selected_score["mape"],
            }
        )
    checks.update(
        all_validation_and_holdout_metrics_independently_recomputed=bool(scores_valid),
        selected_models_use_validation_only=bool(selection_valid),
        all_baseline_future_values_recomputed_from_real_history=bool(baselines_valid),
        all_reference_band_radii_use_validation_errors=bool(bands_valid),
        macro_test_mape_matches=sum(macro) / len(macro) == report["macro_test_mape"]
        or math.isclose(sum(macro) / len(macro), report["macro_test_mape"], abs_tol=1e-12),
    )
    with httpx.Client(base_url="http://127.0.0.1:18880", timeout=60, trust_env=False) as client:
        catalog = client.get("/api/v1/forecasts/catalog").json()
        checks["catalog_bound_to_verified_data_and_model"] = (
            catalog["status"] == "ready"
            and catalog["report"]["model_run_id"] == report["model_run_id"]
            and catalog["meta"]["run_id"] == dataset
        )
        all_details, exports = True, True
        for pid, expected in products.items():
            response = client.get(
                "/api/v1/forecasts",
                params={
                    "product_id": pid,
                    "run_id": dataset,
                    "model_run_id": report["model_run_id"],
                },
            )
            actual = response.json()
            all_details &= (
                response.status_code == 200
                and actual["forecasts"] == expected["forecasts"]
                and actual["evaluation"] == expected["evaluation"]
            )
            csv_response = client.get(
                "/api/v1/forecasts/export.csv", params={"product_id": pid, "run_id": dataset}
            )
            exported = list(csv.DictReader(io.StringIO(csv_response.content.decode("utf-8-sig"))))
            exports &= len(exported) == len(expected["forecasts"]) and all(
                r["data_kind"] == "model_prediction_not_observed_quote" for r in exported
            )
        checks["all_44_product_api_responses_match_artifacts"] = bool(all_details)
        checks["all_forecast_exports_are_labelled_and_have_correct_rows"] = bool(exports)
        checks["unknown_product_rejected"] = (
            client.get("/api/v1/forecasts", params={"product_id": "unknown"}).status_code == 404
        )
        checks["stale_model_version_rejected"] = (
            client.get(
                "/api/v1/forecasts",
                params={"product_id": next(iter(products)), "model_run_id": "wrong-model"},
            ).status_code
            == 409
        )
    with sqlite3.connect(runtime / "forecasts/mlflow.sqlite") as db:
        logged = db.execute(
            "SELECT status,artifact_uri FROM runs WHERE run_uuid=?", [report["mlflow_run_id"]]
        ).fetchone()
    checks["actual_mlflow_run_finished_and_artifacts_persisted"] = bool(
        logged and logged[0] == "FINISHED" and "/runtime/forecasts/mlartifacts/" in logged[1]
    )
    checks["warehouse_pointer_and_database_unchanged"] = (
        pointer == (runtime / "warehouse/current.json").read_bytes()
        and hashlib.sha256(database.read_bytes()).hexdigest() == before
    )
    result = {
        "passed": all(checks.values()),
        "dataset_run_id": dataset,
        "model_run_id": report["model_run_id"],
        "as_of": report["as_of"],
        "ready_products": report["ready_products"],
        "forecast_rows": sum(len(p["forecasts"]) for p in products.values()),
        "backtest_rows": len(backtests),
        "checks": checks,
        "selection_counts": report["selection_counts"],
        "macro_test_mape": sum(macro) / len(macro),
        "naive_macro_test_mape": sum(baseline_macro) / len(baseline_macro),
        "largest_test_errors": sorted(worst, key=lambda r: r["test_mape"], reverse=True)[:5],
        "validation_origins": report["validation_origins"],
        "test_origins": report["test_origins"],
    }
    write_json(ROOT / "artifacts/day5/verification.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    assert result["passed"], checks


if __name__ == "__main__":
    main()
