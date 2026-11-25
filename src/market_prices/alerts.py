"""Explainable review events derived from a pinned, read-only real quotation snapshot."""

import hashlib
import json
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

from market_prices.contracts import stable_id
from market_prices.warehouse import DataError

RULE_NAMES = {
    "large_weekly_change": "同市场周变动较大",
    "high_vs_peer_median": "高于同周市场中位数",
    "low_vs_peer_median": "低于同周市场中位数",
    "source_no_quote": "源文件无报价",
    "market_not_observed": "活跃规格缺市场记录",
    "low_market_coverage": "有效报价覆盖不足",
}


def rules():
    path = Path(__file__).resolve().parents[2] / "config/curation-policy.json"
    policy = json.loads(path.read_text(encoding="utf-8"))
    return {
        "version": "day6-v1",
        "policy_version": policy["version"],
        "policy_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "weekly_change_ratio": policy["review_flags"]["absolute_weekly_change_ratio"],
        "peer_high_ratio": policy["review_flags"]["cross_market_high_ratio"],
        "peer_low_ratio": policy["review_flags"]["cross_market_low_ratio"],
        "minimum_market_coverage": policy["forecast"]["min_market_coverage"],
        "names": RULE_NAMES,
    }


def make_event(rule, row, evidence, config, scope="market"):
    # Data batch and file position are provenance, not the identity of a review finding.
    identity = json.dumps(
        {
            "rule": rule,
            "rule_version": config["version"],
            "policy_sha256": config["policy_sha256"],
            "product_id": row["product_id"],
            "quote_date": str(row["quote_date"]),
            "market_id": row.get("market_id"),
            "evidence": evidence,
        },
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return {
        "id": stable_id([identity]),
        "rule": rule,
        "rule_name": RULE_NAMES[rule],
        "scope": scope,
        "quote_date": str(row["quote_date"]),
        **{
            name: row.get(name)
            for name in [
                "product_id",
                "product_name",
                "product_spec",
                "unit",
                "district",
                "market",
                "market_id",
                "price_raw",
                "source_filename",
                "source_row_number",
                "source_record_id",
                "source_file_sha256",
                "source_url",
                "product_spec_raw",
            ]
        },
        "evidence": {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in evidence.items()
        },
    }


def derive_events(snapshot, date_from, date_to, config):
    """Comparisons always use the same product/spec/unit and exact prior seven-day week."""
    change = Decimal(str(config["weekly_change_ratio"]))
    high, low = Decimal(str(config["peer_high_ratio"])), Decimal(str(config["peer_low_ratio"]))
    records = snapshot.rows(
        """WITH peers AS (
        SELECT product_id,quote_date,median(price)::DECIMAL(16,4) peer_median
        FROM quotes GROUP BY product_id,quote_date)
        SELECT q.*,prev.quote_date matched_previous_date,prev.price matched_previous_price,
            p.peer_median current_peer_median
        FROM quotes q LEFT JOIN quotes prev ON q.product_id=prev.product_id
            AND q.market_id=prev.market_id AND prev.quote_date=q.quote_date-INTERVAL '7 days'
        JOIN peers p ON p.product_id=q.product_id AND p.quote_date=q.quote_date
        WHERE q.quote_date BETWEEN ? AND ?
        AND (q.price IS NULL
            OR (prev.price>0 AND abs(q.price-prev.price)>=prev.price*?)
            OR (p.peer_median>0 AND (q.price>=p.peer_median*? OR q.price<=p.peer_median*?)))""",
        [date_from, date_to, change, high, low],
    )
    events = []
    for row in records:
        price, previous, median = (
            row["price"],
            row["matched_previous_price"],
            row["current_peer_median"],
        )
        if price is None:
            events.append(
                make_event(
                    "source_no_quote",
                    row,
                    {"price": None, "source_marker": row["price_raw"]},
                    config,
                )
            )
            continue
        if previous is not None and previous > 0 and abs(price - previous) >= previous * change:
            events.append(
                make_event(
                    "large_weekly_change",
                    row,
                    {
                        "price": price,
                        "previous_price": previous,
                        "previous_quote_date": str(row["matched_previous_date"]),
                        "change_ratio": float(price / previous - 1),
                        "threshold_ratio": float(change),
                    },
                    config,
                )
            )
        if median and median > 0:
            for name, hit, threshold in [
                ("high_vs_peer_median", price >= median * high, high),
                ("low_vs_peer_median", price <= median * low, low),
            ]:
                if hit:
                    events.append(
                        make_event(
                            name,
                            row,
                            {
                                "price": price,
                                "peer_median": median,
                                "peer_ratio": float(price / median),
                                "threshold_ratio": float(threshold),
                            },
                            config,
                        )
                    )
    coverage = snapshot.rows(
        """SELECT q.product_id,q.quote_date,p.product_name,p.product_spec,p.unit,
        count(*) observed_market_count,count(q.price) quoted_market_count,
        (SELECT count(*) FROM markets) expected_market_count
        FROM quotes q JOIN products p USING(product_id)
        WHERE quote_date BETWEEN ? AND ? GROUP BY ALL
        HAVING count(q.price)<ceil((SELECT count(*) FROM markets)*?)""",
        [date_from, date_to, config["minimum_market_coverage"]],
    )
    for row in coverage:
        events.append(
            make_event(
                "low_market_coverage",
                row,
                {
                    "quoted_markets": row["quoted_market_count"],
                    "observed_markets": row["observed_market_count"],
                    "expected_markets": row["expected_market_count"],
                    "minimum_ratio": config["minimum_market_coverage"],
                },
                config,
                scope="city",
            )
        )
    # Only a specification actually observed in this week is expected across the market panel.
    # This does not treat an entire discontinued/seasonal specification as missing quotes.
    absent = snapshot.rows(
        """WITH active AS (
        SELECT DISTINCT product_id,quote_date FROM quotes WHERE quote_date BETWEEN ? AND ?)
        SELECT a.product_id,a.quote_date,p.product_name,p.product_spec,p.unit,m.*
        FROM active a JOIN products p USING(product_id) CROSS JOIN markets m
        LEFT JOIN quotes q ON q.product_id=a.product_id AND q.quote_date=a.quote_date
            AND q.market_id=m.market_id WHERE q.product_id IS NULL""",
        [date_from, date_to],
    )
    for row in absent:
        events.append(
            make_event(
                "market_not_observed",
                row,
                {"price": None, "reason": "active_specification_has_no_market_record"},
                config,
            )
        )
    return sorted(
        events,
        key=lambda e: (
            -date.fromisoformat(e["quote_date"]).toordinal(),
            e["rule"],
            e["product_name"],
            e["product_spec"],
            e["district"] or "",
            e["market"] or "",
            e["id"],
        ),
    )


def select_events(events, product_id=None, district=None, market_id=None, rule=None):
    return [
        e
        for e in events
        if (not product_id or e["product_id"] == product_id)
        and (not district or e["district"] == district)
        and (not market_id or e["market_id"] == market_id)
        and (not rule or e["rule"] == rule)
    ]


def event_summary(events):
    reviewed = sum(e["review"]["reviewed"] for e in events)
    return {
        "matched_events": len(events),
        "open_events": len(events) - reviewed,
        "reviewed_events": reviewed,
        "affected_quotes": len({e["source_record_id"] for e in events if e["source_record_id"]}),
        "rule_counts": dict(Counter(e["rule"] for e in events)),
    }


def validate_window(snapshot, date_from, date_to):
    end = date_to or snapshot.meta.as_of
    start = date_from or end
    if start > end:
        raise DataError(422, "invalid_date_range", "开始日期不能晚于结束日期")
    return start, end
