"""Immutable uploads, source previews and idempotent confirmation."""

import hashlib
import json
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse

from market_prices.curation import SOURCE_URL, write_json
from market_prices.import_records import (
    MAX_UPLOAD_BYTES,
    classify,
    config_hashes,
    parse_file,
    validate_records,
)
from market_prices.job_store import JobStore, now
from market_prices.warehouse import DataError, open_snapshot


def payload_digest(records):
    body = json.dumps(
        sorted(records, key=lambda r: r["event_id"]),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


def preview_dir(store, preview_id):
    if not re.fullmatch(r"[a-f0-9]{32}", preview_id):
        raise DataError(422, "invalid_preview_id", "预览标识不合法")
    return store.root / "imports/uploads" / preview_id


def read_candidates(store, preview_id):
    path = preview_dir(store, preview_id) / "candidates.json"
    return json.loads(path.read_text(encoding="utf-8"))


def create_preview(content, filename, source_url=SOURCE_URL, sheet_name=None):
    store = JobStore()
    filename = Path(filename.replace("\\", "/")).name[:180]
    suffix = Path(filename).suffix.lower()
    if suffix not in {".csv", ".xlsx"}:
        raise DataError(415, "unsupported_file", "仅支持.csv和.xlsx文件")
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise DataError(413, "upload_size", "文件为空或超过20MB限制")
    source_url = source_url.strip() or SOURCE_URL
    parsed_url = urlparse(source_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc or parsed_url.username:
        raise DataError(422, "invalid_source_url", "来源链接须为有效的HTTP或HTTPS网址")
    preview_id = uuid.uuid4().hex
    folder = preview_dir(store, preview_id)
    folder.mkdir(parents=True, exist_ok=False)
    path = folder / ("original" + suffix)
    path.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    config_root = Path(__file__).resolve().parents[2] / "config"
    parsed, sheet = parse_file(path, sheet_name)
    validated = validate_records(parsed, sha, filename, source_url, config_root)
    with open_snapshot() as snapshot:
        existing = {
            r["business_key"]: str(r["price"]) if r["price"] is not None else None
            for r in snapshot.rows("SELECT business_key,price FROM quotes")
        }
        counts = classify(validated, existing)
        base_run = snapshot.meta.run_id
    candidates = [r["record"] for r in validated if r["classification"] == "new"]
    dates = sorted({r["record"]["quote_date_raw"] for r in validated if not r["errors"]})
    sample = [
        {
            "input_row": r["input_row"],
            "sheet": r["sheet"],
            "classification": r["classification"],
            "errors": r["errors"],
            "quote_date": r["record"]["quote_date_raw"],
            "product_name": r["record"]["product_name_raw"],
            "product_spec": r["record"]["product_spec_raw"],
            "unit": r["record"]["unit_raw"],
            "district": r["record"]["district_raw"],
            "market": r["record"]["market_raw"],
            "price_raw": r["record"]["price_raw"],
        }
        for r in validated
    ]
    errors = [r for r in sample if r["errors"]]
    document = {
        "preview_id": preview_id,
        "created_at": now(),
        "filename": filename,
        "file_sha256": sha,
        "file_bytes": len(content),
        "source_url": source_url,
        "sheet": sheet,
        "base_run_id": base_run,
        "source_rows": len(validated),
        "counts": counts,
        "date_start": dates[0] if dates else None,
        "date_end": dates[-1] if dates else None,
        "can_confirm": not counts["invalid"] and not counts["conflict"],
        "sample": sample[:20],
        "error_examples": errors[:30],
        "config_hashes": config_hashes(config_root),
        "candidate_digest": payload_digest(candidates),
    }
    write_json(folder / "candidates.json", candidates)
    write_json(folder / "validation.json", sample)
    store.save_preview(document)
    return document


def confirm_preview(preview_id):
    store = JobStore()
    preview = store.preview(preview_id)
    try:
        return store.job(preview_id)
    except DataError as error:
        if error.status != 404:
            raise
    if not preview["can_confirm"]:
        raise DataError(422, "preview_has_errors", "预览存在无效或冲突记录，请修正后重新上传")
    config_root = Path(__file__).resolve().parents[2] / "config"
    if config_hashes(config_root) != preview["config_hashes"]:
        raise DataError(409, "configuration_changed", "映射或规则已变化，请重新预览")
    with open_snapshot() as snapshot:
        if snapshot.meta.run_id != preview["base_run_id"]:
            raise DataError(409, "stale_preview", "数据批次已更新，请重新预览文件后确认")
    candidates = read_candidates(store, preview_id)
    original = next(preview_dir(store, preview_id).glob("original.*"))
    if (
        payload_digest(candidates) != preview["candidate_digest"]
        or hashlib.sha256(original.read_bytes()).hexdigest() != preview["file_sha256"]
    ):
        raise DataError(409, "upload_changed", "上传文件或预览内容已变化，请重新上传")
    status = "queued" if candidates else "no_op"
    document = {
        "id": preview_id,
        "preview_id": preview_id,
        "filename": preview["filename"],
        "source_file_sha256": preview["file_sha256"],
        "source_url": preview["source_url"],
        "created_at": now(),
        "updated_at": now(),
        "started_at": None,
        "finished_at": now() if not candidates else None,
        "status": status,
        "base_run_id": preview["base_run_id"],
        "result_run_id": None,
        "source_rows": preview["source_rows"],
        "expected_rows": len(candidates),
        "expected_digest": payload_digest(candidates),
        "published_rows": 0,
        "received_rows": 0,
        "inserted_rows": 0,
        "skipped_rows": preview["counts"]["existing_duplicate"]
        + preview["counts"]["file_duplicate"],
        "attempt": 0,
        "recovery_count": 0,
        "error_message": None,
        "message": "全部记录已存在，未重复入库" if not candidates else "等待持续流处理服务接收",
    }
    result, _created = store.create_job(document)
    return result
