"""Read original data without changing it; create one real-week fixture and provenance."""

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    content = args.source.read_bytes()
    with args.source.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    dates = sorted({row["日期"] for row in rows})
    latest = dates[-1]
    sample = [row for row in rows if row["日期"] == latest]
    folder = Path(__file__).resolve().parents[1] / "data/samples"
    folder.mkdir(parents=True, exist_ok=True)
    sample_path = folder / "latest_week.csv"
    with sample_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(sample)
    missing = sum(row["价格"].strip() == "-" for row in rows)
    numeric = [Decimal(row["价格"]) for row in rows if row["价格"].strip() != "-"]
    if any(value <= 0 for value in numeric):
        raise ValueError("Non-positive source prices need review")
    manifest = {
        "source_filename": args.source.name,
        "source_url": "https://cddrc.chengdu.gov.cn/cdsfzggw/scjg/pricelist.shtml",
        "source_provenance": "用户提供的采集结果，文件说明标注来源为成都市发改委；尚未逐期对照官网",
        "source_file_sha256": hashlib.sha256(content).hexdigest(),
        "date_start": dates[0],
        "date_end": latest,
        "period_count": len(dates),
        "frequency": "weekly",
        "total_rows": len(rows),
        "quoted_rows": len(numeric),
        "missing_rows": missing,
        "date_gap_days": dict(
            Counter(
                (date.fromisoformat(b) - date.fromisoformat(a)).days
                for a, b in zip(dates, dates[1:])
            )
        ),
        "market_count": len({(row["区"], row["市场"]) for row in rows}),
        "district_count": len({row["区"] for row in rows}),
        "product_names_count": len({row["商品名称"] for row in rows}),
        "raw_spec_count": len(
            {(row["商品名称"], row["产品规格"], row["计量单位"]) for row in rows}
        ),
        "units": dict(Counter(row["计量单位"] for row in rows)),
        "sample_quote_date": latest,
        "sample_rows": len(sample),
        "sample_missing": sum(row["价格"].strip() == "-" for row in sample),
        "sample_file_sha256": hashlib.sha256(sample_path.read_bytes()).hexdigest(),
        "sample_mode": "historical_replay",
        "sample_note": "按原记录抽取完整一期，未随机生成、插值或改写任何价格",
    }
    (folder / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
