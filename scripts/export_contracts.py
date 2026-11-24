import json
from pathlib import Path

from market_prices.api import app
from market_prices.contracts import QuoteEvent

folder = Path(__file__).resolve().parents[1] / "contracts"
folder.mkdir(exist_ok=True)
for filename, content in [
    ("quote-event.schema.json", QuoteEvent.model_json_schema()),
    ("openapi.day1.json", app.openapi()),
]:
    (folder / filename).write_text(
        json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8"
    )
