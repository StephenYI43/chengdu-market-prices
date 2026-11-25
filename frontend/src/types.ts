export interface DatasetMeta {
  run_id: string; data_mode: 'historical_snapshot'; verified: boolean;
  date_start: string; as_of: string; source_file_sha256: string;
  source_filename: string; source_url: string;
  source_kind?: 'single_file' | 'manifest'; source_files_count?: number;
}
export interface Overview {
  meta: DatasetMeta;
  totals: { source_rows: number; quoted_rows: number; missing_rows: number; products: number;
    markets: number; districts: number; periods: number; review_flagged_rows: number;
    eligible_forecast_series: number };
  categories: string[]; districts: string[]; quote_dates: string[]; default_product_id: string;
}
export interface Product {
  product_id: string; product_name: string; product_spec: string; unit: string; category: string;
  first_observed_week: string; last_observed_week: string; observed_weeks: number;
  eligible_baseline: boolean; current: boolean;
}
export interface Market { market_id: string; district_id: string; district: string; market: string }
export interface Catalog<T> { meta: DatasetMeta; items: T[] }
export interface PriceStats {
  observed_market_count: number; quoted_market_count: number; missing_market_count: number;
  absent_market_count: number; expected_market_count: number; market_coverage: number;
  mean_price: string | null; median_price: string | null; min_price: string | null;
  max_price: string | null; price_range: string | null; ranking_available: boolean;
}
export interface TrendPoint extends PriceStats {
  quote_date: string; wow_change: number | null; common_market_wow_change: number | null;
  common_market_count: number;
}
export interface Trends { meta: DatasetMeta; product: Product; scope: string; items: TrendPoint[] }
export interface MarketQuote extends Market {
  price: string | null; price_raw: string | null; quote_status: 'quoted' | 'missing' | 'not_observed';
  rank: number | null; source_row_number: number | null; review_flags: string[];
}
export interface Comparison {
  meta: DatasetMeta; product: Product; scope: string; quote_date: string; summary: PriceStats;
  common_market_wow_change: number | null; common_market_count: number; items: MarketQuote[];
}
export interface PriceRecord {
  quote_date: string; product_id: string; product_name: string; product_spec: string;
  product_spec_raw: string; unit: string; district: string; market: string; market_id: string;
  price: string | null; price_raw: string; quote_status: 'quoted' | 'missing';
  missing_reason: string | null; review_flags: string[]; source_row_number: number;
  source_filename: string; source_record_id: string;
}
export interface Prices {
  meta: DatasetMeta; product: Product; scope: string; total: number; page: number;
  page_size: number; items: PriceRecord[];
}
export interface Selection {
  run_id: string; product_id: string; district: string; market_id: string;
  date_from: string; date_to: string;
}
