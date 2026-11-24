# 第二天数据仓库结构

根目录为 `runtime/warehouse/runs/<run_id>/`，当前已验收批次由 `runtime/warehouse/current.json` 指向。源数据、映射和规则哈希保存在 `stage.json` 与 `verification.json` 中。

## 表与粒度

| DuckDB表 | Parquet位置 | 粒度与用途 |
|---|---|---|
| raw_quotes | ods/quotes_raw.parquet | 每条CSV记录，完整原始文本、源记录序号和哈希 |
| quotes | dwd/quotes/quote_year=... | 报价期×商品规格×市场；价格、缺失状态、原字段、质量提示 |
| products | dim/products | 44个商品规格；规范名称、规格、单位、分类、观测范围 |
| product_aliases | dim/product_aliases | 53个原始名称规格组合及映射理由、首次末次观测、记录数 |
| markets | dim/markets | 18个来源市场；市场ID、区分组ID和名称 |
| districts | dim/districts | 6个来源监测分组，不作行政区边界定义 |
| units | dim/units | 3个计价单位及其计价基数 |
| calendar | dim/calendar | 105个实际报价日期；年份、ISO星期序号 |
| product_weekly | dws/product_weekly | 报价期×商品规格，全市监测市场范围的汇总和比较 |
| district_weekly | dws/district_weekly | 报价期×商品规格×区分组 |
| quarantined_quotes | quarantine/quotes | 非法或冲突报价及原因；本次0条 |
| duplicate_quotes | audit/duplicates | 相同数值的重复业务键组及保留顺序；本次0条 |
| review_flags | audit/review_flags | 727条质量提示记录，原价保留 |
| spec_changes | audit/spec_changes | 连续两期的商品规格集合发生变化；本次1条 |
| forecast_profiles | mart/forecast_profiles | 商品规格×截止期的覆盖指标和预测入选理由 |
| build_metadata | 仅warehouse.duckdb | 数据库对应的批次和源文件哈希 |

`warehouse.duckdb` 存储物理表，不依赖Windows或容器的绝对Parquet路径，后续后端可在容器内只读访问同一批次数据库。

## 报价事实表主要字段

| 字段组 | 字段与含义 |
|---|---|
| 业务键 | quote_date、product_id、market_id、business_key |
| 报价 | price decimal(12,4)、price_raw、quote_status、missing_reason |
| 商品 | product_name、product_spec、category、unit、unit_id |
| 市场 | market、district、district_id |
| 原始文本 | quote_date_raw、product_name_raw、product_spec_raw、unit_raw、district_raw、market_raw |
| 来源 | source_row_number、source_record_id、source_file_sha256、source_filename、source_url、event_id |
| 审计 | previous_quote_date、previous_price、market_wow_change、median_price、peer_median_ratio、review_flags |
| 分区 | quote_year |

## 周度汇总主要字段

- observed_market_count：实际出现记录的市场数，包含无报价。
- quoted_market_count / missing_market_count：有效价格市场数和显式无报价市场数。
- expected_market_count：市场维表定义的目标市场数，全市18、每区本次为3。
- market_coverage：有效报价市场数除以目标市场数。
- mean_price / median_price / min_price / max_price / price_range / stddev_price：同商品规格内的价格统计。
- ranking_available：至少两个有效市场。
- wow_change：全市相邻连续两期均价变动率，0.1代表10%。
- common_market_count / common_current_mean / common_previous_mean / common_market_wow_change：连续两期配对市场数量、均价和变动率。

## 预测候选表主要字段

`observed_weeks` 是出现记录的周数；`weeks_with_any_price` 是至少有一个价格的周数；`adequate_weeks` 是满足市场覆盖阈值的周数。三者含义不同。

`expected_weeks_since_first` 从首次观测计算至截止期，`absent_weeks_since_first` 表示期间未出现记录的周数，`history_coverage` 使用覆盖充足周数作为分子。`recent_consecutive_adequate_weeks` 从截止期倒数，遇缺期或覆盖不足立即停止。

`eligible_baseline` 为首批基线入选标识，`annual_seasonality_candidate` 为连续104周覆盖标识，`exclusion_reasons` 返回具体原因码。规则与限制见 [清洗口径](cleaning-rules.md)。

## 可直接检查的SQL

连接当前批次的 `warehouse.duckdb` 后：

```sql
SELECT count(*) AS rows, count(price) AS priced_rows FROM quotes;

SELECT product_name, product_spec, unit, observed_weeks,
       recent_consecutive_adequate_weeks, eligible_baseline, exclusion_reasons
FROM forecast_profiles ORDER BY eligible_baseline DESC, product_name, product_spec;

SELECT quote_date, product_name, product_spec, unit, mean_price,
       quoted_market_count, price_range, common_market_wow_change
FROM product_weekly
WHERE quote_date=(SELECT max(quote_date) FROM calendar)
ORDER BY product_name, product_spec;

SELECT source_row_number, quote_date, product_name, market, price, review_flags
FROM review_flags ORDER BY quote_date, product_name, market;
```
