"""Read-only final checks for the running project and the submitted DOCX report."""

import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def sha(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18880")
    parser.add_argument("--report-docx", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/day7/verification.json")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    pointer_path = ROOT / "runtime/warehouse/current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    database = ROOT / "runtime/warehouse" / pointer["relative_path"] / "warehouse.duckdb"
    before = {str(p.relative_to(ROOT)): sha(p) for p in [pointer_path, database]}

    def get(path, binary=False):
        with urlopen(base + path, timeout=15) as response:
            body = response.read()
        return body if binary else json.loads(body)

    checks = {}
    live = get("/health/live")
    ready = get("/health/ready")
    checks["api_live_and_ready"] = bool(live) and bool(ready)
    status = get("/api/v1/system/status")
    checks["four_actual_service_checks_are_ready"] = all(
        [
            status["kafka"]["online"],
            status["stream"]["online"],
            status["forecast_service"]["online"],
            status["current_model"] is not None,
        ]
    )
    checks["source_business_counts_match_report"] = (
        status["dataset"]["totals"]["source_rows"] == 75726
        and status["dataset"]["totals"]["periods"] == 105
        and status["dataset"]["meta"]["as_of"] == "2026-11-24"
    )
    checks["model_matches_pinned_dataset_and_cutoff"] = (
        status["dataset"]["meta"]["run_id"] == pointer["run_id"]
        and status["current_model"]["as_of"] == status["dataset"]["meta"]["as_of"]
        and status["current_model"]["ready_products"] == 37
    )
    latest = get("/api/v1/alerts")
    history = get("/api/v1/alerts?date_from=2024-11-26&date_to=2026-11-24")
    checks["latest_and_history_alert_counts_match_verified_evidence"] = (
        latest["total"] == 3 and history["total"] == 896
    )
    html = get("/", binary=True).decode("utf-8")
    assets = sorted(set(re.findall(r'(?:src|href)="(/assets/[^\"]+)"', html)))
    checks["built_frontend_assets_are_served"] = bool(assets) and all(
        len(get(path, binary=True)) > 0 for path in assets
    )
    openapi = get("/openapi.json")
    checks["openapi_exposes_all_core_flows"] = all(
        any(prefix in path for path in openapi["paths"])
        for prefix in ["/prices", "/imports", "/forecasts", "/alerts", "/system/status"]
    )
    report = None
    if args.report_docx:
        with ZipFile(args.report_docx) as archive:
            document = ET.fromstring(archive.read("word/document.xml"))
            all_xml = [
                archive.read(name).decode("utf-8")
                for name in archive.namelist()
                if name.endswith(".xml")
            ]
            core_xml = archive.read("docProps/core.xml").decode("utf-8")
        text = "".join(node.text or "" for node in document.findall(".//w:t", NS))
        checks["report_has_supplied_students_and_teacher"] = all(
            value in text
            for value in [
                "宋珺熠", "2024110131", "何小柯", "2023070712", "文治民", "2021120343", "卢宇"
            ]
        )
        checks["report_has_five_required_sections"] = all(
            value in text
            for value in ["系统简介", "设计方案", "关键模块及代码说明", "效果展示", "心得体会总结"]
        )
        checks["report_metadata_has_current_group"] = all(
            name in core_xml for name in ["宋珺熠", "何小柯", "文治民"]
        )
        checks["report_has_no_old_roster_or_unfilled_slots"] = not any(
            value in "".join(all_xml)
            for value in [
                "谌泽轩", "熊浩然", "桂林秀", "杨显达", "刘镇涛", "待填写", "FIGSLOT", "TBLSLOT"
            ]
        )
        section = document.findall(".//w:sectPr", NS)
        checks["report_keeps_single_a4_portrait_section"] = len(section) == 1 and all(
            abs(int(section[0].find("w:pgSz", NS).get(f"{{{NS['w']}}}{key}")) - target) < 15
            for key, target in [("w", 11906), ("h", 16838)]
        )
        report = {
            "filename": args.report_docx.name,
            "sha256": sha(args.report_docx),
            "text_characters": len(text),
            "tables": len(document.findall(".//w:tbl", NS)),
            "drawings": len(document.findall(".//w:drawing", NS)),
        }
    checks["warehouse_pointer_and_database_unchanged"] = all(
        sha(ROOT / path) == digest for path, digest in before.items()
    )
    result = {
        "passed": all(checks.values()),
        "checked_at": datetime.now(UTC).isoformat(),
        "dataset_run_id": pointer["run_id"],
        "model_run_id": status["current_model"]["model_run_id"],
        "checks": checks,
        "report": report,
        "note": (
            "Read-only final checks; recovery/review mutation evidence is retained "
            "from earlier isolated verification."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
