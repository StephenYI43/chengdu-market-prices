"""Shared ingestion contract. Missing quotes retain their original meaning."""

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def stable_id(parts: list[str]) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def normalized_spec(value: str) -> str:
    # Typographical normalization only. Actual brand/grade changes stay distinct.
    return re.sub(r"[\s，,、]", "", value)


class QuoteEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    event_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    business_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    batch_id: str
    mode: Literal["live", "historical_replay"]
    quote_date: date
    product_name: str = Field(min_length=1)
    product_spec: str = Field(min_length=1)
    unit: Literal["元/500克", "元/桶", "元/袋"]
    district: str = Field(min_length=1)
    market: str = Field(min_length=1)
    price_raw: str
    price: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=4)
    quote_status: Literal["quoted", "missing"]
    source_url: str
    source_file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    ingested_at: datetime

    @model_validator(mode="after")
    def check_price_status(self):
        if (self.quote_status == "missing") != (self.price is None):
            raise ValueError("Missing quotes must have null price; quoted records need a price")
        if self.ingested_at.tzinfo is None:
            raise ValueError("ingested_at must include a timezone")
        return self


class Readiness(BaseModel):
    status: Literal["ready", "not_ready"]
    kafka: bool
    detail: str | None = None
