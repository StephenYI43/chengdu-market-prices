"""Publish a bounded, unchanged, real historical sample to an isolated topic."""

import argparse
import csv
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from confluent_kafka import Producer
from confluent_kafka.admin import AdminClient, NewTopic

from market_prices.contracts import QuoteEvent, normalized_spec, stable_id


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "data/samples/manifest.json").read_text(encoding="utf-8"))
    topic = f"price.day1.{args.run_id}"
    servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092")
    admin = AdminClient({"bootstrap.servers": servers})
    futures = admin.create_topics(
        [NewTopic(topic, num_partitions=1, replication_factor=1)], request_timeout=20
    )
    for future in futures.values():
        future.result(timeout=30)
    producer = Producer({"bootstrap.servers": servers, "enable.idempotence": True})
    events = []
    failures = []

    def delivered(error, _message):
        if error:
            failures.append(str(error))

    with (root / "data/samples/latest_week.csv").open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            key = stable_id(
                [
                    row["日期"],
                    row["商品名称"],
                    normalized_spec(row["产品规格"]),
                    row["计量单位"],
                    row["区"],
                    row["市场"],
                ]
            )
            raw = row["价格"].strip()
            event = QuoteEvent(
                event_id=stable_id([manifest["source_file_sha256"], key, raw]),
                business_key=key,
                batch_id=args.run_id,
                mode="historical_replay",
                quote_date=row["日期"],
                product_name=row["商品名称"],
                product_spec=row["产品规格"],
                unit=row["计量单位"],
                district=row["区"],
                market=row["市场"],
                price_raw=raw,
                price=None if raw == "-" else Decimal(raw),
                quote_status="missing" if raw == "-" else "quoted",
                source_url=manifest["source_url"],
                source_file_sha256=manifest["source_file_sha256"],
                ingested_at=datetime.now(UTC),
            )
            producer.produce(
                topic, key=key, value=event.model_dump_json().encode("utf-8"), callback=delivered
            )
            events.append(event.model_dump(mode="json"))
            producer.poll(0)
    remaining = producer.flush(30)
    if remaining or failures:
        raise RuntimeError(f"Unconfirmed messages={remaining}, delivery errors={failures}")
    quoted = [Decimal(event["price"]) for event in events if event["price"] is not None]
    expected = {
        "run_id": args.run_id,
        "topic": topic,
        "mode": "historical_replay",
        "rows": len(events),
        "quoted": len(quoted),
        "missing": len(events) - len(quoted),
        "business_keys": len({event["business_key"] for event in events}),
        "price_sum_control_only": str(sum(quoted)),
        "quote_date": manifest["sample_quote_date"],
        "event_ids": sorted(event["event_id"] for event in events),
        "records": events,
    }
    folder = root / "artifacts/day1" / args.run_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "published.json").write_text(
        json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {key: value for key, value in expected.items() if key not in {"event_ids", "records"}},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
