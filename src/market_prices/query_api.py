"""Read-only historical analysis API, pinned to an explicit verified data batch."""

import csv
import io
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response

from market_prices.query_models import (
    ComparisonResponse,
    MarketsResponse,
    OverviewResponse,
    PricesResponse,
    ProductsResponse,
    TrendsResponse,
)
from market_prices.warehouse import PRICE_COLUMNS, SORTS, Snapshot, open_snapshot

router = APIRouter(
    prefix="/api/v1",
    tags=["历史价格分析"],
    responses={
        404: {"description": "商品、市场或批次不存在"},
        503: {"description": "已验收数据仓库不可用"},
    },
)


def get_snapshot(
    run_id: Annotated[str | None, Query(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")] = None,
):
    with open_snapshot(run_id) as snapshot:
        yield snapshot


Data = Annotated[Snapshot, Depends(get_snapshot)]
Sort = Literal["date_desc", "date_asc", "price_asc", "price_desc"]
Status = Literal["quoted", "missing"]


@router.get("/overview", response_model=OverviewResponse)
def overview(data: Data):
    preferred = data.rows(
        "SELECT product_id FROM products "
        "ORDER BY (product_name='籼米') DESC,product_name,product_spec LIMIT 1"
    )
    return {
        "meta": data.meta,
        "totals": data.report["summary"],
        "categories": [
            r["category"]
            for r in data.rows("SELECT DISTINCT category FROM products ORDER BY category")
        ],
        "districts": [
            r["district"] for r in data.rows("SELECT district FROM districts ORDER BY district")
        ],
        "quote_dates": [
            r["quote_date"]
            for r in data.rows("SELECT quote_date FROM calendar ORDER BY quote_date")
        ],
        "default_product_id": preferred[0]["product_id"],
    }


@router.get("/products", response_model=ProductsResponse)
def products(
    data: Data,
    q: Annotated[str, Query(max_length=80)] = "",
    category: Annotated[str | None, Query(max_length=40)] = None,
):
    return {"meta": data.meta, "items": data.products(q, category)}


@router.get("/markets", response_model=MarketsResponse)
def markets(data: Data, district: Annotated[str | None, Query(max_length=40)] = None):
    data.scope(district)
    return {
        "meta": data.meta,
        "items": data.rows(
            "SELECT * FROM markets WHERE (? IS NULL OR district=?) ORDER BY district,market",
            [district, district],
        ),
    }


@router.get("/trends", response_model=TrendsResponse)
def trends(
    data: Data,
    product_id: Annotated[str, Query(min_length=1, max_length=96)],
    date_from: date | None = None,
    date_to: date | None = None,
    district: Annotated[str | None, Query(max_length=40)] = None,
    market_id: Annotated[str | None, Query(max_length=96)] = None,
):
    return {
        "meta": data.meta,
        "product": data.product(product_id),
        "scope": data.scope(district, market_id),
        "items": data.trends(product_id, date_from, date_to, district, market_id),
    }


@router.get("/comparisons", response_model=ComparisonResponse)
def comparisons(
    data: Data,
    product_id: Annotated[str, Query(min_length=1, max_length=96)],
    quote_date: date | None = None,
    district: Annotated[str | None, Query(max_length=40)] = None,
    market_id: Annotated[str | None, Query(max_length=96)] = None,
):
    day = quote_date or data.meta.as_of
    return {
        "meta": data.meta,
        "product": data.product(product_id),
        "scope": data.scope(district, market_id),
        "quote_date": day,
        **data.comparison(product_id, day, district, market_id),
    }


@router.get("/prices", response_model=PricesResponse)
def prices(
    data: Data,
    product_id: Annotated[str, Query(min_length=1, max_length=96)],
    date_from: date | None = None,
    date_to: date | None = None,
    district: Annotated[str | None, Query(max_length=40)] = None,
    market_id: Annotated[str | None, Query(max_length=96)] = None,
    quote_status: Status | None = None,
    review_only: bool = False,
    sort: Sort = "date_desc",
    page: Annotated[int, Query(ge=1, le=100000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    product = data.product(product_id)
    where, params = data.filters(
        product_id, date_from, date_to, district, market_id, quote_status, review_only
    )
    return {
        "meta": data.meta,
        "product": product,
        "scope": data.scope(district, market_id),
        "total": data.scalar(f"SELECT count(*) FROM quotes WHERE {where}", params),
        "page": page,
        "page_size": page_size,
        "items": data.rows(
            f"SELECT {PRICE_COLUMNS} FROM quotes WHERE {where} "
            f"ORDER BY {SORTS[sort]} LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        ),
    }


@router.get(
    "/prices/export.csv",
    response_class=Response,
    responses={
        200: {"content": {"text/csv": {}}, "description": "当前筛选下的全部实际报价，UTF-8 BOM CSV"}
    },
)
def export_prices(
    data: Data,
    product_id: Annotated[str, Query(min_length=1, max_length=96)],
    date_from: date | None = None,
    date_to: date | None = None,
    district: Annotated[str | None, Query(max_length=40)] = None,
    market_id: Annotated[str | None, Query(max_length=96)] = None,
    quote_status: Status | None = None,
    review_only: bool = False,
    sort: Sort = "date_desc",
):
    data.product(product_id)
    where, params = data.filters(
        product_id, date_from, date_to, district, market_id, quote_status, review_only
    )
    rows = data.rows(
        f"SELECT {PRICE_COLUMNS} FROM quotes WHERE {where} ORDER BY {SORTS[sort]}", params
    )
    stream = io.StringIO(newline="")
    fields = [
        "quote_date",
        "product_name",
        "product_spec",
        "product_spec_raw",
        "unit",
        "district",
        "market",
        "price",
        "price_raw",
        "quote_status",
        "review_flags",
        "source_row_number",
        "source_filename",
        "source_record_id",
    ]
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(
        [
            "报价日期",
            "商品名称",
            "规范规格",
            "原始规格",
            "单位",
            "区分组",
            "市场",
            "解析价格",
            "原始价格",
            "报价状态",
            "复核提示",
            "源记录序号",
            "来源文件",
            "来源记录ID",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                ";".join(row[field]) if isinstance(row[field], list) else row[field]
                for field in fields
            ]
        )
    return Response(
        content=b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="prices-{data.meta.run_id}.csv"',
            "X-Dataset-Run-Id": data.meta.run_id,
        },
    )
