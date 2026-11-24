"""Consume real quotes with Structured Streaming and reconcile each event to its source."""

import argparse
import json
import os
from decimal import Decimal
from pathlib import Path

import duckdb
from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql import types as T


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    folder = Path("/app/artifacts/day1") / args.run_id
    expected = json.loads((folder / "published.json").read_text(encoding="utf-8"))
    spark = (
        SparkSession.builder.appName("chengdu-day1-real-data-smoke")
        .config("spark.sql.shuffle.partitions", "2")
        .config("spark.sql.session.timeZone", "Asia/Shanghai")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("WARN")
    fields = [
        "event_id",
        "business_key",
        "batch_id",
        "mode",
        "quote_date",
        "product_name",
        "product_spec",
        "unit",
        "district",
        "market",
        "price_raw",
        "price",
        "quote_status",
        "source_url",
        "source_file_sha256",
        "ingested_at",
        "schema_version",
    ]
    schema = T.StructType([T.StructField(name, T.StringType(), True) for name in fields])
    incoming = (
        spark.readStream.format("kafka")
        .option("kafka.bootstrap.servers", os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092"))
        .option("subscribe", expected["topic"])
        .option("startingOffsets", "earliest")
        .option("failOnDataLoss", "true")
        .load()
    )
    quotes = (
        incoming.select(F.from_json(F.col("value").cast("string"), schema).alias("q"))
        .select("q.*")
        .withColumn("price", F.col("price").cast(T.DecimalType(12, 4)))
    )
    query = (
        quotes.writeStream.format("parquet")
        .outputMode("append")
        .option("path", str(folder / "quotes.parquet"))
        .option("checkpointLocation", str(folder / "checkpoint"))
        .trigger(availableNow=True)
        .start()
    )
    if not query.awaitTermination(120):
        query.stop()
        raise TimeoutError("Streaming sample did not finish within 120 seconds")
    actual = spark.read.parquet(str(folder / "quotes.parquet"))
    summary = (
        actual.agg(
            F.count("*").alias("rows"),
            F.count("price").alias("quoted"),
            F.countDistinct("business_key").alias("business_keys"),
            F.sum("price").alias("price_sum_control_only"),
            F.countDistinct("market").alias("markets"),
            F.countDistinct("district").alias("districts"),
        )
        .first()
        .asDict()
    )
    summary["missing"] = summary["rows"] - summary["quoted"]
    actual_records = {row.event_id: row.asDict() for row in actual.collect()}
    actual_ids = sorted(actual_records)
    checks = {
        key: summary[key] == expected[key] for key in ["rows", "quoted", "missing", "business_keys"]
    }
    checks["all_event_ids_match"] = actual_ids == expected["event_ids"]
    checks["all_payload_fields_match"] = checks["all_event_ids_match"] and all(
        all(
            actual_records[event["event_id"]][field]
            == (
                Decimal(event[field])
                if field == "price" and event[field] is not None
                else event[field]
            )
            for field in fields
        )
        for event in expected["records"]
    )
    checks["price_control_sum_match"] = summary["price_sum_control_only"] == Decimal(
        expected["price_sum_control_only"]
    )
    checks["original_quote_date_preserved"] = (
        actual.select("quote_date").distinct().first()[0] == expected["quote_date"]
        and actual.select("quote_date").distinct().count() == 1
    )
    checks["replay_label_preserved"] = (
        actual.filter(F.col("mode") != "historical_replay").count() == 0
    )
    invalid = actual.filter(
        ((F.col("quote_status") == "missing") & F.col("price").isNotNull())
        | ((F.col("quote_status") == "quoted") & (F.col("price").isNull() | (F.col("price") <= 0)))
    ).count()
    checks["missing_and_price_semantics_match"] = invalid == 0
    with duckdb.connect() as db:
        parquet_counts = db.execute(
            "SELECT count(*), count(price) FROM read_parquet(?)",
            [str(folder / "quotes.parquet/part-*.parquet")],
        ).fetchone()
    checks["duckdb_parquet_counts_match"] = parquet_counts == (expected["rows"], expected["quoted"])
    passed = all(checks.values())
    report = {
        "passed": passed,
        "run_id": args.run_id,
        "spark_version": spark.version,
        "topic": expected["topic"],
        "quote_date": expected["quote_date"],
        "actual": summary,
        "checks": checks,
        "control_sum_note": "仅用于核对传输完整性，不同单位价格之和不作业务指标",
    }
    (folder / "verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, default=str))
    spark.stop()
    if not passed:
        raise AssertionError("Kafka to Spark reconciliation failed")


if __name__ == "__main__":
    main()
