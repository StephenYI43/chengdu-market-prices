"""Spark batch curation. Never impute, winsorize or merge semantic specifications."""

import argparse
import csv
import hashlib
import json
from pathlib import Path

from pyspark.sql import SparkSession, Window
from pyspark.sql import functions as F

from market_prices.curation import (
    RAW_FIELDS,
    UNIT_ROWS,
    entity_id,
    forecast_profiles,
    read_aliases,
    run_folder,
    write_json,
)


def save(frame, folder, name, partition=None):
    writer = frame.coalesce(2).write.mode("errorifexists").option("compression", "zstd")
    if partition:
        writer = writer.partitionBy(partition)
    writer.parquet(str(folder / name))


def reasons(*conditions):
    return F.filter(F.array(*conditions), lambda value: value.isNotNull())


def summarize(quotes, groups, panel):
    return (
        quotes.groupBy(*groups)
        .agg(
            F.count("*").alias("observed_market_count"),
            F.count("price").alias("quoted_market_count"),
            F.sum(F.when(F.col("quote_status") == "missing", 1).otherwise(0)).alias(
                "missing_market_count"
            ),
            F.round(F.avg("price"), 4).cast("decimal(16,4)").alias("mean_price"),
            F.expr("percentile(price, 0.5)").cast("decimal(16,4)").alias("median_price"),
            F.min("price").alias("min_price"),
            F.max("price").alias("max_price"),
            F.stddev_pop("price").alias("stddev_price"),
        )
        .withColumns(
            {
                "expected_market_count": F.lit(panel),
                "market_coverage": F.col("quoted_market_count") / F.lit(panel),
                "price_range": F.col("max_price") - F.col("min_price"),
                "ranking_available": F.col("quoted_market_count") >= 2,
            }
        )
    )


def process(spark, folder, root):
    stage = json.loads((folder / "stage.json").read_text(encoding="utf-8"))
    for filename, key in [
        ("product-aliases.csv", "alias_file_sha256"),
        ("curation-policy.json", "policy_file_sha256"),
        ("markets.csv", "market_file_sha256"),
    ]:
        if hashlib.sha256((root / "config" / filename).read_bytes()).hexdigest() != stage[key]:
            raise ValueError(f"Configuration changed after staging: {filename}")
    policy = json.loads((root / "config/curation-policy.json").read_text(encoding="utf-8"))
    aliases = spark.createDataFrame(read_aliases(root / "config/product-aliases.csv"))
    with (root / "config/markets.csv").open(encoding="utf-8", newline="") as file:
        market_rows = list(csv.DictReader(file))
    if len({(r["district"], r["market"]) for r in market_rows}) != len(market_rows):
        raise ValueError("Market registry contains duplicates")
    for row in market_rows:
        row["market_id"] = entity_id("market", row["district"], row["market"])
        row["district_id"] = entity_id("district", row["district"])
    markets = spark.createDataFrame(market_rows)
    units = spark.createDataFrame(UNIT_ROWS)
    raw = spark.read.parquet(str(folder / "ods/quotes_raw.parquet"))
    base = (
        raw.join(F.broadcast(aliases), ["product_name_raw", "product_spec_raw", "unit_raw"], "left")
        .withColumns({"district": F.trim("district_raw"), "market": F.trim("market_raw")})
        .join(F.broadcast(markets), ["district", "market"], "left")
        .join(F.broadcast(units.select("unit", "unit_id")), "unit", "left")
        .withColumns(
            {
                "quote_date": F.expr("try_cast(quote_date_raw AS DATE)"),
                "price": F.when(
                    F.trim("price_raw") == "-", F.lit(None).cast("decimal(12,4)")
                ).otherwise(F.expr("try_cast(trim(price_raw) AS DECIMAL(12,4))")),
                "quote_status": F.when(F.trim("price_raw") == "-", "missing").otherwise("quoted"),
                "missing_reason": F.when(F.trim("price_raw") == "-", "source_no_quote"),
            }
        )
    )
    required_empty = F.lit(False)
    for field in RAW_FIELDS[:-1]:
        required_empty = required_empty | F.col(field).isNull() | (F.trim(field) == "")
    base = base.withColumn(
        "error_reasons",
        reasons(
            F.when(required_empty, "empty_required_field"),
            F.when(
                F.col("quote_date").isNull()
                | ~F.col("quote_date_raw").rlike(r"^\d{4}-\d{2}-\d{2}$"),
                "invalid_date",
            ),
            F.when(F.col("product_id").isNull(), "unmapped_product_alias"),
            F.when(F.col("market_id").isNull(), "unknown_market"),
            F.when(F.col("unit_id").isNull(), "unsupported_unit"),
            F.when(
                (F.col("quote_status") == "quoted")
                & (
                    F.col("price").isNull()
                    | (F.col("price") <= 0)
                    | ~F.trim("price_raw").rlike(r"^[0-9]+(?:\.[0-9]{1,4})?$")
                ),
                "invalid_price",
            ),
        ),
    ).cache()
    valid = base.filter(F.size("error_reasons") == 0)
    duplicates = valid.groupBy("business_key").agg(
        F.count("*").alias("duplicate_group_size"),
        F.countDistinct(F.struct("price", "quote_status")).alias("distinct_quote_values"),
    )
    ranked = (
        valid.join(duplicates, "business_key")
        .withColumn(
            "duplicate_rank",
            F.row_number().over(Window.partitionBy("business_key").orderBy("source_row_number")),
        )
        .cache()
    )
    conflict = (
        ranked.filter(F.col("distinct_quote_values") > 1)
        .withColumn("error_reasons", F.array(F.lit("conflicting_duplicate")))
        .select(*base.columns)
    )
    quarantine = base.filter(F.size("error_reasons") > 0).unionByName(conflict)
    identical_duplicates = ranked.filter(
        (F.col("duplicate_group_size") > 1) & (F.col("distinct_quote_values") == 1)
    )
    clean = ranked.filter((F.col("distinct_quote_values") == 1) & (F.col("duplicate_rank") == 1))
    clean = clean.drop(
        "duplicate_group_size", "distinct_quote_values", "duplicate_rank", "error_reasons"
    ).cache()
    clean_rows = clean.count()
    if clean_rows == 0:
        save(quarantine, folder, "quarantine/quotes")
        raise ValueError("No clean quotes remain; run is not publishable")

    product_fields = ["product_id", "product_name", "product_spec", "unit", "unit_id", "category"]
    weekly = summarize(clean, ["quote_date", *product_fields], len(market_rows))
    price_window = Window.partitionBy("product_id").orderBy("quote_date")
    weekly = weekly.withColumns(
        {
            "previous_observed_date": F.lag("quote_date").over(price_window),
            "previous_observed_mean": F.lag("mean_price").over(price_window),
        }
    ).withColumn(
        "wow_change",
        F.when(
            (F.datediff("quote_date", "previous_observed_date") == 7)
            & (F.col("previous_observed_mean") > 0),
            (F.col("mean_price") / F.col("previous_observed_mean") - 1).cast("double"),
        ),
    )
    price_points = clean.filter(F.col("price").isNotNull()).select(
        "product_id", "market_id", "quote_date", "price"
    )
    pairs = price_points.alias("cur").join(
        price_points.alias("prev"),
        (F.col("cur.product_id") == F.col("prev.product_id"))
        & (F.col("cur.market_id") == F.col("prev.market_id"))
        & (F.col("prev.quote_date") == F.date_sub(F.col("cur.quote_date"), 7)),
        "inner",
    )
    paired_weekly = (
        pairs.select(
            F.col("cur.product_id").alias("product_id"),
            F.col("cur.quote_date").alias("quote_date"),
            F.col("cur.price").alias("current_price"),
            F.col("prev.price").alias("previous_price"),
        )
        .groupBy("product_id", "quote_date")
        .agg(
            F.count("*").alias("common_market_count"),
            F.avg("current_price").alias("common_current_mean"),
            F.avg("previous_price").alias("common_previous_mean"),
        )
        .withColumn(
            "common_market_wow_change",
            (F.col("common_current_mean") / F.col("common_previous_mean") - 1).cast("double"),
        )
    )
    weekly = (
        weekly.join(paired_weekly, ["product_id", "quote_date"], "left")
        .fillna({"common_market_count": 0})
        .cache()
    )

    market_window = Window.partitionBy("product_id", "market_id").orderBy("quote_date")
    flagged = clean.join(
        weekly.select("product_id", "quote_date", "median_price"),
        ["product_id", "quote_date"],
        "left",
    )
    flagged = flagged.withColumns(
        {
            "previous_quote_date": F.lag("quote_date").over(market_window),
            "previous_price": F.lag("price").over(market_window),
        }
    ).withColumns(
        {
            "market_wow_change": F.when(
                (F.datediff("quote_date", "previous_quote_date") == 7)
                & (F.col("previous_price") > 0),
                (F.col("price") / F.col("previous_price") - 1).cast("double"),
            ),
            "peer_median_ratio": F.when(
                F.col("median_price") > 0, (F.col("price") / F.col("median_price")).cast("double")
            ),
        }
    )
    flags = policy["review_flags"]
    flagged = (
        flagged.withColumn(
            "review_flags",
            reasons(
                F.when(
                    F.abs("market_wow_change") >= flags["absolute_weekly_change_ratio"],
                    "large_weekly_change",
                ),
                F.when(
                    F.col("peer_median_ratio") >= flags["cross_market_high_ratio"],
                    "high_vs_peer_median",
                ),
                F.when(
                    F.col("peer_median_ratio") <= flags["cross_market_low_ratio"],
                    "low_vs_peer_median",
                ),
            ),
        )
        .withColumn("quote_year", F.year("quote_date"))
        .cache()
    )

    alias_stats = clean.groupBy(
        "product_id", "product_name_raw", "product_spec_raw", "unit_raw"
    ).agg(
        F.min("quote_date").alias("first_observed_week"),
        F.max("quote_date").alias("last_observed_week"),
        F.count("*").alias("source_row_count"),
    )
    product_aliases = aliases.join(
        alias_stats, ["product_id", "product_name_raw", "product_spec_raw", "unit_raw"], "left"
    )
    products = clean.groupBy(*product_fields).agg(
        F.min("quote_date").alias("first_observed_week"),
        F.max("quote_date").alias("last_observed_week"),
        F.countDistinct("quote_date").alias("observed_weeks"),
        F.countDistinct("product_spec_raw").alias("raw_alias_count"),
    )
    districts = (
        markets.select("district_id", "district")
        .distinct()
        .withColumn("group_kind", F.lit("source_monitoring_group"))
    )
    calendar = (
        clean.select("quote_date")
        .distinct()
        .withColumns(
            {
                "quote_year": F.year("quote_date"),
                "iso_weekday": F.weekday("quote_date") + 1,
            }
        )
    )
    district_panel = (
        markets.groupBy("district_id").count().withColumnRenamed("count", "expected_market_count")
    )
    district_weekly = summarize(
        clean, ["quote_date", *product_fields, "district_id", "district"], 1
    )
    district_weekly = (
        district_weekly.drop("expected_market_count", "market_coverage")
        .join(district_panel, "district_id")
        .withColumn(
            "market_coverage", F.col("quoted_market_count") / F.col("expected_market_count")
        )
    )

    spec_sets = clean.groupBy("quote_date", "product_name").agg(
        F.sort_array(F.collect_set(F.struct("product_id", "product_spec", "unit"))).alias(
            "current_specifications"
        )
    )
    spec_window = Window.partitionBy("product_name").orderBy("quote_date")
    spec_changes = spec_sets.withColumns(
        {
            "previous_specifications": F.lag("current_specifications").over(spec_window),
            "previous_quote_date": F.lag("quote_date").over(spec_window),
        }
    ).filter(
        (F.col("current_specifications") != F.col("previous_specifications"))
        & (F.datediff("quote_date", "previous_quote_date") == 7)
    )
    as_of = calendar.agg(F.max("quote_date")).first()[0]
    profile_rows = forecast_profiles(
        [r.asDict() for r in weekly.collect()], as_of, policy["forecast"]
    )
    profiles = spark.createDataFrame(profile_rows)
    for frame, name in [
        (products, "dim/products"),
        (product_aliases, "dim/product_aliases"),
        (markets, "dim/markets"),
        (districts, "dim/districts"),
        (units, "dim/units"),
        (calendar, "dim/calendar"),
        (quarantine, "quarantine/quotes"),
        (identical_duplicates, "audit/duplicates"),
        (flagged.filter(F.size("review_flags") > 0), "audit/review_flags"),
        (spec_changes, "audit/spec_changes"),
        (weekly, "dws/product_weekly"),
        (district_weekly, "dws/district_weekly"),
        (profiles, "mart/forecast_profiles"),
    ]:
        save(frame, folder, name)
    save(flagged, folder, "dwd/quotes", "quote_year")
    write_json(
        folder / "spark-result.json",
        {
            "spark_version": spark.version,
            "source_rows": raw.count(),
            "clean_rows": clean_rows,
            "quarantine_rows": quarantine.count(),
            "identical_duplicate_rows_removed": identical_duplicates.filter(
                F.col("duplicate_rank") > 1
            ).count(),
            "product_count": products.count(),
            "market_count": markets.count(),
            "district_count": districts.count(),
            "period_count": calendar.count(),
            "product_weekly_rows": weekly.count(),
            "district_weekly_rows": district_weekly.count(),
            "spec_change_events": spec_changes.count(),
            "review_flagged_rows": flagged.filter(F.size("review_flags") > 0).count(),
            "eligible_forecast_series": sum(r["eligible_baseline"] for r in profile_rows),
            "as_of": as_of,
        },
    )
    print((folder / "spark-result.json").read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    root = Path("/app")
    folder = run_folder(root / "runtime/warehouse", args.run_id)
    spark = (
        SparkSession.builder.appName("chengdu-day2-full-curation")
        .config("spark.sql.shuffle.partitions", "2")
        .config("spark.sql.session.timeZone", "Asia/Shanghai")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("WARN")
    try:
        process(spark, folder, root)
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
