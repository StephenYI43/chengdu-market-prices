"""Parse real CSV/XLSX quotations and classify duplicates without changing old prices."""

import csv
import hashlib
import io
import re
import zipfile
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from openpyxl import load_workbook

from market_prices.contracts import normalized_spec, stable_id
from market_prices.curation import RAW_COLUMNS, RAW_FIELDS, read_aliases
from market_prices.warehouse import DataError

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_ROWS = 100_000
HEADER_ALIASES = {
    "报价日期": "日期",
    "原始规格": "产品规格",
    "原始价格": "价格",
    "区分组": "区",
    "单位": "计量单位",
}


def text(value):
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    return str(value).strip()


def header(values):
    return [HEADER_ALIASES.get(text(value), text(value)) for value in values]


def supported(columns):
    return set(RAW_COLUMNS).issubset(columns) or (
        columns[:4] == RAW_COLUMNS[:4]
        and len(columns) > 4
        and all("·" in column for column in columns[4:])
    )


def expand(values, columns, origin):
    if len(columns) != len(set(columns)):
        raise DataError(422, "duplicate_headers", "文件存在重名列，请检查表头")
    long_format = set(RAW_COLUMNS).issubset(columns)
    if not supported(columns):
        raise DataError(
            422, "unsupported_headers", "未识别报价表头，请上传原始长表、宽表或本平台导出的CSV"
        )
    output = []
    for record_number, row in enumerate(values, start=2):
        if len(row) > len(columns) or (long_format and len(row) != len(columns)):
            raise DataError(422, "malformed_row", f"第{record_number}行列数与表头不一致")
        if not any(text(value) for value in row):
            continue
        mapped = dict(zip(columns, list(row) + [None] * (len(columns) - len(row))))
        if any(isinstance(value, str) and value.startswith("=") for value in mapped.values()):
            raise DataError(
                422, "formula_not_allowed", f"第{record_number}行含公式，请提供原始报价值"
            )
        variants = (
            [{key: text(mapped[key]) for key in RAW_COLUMNS}]
            if long_format
            else [
                {
                    **{key: text(mapped[key]) for key in RAW_COLUMNS[:4]},
                    "区": column.split("·", 1)[0],
                    "市场": column.split("·", 1)[1],
                    "价格": text(mapped[column]),
                }
                for column in columns[4:]
            ]
        )
        for variant in variants:
            output.append({"values": variant, "input_row": record_number, "sheet": origin})
            if len(output) > MAX_ROWS:
                raise DataError(413, "too_many_rows", f"单次最多导入{MAX_ROWS:,}条市场报价")
    if not output:
        raise DataError(422, "empty_file", "文件没有可读取的报价记录")
    return output


def parse_file(path: Path, sheet_name=None):
    if path.suffix.lower() == ".csv":
        content = path.read_bytes()
        encodings = (
            ["utf-16"] if content[:2] in (b"\xff\xfe", b"\xfe\xff") else ["utf-8-sig", "gb18030"]
        )
        decoded = None
        for encoding in encodings:
            try:
                decoded = content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if decoded is None:
            raise DataError(422, "invalid_encoding", "CSV编码无法识别，请保存为UTF-8 CSV")
        try:
            reader = csv.reader(io.StringIO(decoded, newline=""), strict=True)
            columns = header(next(reader))
            return expand(reader, columns, "CSV"), "CSV"
        except (StopIteration, csv.Error) as error:
            raise DataError(422, "invalid_csv", "CSV内容为空或引号格式不正确") from error
    if path.suffix.lower() != ".xlsx":
        raise DataError(415, "unsupported_file", "仅支持.csv和.xlsx文件")
    try:
        with zipfile.ZipFile(path) as archive:
            files = archive.infolist()
            if len(files) > 512 or sum(item.file_size for item in files) > 100 * 1024 * 1024:
                raise DataError(413, "xlsx_too_large", "Excel解压后内容过大")
            for item in files:
                if item.filename.endswith(".xml"):
                    with archive.open(item) as part:
                        if b"<!DOCTYPE" in part.read(65536).upper():
                            raise DataError(422, "unsafe_xml", "Excel包含不支持的XML声明")
        workbook = load_workbook(path, read_only=True, data_only=False, keep_links=False)
        try:
            candidates = []
            for sheet in workbook:
                if sheet_name and sheet.title != sheet_name:
                    continue
                columns = header(next(sheet.iter_rows(values_only=True), ()))
                if supported(columns):
                    candidates.append((sheet, columns))
            preferred = [item for item in candidates if item[0].title == "长表"]
            if preferred and not sheet_name:
                candidates = preferred
            if len(candidates) != 1:
                message = (
                    "未找到指定的报价工作表"
                    if not candidates
                    else "检测到多个报价工作表，请填写工作表名称"
                )
                raise DataError(422, "ambiguous_worksheet", message)
            sheet, columns = candidates[0]
            result = expand(sheet.iter_rows(min_row=2, values_only=True), columns, sheet.title)
            return result, sheet.title
        finally:
            workbook.close()
    except (zipfile.BadZipFile, KeyError, ValueError) as error:
        raise DataError(422, "invalid_xlsx", "Excel文件损坏或格式无法识别") from error


def price_value(raw):
    if raw == "-":
        return None
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,4})?", raw):
        raise ValueError("价格须为正数或无报价标记“-”")
    try:
        value = Decimal(raw)
        if not value.is_finite() or not 0 < value < 100_000_000:
            raise ValueError("价格不在允许范围")
        return str(value.quantize(Decimal("0.0001")))
    except InvalidOperation as error:
        raise ValueError("价格格式不正确") from error


def validate_records(parsed, source_hash, filename, source_url, config_root: Path):
    aliases = {
        (r["product_name_raw"], r["product_spec_raw"], r["unit_raw"]): r
        for r in read_aliases(config_root / "product-aliases.csv")
    }
    with (config_root / "markets.csv").open(encoding="utf-8", newline="") as file:
        markets = {(r["district"], r["market"]) for r in csv.DictReader(file)}
    result = []
    for number, item in enumerate(parsed, start=1):
        row = item["values"]
        errors = []
        if any(not row[key] for key in RAW_COLUMNS):
            errors.append("存在空的必填字段，无报价请用“-”")
        try:
            day = date.fromisoformat(row["日期"])
            if day.isoformat() != row["日期"] or day.weekday() != 1:
                errors.append("报价日期须为YYYY-MM-DD格式的周二报价期")
        except ValueError:
            errors.append("报价日期不合法")
        alias = aliases.get((row["商品名称"], row["产品规格"], row["计量单位"]))
        if alias is None:
            errors.append("商品、原始规格或单位未登记，请核对映射")
        if (row["区"], row["市场"]) not in markets:
            errors.append("区分组与市场组合未登记")
        try:
            price = price_value(row["价格"])
        except ValueError as error:
            errors.append(str(error))
            price = None
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
        raw_record = {
            **dict(zip(RAW_FIELDS, (row[key] for key in RAW_COLUMNS))),
            "source_row_number": number,
            "source_record_id": stable_id([source_hash, str(number)]),
            "source_file_sha256": source_hash,
            "source_filename": filename,
            "source_url": source_url,
            "business_key": key,
            "event_id": stable_id([source_hash, key, row["价格"]]),
        }
        result.append(
            {
                "record": raw_record,
                "price": price,
                "errors": errors,
                "input_row": item["input_row"],
                "sheet": item["sheet"],
                "product_id": alias["product_id"] if alias else None,
            }
        )
    return result


def classify(records, existing):
    groups = defaultdict(list)
    for row in records:
        row["classification"] = "invalid" if row["errors"] else "pending"
        if not row["errors"]:
            groups[row["record"]["business_key"]].append(row)
    for key, group in groups.items():
        if len({r["price"] for r in group}) > 1:
            for row in group:
                row["classification"] = "conflict"
                row["errors"] = ["同一文件内的相同业务键出现不同价格或报价状态"]
            continue
        first, *duplicates = group
        for row in duplicates:
            row["classification"] = "file_duplicate"
        if key in existing:
            if first["price"] == existing[key]:
                first["classification"] = "existing_duplicate"
            else:
                first["classification"] = "conflict"
                first["errors"] = ["报价与已入库历史记录冲突，不会自动覆盖"]
        else:
            first["classification"] = "new"
    counts = {
        name: sum(r["classification"] == name for r in records)
        for name in ["new", "existing_duplicate", "file_duplicate", "conflict", "invalid"]
    }
    return counts


def config_hashes(root):
    return {
        key: hashlib.sha256((root / filename).read_bytes()).hexdigest()
        for key, filename in [
            ("alias_file_sha256", "product-aliases.csv"),
            ("policy_file_sha256", "curation-policy.json"),
            ("market_file_sha256", "markets.csv"),
        ]
    }
