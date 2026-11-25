"""Read-only derived predictions; forecast artifacts never become observed quotations."""

import csv
import io
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import Response

from market_prices.forecast_store import bundle_for, service_status
from market_prices.warehouse import DataError, open_snapshot

router = APIRouter(prefix="/api/v1/forecasts", tags=["周度预测与回测"])
RunId = Annotated[str | None, Query(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")]


def get_bundle(run_id, model_run_id=None):
    with open_snapshot(run_id) as snapshot:
        meta = snapshot.meta.model_dump(mode="json")
    bundle = bundle_for(meta["run_id"])
    if bundle and bundle[0]["dataset"]["source_file_sha256"] != meta["source_file_sha256"]:
        raise DataError(503, "forecast_source_mismatch", "模型与来源数据不一致，等待重新训练")
    if bundle and model_run_id and bundle[0]["model_run_id"] != model_run_id:
        raise DataError(409, "forecast_model_changed", "模型版本已更新，请刷新预测页面")
    return meta, bundle


@router.get("/catalog")
def forecast_catalog(run_id: RunId = None):
    meta, bundle = get_bundle(run_id)
    if not bundle:
        return {"status": "not_ready", "meta": meta, "service": service_status(), "items": []}
    report, products, _ = bundle
    return {
        "status": "ready",
        "meta": meta,
        "report": report,
        "service": service_status(),
        "items": [
            {
                "product": p["product"],
                "status": p["status"],
                "profile": p["profile"],
                "selected_model": p.get("selected_model"),
            }
            for p in products.values()
        ],
    }


@router.get("")
def forecast_detail(product_id: str, run_id: RunId = None, model_run_id: RunId = None):
    meta, bundle = get_bundle(run_id, model_run_id)
    if not bundle:
        raise DataError(503, "forecast_not_ready", "该数据批次的预测尚未就绪，训练服务正在处理")
    report, products, _ = bundle
    if product_id not in products:
        raise DataError(404, "product_not_found", "未找到该商品规格")
    return {
        "meta": meta,
        "model_run_id": report["model_run_id"],
        "as_of": report["as_of"],
        "report": report,
        **products[product_id],
    }


@router.get("/export.csv")
def export_predictions(product_id: str, run_id: RunId = None, model_run_id: RunId = None):
    result = forecast_detail(product_id, run_id, model_run_id)
    output = io.StringIO(newline="")
    fields = [
        "data_kind",
        "dataset_run_id",
        "model_run_id",
        "as_of",
        "product_name",
        "product_spec",
        "unit",
        "model",
        "target_date",
        "horizon",
        "predicted",
        "lower",
        "upper",
        "reference_samples",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in result["forecasts"]:
        writer.writerow(
            {
                "data_kind": "model_prediction_not_observed_quote",
                "dataset_run_id": result["meta"]["run_id"],
                "model_run_id": result["model_run_id"],
                "as_of": result["as_of"],
                **{key: result["product"][key] for key in ["product_name", "product_spec", "unit"]},
                "model": result.get("selected_model"),
                **row,
            }
        )
    return Response(
        "\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="weekly-model-predictions.csv"'},
    )
