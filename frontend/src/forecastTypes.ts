import type { DatasetMeta, Product } from './types';
export interface Score { n: number; mae: number; rmse: number; mape: number; wape: number }
export interface ForecastPoint { target_date: string; horizon: number; predicted: string; lower: string | null; upper: string | null; reference_samples: number }
export interface BacktestPoint { origin: string; target_date: string; horizon: number; model: string; predicted: number; actual: number }
export interface ForecastReport {
  model_run_id: string; as_of: string; ready_products: number; excluded_products: number;
  selection_counts: Record<string, number>; model_names: Record<string, string>;
  validation_origins: string[]; test_origins: string[]; selection_available_at: string;
  macro_test_mape: number | null; created_at: string; lightgbm_version: string; mlflow_run_id: string;
}
export interface Profile { exclusion_reasons: string[]; adequate_weeks: number; recent_consecutive_adequate_weeks: number; history_coverage: number }
export interface ForecastCatalog {
  status: string; report?: ForecastReport;
  service: { online: boolean; status?: string; message?: string; error?: string };
  items: { product: Product; status: string; profile: Profile; selected_model?: string }[];
}
export interface ForecastDetail {
  meta: DatasetMeta; model_run_id: string; as_of: string; report: ForecastReport;
  product: Product; status: string; profile: Profile; selected_model?: string; selection_reason?: string;
  validation_scores?: Record<string, Score | null>;
  history: { quote_date: string; mean_price: string | null; quoted_market_count: number }[];
  forecasts: ForecastPoint[];
  evaluation: { models: Record<string, Score | null>; selected_by_horizon: Record<string, Score | null>; selected: Score | null; rows: BacktestPoint[] } | null;
}
