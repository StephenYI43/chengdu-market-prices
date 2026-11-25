"""Public query contracts. Decimal prices serialize as JSON strings."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel


class DatasetMeta(BaseModel):
    run_id: str
    data_mode: Literal["historical_snapshot"] = "historical_snapshot"
    verified: bool = True
    date_start: date
    as_of: date
    source_file_sha256: str
    source_filename: str
    source_url: str
    source_kind: Literal["single_file", "manifest"] = "single_file"
    source_files_count: int = 1


class Totals(BaseModel):
    source_rows: int
    quoted_rows: int
    missing_rows: int
    products: int
    markets: int
    districts: int
    periods: int
    review_flagged_rows: int
    eligible_forecast_series: int


class OverviewResponse(BaseModel):
    meta: DatasetMeta
    totals: Totals
    categories: list[str]
    districts: list[str]
    quote_dates: list[date]
    default_product_id: str


class Product(BaseModel):
    product_id: str
    product_name: str
    product_spec: str
    unit: str
    category: str
    first_observed_week: date
    last_observed_week: date
    observed_weeks: int
    eligible_baseline: bool
    current: bool


class ProductsResponse(BaseModel):
    meta: DatasetMeta
    items: list[Product]


class Market(BaseModel):
    market_id: str
    district_id: str
    district: str
    market: str


class MarketsResponse(BaseModel):
    meta: DatasetMeta
    items: list[Market]


class PriceStats(BaseModel):
    observed_market_count: int
    quoted_market_count: int
    missing_market_count: int
    absent_market_count: int
    expected_market_count: int
    market_coverage: float
    mean_price: Decimal | None
    median_price: Decimal | None
    min_price: Decimal | None
    max_price: Decimal | None
    price_range: Decimal | None
    ranking_available: bool


class TrendPoint(PriceStats):
    quote_date: date
    wow_change: float | None
    common_market_wow_change: float | None
    common_market_count: int


class TrendsResponse(BaseModel):
    meta: DatasetMeta
    product: Product
    scope: str
    items: list[TrendPoint]


class MarketQuote(Market):
    price: Decimal | None
    price_raw: str | None
    quote_status: Literal["quoted", "missing", "not_observed"]
    rank: int | None
    source_row_number: int | None
    review_flags: list[str]


class ComparisonResponse(BaseModel):
    meta: DatasetMeta
    product: Product
    scope: str
    quote_date: date
    summary: PriceStats
    common_market_wow_change: float | None
    common_market_count: int
    items: list[MarketQuote]


class PriceRecord(BaseModel):
    quote_date: date
    product_id: str
    product_name: str
    product_spec: str
    product_spec_raw: str
    unit: str
    district: str
    market: str
    market_id: str
    price: Decimal | None
    price_raw: str
    quote_status: Literal["quoted", "missing"]
    missing_reason: str | None
    review_flags: list[str]
    source_row_number: int
    source_filename: str
    source_record_id: str


class PricesResponse(BaseModel):
    meta: DatasetMeta
    product: Product
    scope: str
    total: int
    page: int
    page_size: int
    items: list[PriceRecord]
