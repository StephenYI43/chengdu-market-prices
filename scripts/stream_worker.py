"""Persistent Kafka -> Spark Structured Streaming -> verified incremental snapshots."""

import json
import logging
import os
import threading
import time
import uuid

import pyarrow as pa
from build_incremental import build_snapshot
from confluent_kafka import Producer
from confluent_kafka.admin import AdminClient, NewTopic
from pyspark.sql import SparkSession

from market_prices.import_records import price_value
from market_prices.import_service import payload_digest, read_candidates
from market_prices.job_store import JobStore, now
from market_prices.warehouse import open_snapshot

logger = logging.getLogger("price-stream")


def ensure_topic(servers, topic):
    admin = AdminClient({"bootstrap.servers": servers})
    if topic not in admin.list_topics(timeout=15).topics:
        for future in admin.create_topics([NewTopic(topic, 1, 1)], request_timeout=20).values():
            try:
                future.result(timeout=30)
            except Exception as error:
                if "TOPIC_ALREADY_EXISTS" not in str(error):
                    raise


def dispatch(store, owner, stop, phase):
    servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
    topic = os.getenv("MARKET_IMPORT_TOPIC", "price.imports.v1")
    producer = Producer(
        {"bootstrap.servers": servers, "enable.idempotence": True, "message.timeout.ms": 30000}
    )
    while not stop.is_set():
        if not store.worker_lease(owner, phase[0]):
            raise RuntimeError("Worker lease was lost")
        job = store.claim_publish(owner)
        if not job:
            stop.wait(1)
            continue
        try:
            candidates = read_candidates(store, job["preview_id"])
            failures = []

            def delivered(error, _message):
                if error:
                    failures.append(str(error))

            for number, record in enumerate(candidates, start=1):
                body = {"kind": "quote", "job_id": job["id"], "record": record}
                while True:
                    try:
                        producer.produce(
                            topic,
                            partition=0,
                            key=job["id"],
                            value=json.dumps(body, ensure_ascii=False).encode(),
                            callback=delivered,
                        )
                        break
                    except BufferError:
                        producer.poll(0.1)
                producer.poll(0)
                if number % 5000 == 0:
                    store.worker_lease(owner, phase[0])
                    store.update_job(
                        job["id"], {"published_rows": number}, only_status={"publishing"}
                    )
            seal = {
                "kind": "seal",
                "job_id": job["id"],
                "count": len(candidates),
                "digest": payload_digest(candidates),
            }
            producer.produce(
                topic,
                partition=0,
                key=job["id"],
                value=json.dumps(seal).encode(),
                callback=delivered,
            )
            if producer.flush(30) or failures:
                raise RuntimeError("Kafka消息未全部确认发送")
            store.update_job(
                job["id"],
                {
                    "status": "streaming",
                    "published_rows": len(candidates),
                    "message": "报价已发送，等待流处理核对",
                },
                only_status={"publishing"},
            )
        except Exception:
            logger.exception("Import dispatch failed")
            store.update_job(
                job["id"],
                {
                    "status": "failed",
                    "error_message": "消息发送失败，请检查服务后重新预览导入",
                    "finished_at": now(),
                },
            )


def finish_ready(spark, store):
    for job in store.ready_jobs():
        received = store.received_records(job["id"])
        if len(received) != job["expected_rows"]:
            continue
        try:
            if payload_digest(received) != job["expected_digest"]:
                raise RuntimeError("消息完整性核对失败")
            with open_snapshot() as current:
                if current.meta.run_id == job.get("target_run_id"):
                    store.update_job(
                        job["id"],
                        {
                            "status": "succeeded",
                            "result_run_id": current.meta.run_id,
                            "finished_at": now(),
                            "published_rows": job["expected_rows"],
                            "received_rows": len(received),
                            "inserted_rows": job.get("pending_rows", job["inserted_rows"]),
                            "message": "已恢复完成状态，数据未重复入库",
                        },
                    )
                    continue
                existing = {
                    r["business_key"]: str(r["price"]) if r["price"] is not None else None
                    for r in current.rows("SELECT business_key,price FROM quotes")
                }
                appended = []
                for record in received:
                    key = record["business_key"]
                    if key in existing:
                        if existing[key] != price_value(record["price_raw"]):
                            raise RuntimeError("任务执行前发现新的历史报价冲突，未覆盖历史")
                    else:
                        appended.append(record)
                if not appended:
                    store.update_job(
                        job["id"],
                        {
                            "status": "no_op",
                            "finished_at": now(),
                            "message": "入库前复核全部记录已存在，未重复入库",
                            "skipped_rows": job["skipped_rows"] + len(received),
                        },
                    )
                    continue
                base = current.db.sql("SELECT * FROM raw_quotes").to_arrow_table()
                parent = current.meta.run_id
            merged = pa.concat_tables([base, pa.Table.from_pylist(appended, schema=base.schema)])
            attempt = job.get("attempt", 0) + 1
            run_id = f"import-{job['id']}-{attempt}"
            store.update_job(
                job["id"],
                {
                    "status": "building",
                    "attempt": attempt,
                    "target_run_id": run_id,
                    "pending_rows": len(appended),
                    "skipped_rows": job["skipped_rows"] + len(received) - len(appended),
                    "message": "正在保留历史数据并计算新的价格汇总",
                },
            )
            report = build_snapshot(spark, store.root, merged, run_id, parent, store, job["id"])
            store.update_job(
                job["id"],
                {
                    "status": "succeeded",
                    "result_run_id": run_id,
                    "finished_at": now(),
                    "message": "增量入库和数据核对完成",
                    "verification_checks": len(report["checks"]),
                    "published_rows": job["expected_rows"],
                    "received_rows": len(received),
                    "inserted_rows": len(appended),
                },
            )
        except Exception as error:
            logger.exception("Import materialization failed")
            store.update_job(
                job["id"],
                {
                    "status": "failed",
                    "error_message": str(error)[:240],
                    "finished_at": now(),
                    "message": "任务失败，当前可用数据批次未被覆盖",
                },
            )
        finally:
            spark.catalog.clearCache()


def main():
    logging.basicConfig(level=logging.INFO)
    store = JobStore()
    owner = uuid.uuid4().hex
    while not store.worker_lease(owner, "starting"):
        time.sleep(2)
    store.recover()
    servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
    topic = os.getenv("MARKET_IMPORT_TOPIC", "price.imports.v1")
    ensure_topic(servers, topic)
    spark = (
        SparkSession.builder.appName("chengdu-continuous-imports")
        .config("spark.sql.shuffle.partitions", "2")
        .config("spark.sql.session.timeZone", "Asia/Shanghai")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("WARN")
    stop = threading.Event()
    phase = ["starting"]
    thread = threading.Thread(target=dispatch, args=(store, owner, stop, phase), daemon=True)
    thread.start()
    try:
        finish_ready(spark, store)
        incoming = (
            spark.readStream.format("kafka")
            .option("kafka.bootstrap.servers", servers)
            .option("subscribe", topic)
            .option("startingOffsets", "earliest")
            .option("maxOffsetsPerTrigger", 5000)
            .option("failOnDataLoss", "true")
            .load()
        )

        def consume(frame, _batch_id):
            envelopes = [
                json.loads(row.value)
                for row in frame.selectExpr("CAST(value AS STRING) value").collect()
            ]
            store.ingest(envelopes)
            finish_ready(spark, store)

        checkpoint = store.root / "stream/checkpoint"
        query = (
            incoming.writeStream.foreachBatch(consume)
            .option("checkpointLocation", str(checkpoint))
            .trigger(processingTime="2 seconds")
            .start()
        )
        phase[0] = "online"
        store.worker_lease(owner, "online")
        while not query.awaitTermination(5):
            if not thread.is_alive():
                raise RuntimeError("Publisher or worker heartbeat stopped; restarting safely")
    finally:
        stop.set()
        spark.stop()


if __name__ == "__main__":
    main()
