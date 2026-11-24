"""A request owns one read-only connection to one verified immutable snapshot."""

import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path

import duckdb

from market_prices.curation import run_folder
from market_prices.query_models import DatasetMeta, Product

logger = logging.getLogger(__name__)
PRICE_COLUMNS = """quote_date,product_id,product_name,product_spec,product_spec_raw,unit,
district,market,market_id,price,price_raw,quote_status,missing_reason,review_flags,
source_row_number,source_filename,source_record_id"""
SORTS = {
    "date_desc": "quote_date DESC,district,market,source_row_number",
    "date_asc": "quote_date ASC,district,market,source_row_number",
    "price_asc": "price ASC NULLS LAST,quote_date DESC,district,market",
    "price_desc": "price DESC NULLS LAST,quote_date DESC,district,market",
}


class DataError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


class Snapshot:
    def __init__(self, db, report, stage):
        self.db, self.report = db, report
        self.meta = DatasetMeta(
            run_id=report["run_id"],
            date_start=report["summary"]["date_start"],
            as_of=report["summary"]["date_end"],
            source_file_sha256=report["source_file_sha256"],
            source_filename=stage["source_filename"],
            source_url=stage["source_url"],
        )

    def rows(self, sql: str, params=None):
        result = self.db.execute(sql, params or [])
        fields = [c[0] for c in result.description]
        return [dict(zip(fields, values)) for values in result.fetchall()]

    def scalar(self, sql, params=None):
        return self.db.execute(sql, params or []).fetchone()[0]

    def products(self, q="", category=None):
        return self.rows(
            """SELECT p.*,coalesce(f.eligible_baseline,false) eligible_baseline,
            p.last_observed_week=? AS current FROM products p
            LEFT JOIN forecast_profiles f USING(product_id)
            WHERE contains(lower(p.product_name || ' ' || p.product_spec),lower(?))
              AND (? IS NULL OR p.category=?) ORDER BY p.category,p.product_name,p.product_spec""",
            [self.meta.as_of, q, category, category],
        )

    def product(self, product_id):
        rows = self.rows(
            """SELECT p.*,coalesce(f.eligible_baseline,false) eligible_baseline,
            p.last_observed_week=? AS current FROM products p
            LEFT JOIN forecast_profiles f USING(product_id) WHERE product_id=?""",
            [self.meta.as_of, product_id],
        )
        if not rows:
            raise DataError(404, "product_not_found", "未找到该商品规格")
        return Product(**rows[0])

    def scope(self, district=None, market_id=None):
        if district and not self.scalar(
            "SELECT count(*) FROM districts WHERE district=?", [district]
        ):
            raise DataError(404, "district_not_found", "未找到该监测区分组")
        market = None
        if market_id:
            matches = self.rows("SELECT * FROM markets WHERE market_id=?", [market_id])
            if not matches:
                raise DataError(404, "market_not_found", "未找到该市场")
            market = matches[0]
            if district and market["district"] != district:
                raise DataError(422, "inconsistent_scope", "所选市场不属于所选区分组")
        label = (
            market["market"] if market else f"{district}监测市场" if district else "全部监测市场"
        )
        return label

    def filters(
        self,
        product_id,
        date_from=None,
        date_to=None,
        district=None,
        market_id=None,
        quote_status=None,
        review_only=False,
    ):
        self.scope(district, market_id)
        if date_from and date_to and date_from > date_to:
            raise DataError(422, "invalid_date_range", "开始日期不能晚于结束日期")
        conditions, params = ["product_id=?"], [product_id]
        for value, condition in [
            (date_from, "quote_date>=?"),
            (date_to, "quote_date<=?"),
            (district, "district=?"),
            (market_id, "market_id=?"),
            (quote_status, "quote_status=?"),
        ]:
            if value is not None:
                conditions.append(condition)
                params.append(value)
        if review_only:
            conditions.append("len(review_flags)>0")
        return " AND ".join(conditions), params

    def trends(self, product_id, date_from=None, date_to=None, district=None, market_id=None):
        self.scope(district, market_id)
        if date_from and date_to and date_from > date_to:
            raise DataError(422, "invalid_date_range", "开始日期不能晚于结束日期")
        if market_id:
            source = """SELECT quote_date,price mean_price,price median_price,price min_price,
                price max_price,
                CASE WHEN price IS NULL THEN NULL ELSE 0::DECIMAL(16,4) END price_range,
                1 observed_market_count,(price IS NOT NULL)::INTEGER quoted_market_count,
                (price IS NULL)::INTEGER missing_market_count,1 expected_market_count,
                false ranking_available,(price IS NOT NULL)::INTEGER::DOUBLE market_coverage
                FROM quotes WHERE product_id=? AND market_id=?"""
            params = [product_id, market_id]
        elif district:
            source = "SELECT * FROM district_weekly WHERE product_id=? AND district=?"
            params = [product_id, district]
        else:
            source = "SELECT * FROM product_weekly WHERE product_id=?"
            params = [product_id]
        common = (
            "common_market_wow_change,common_market_count"
            if not district and not market_id
            else "NULL::DOUBLE common_market_wow_change,0 common_market_count"
        )
        query = f"""WITH source AS ({source}), lagged AS (
            SELECT *,lag(quote_date) OVER(ORDER BY quote_date) prev_date,
              lag(mean_price) OVER(ORDER BY quote_date) prev_mean FROM source
        ) SELECT quote_date,mean_price,median_price,min_price,max_price,price_range,
            observed_market_count,quoted_market_count,missing_market_count,expected_market_count,
            expected_market_count-observed_market_count absent_market_count,
            ranking_available,market_coverage,
            CASE WHEN date_diff('day',prev_date,quote_date)=7 AND prev_mean>0
              THEN (mean_price/prev_mean)-1 ELSE NULL END wow_change,{common}
            FROM lagged WHERE (? IS NULL OR quote_date>=?) AND (? IS NULL OR quote_date<=?)
            ORDER BY quote_date"""
        result = self.rows(query, [*params, date_from, date_from, date_to, date_to])
        if district or market_id:
            where, scoped_params = self.filters(product_id, district=district, market_id=market_id)
            common_rows = self.rows(
                f"""WITH scoped AS (SELECT quote_date,market_id,price FROM quotes WHERE {where})
                SELECT cur.quote_date,count(*) common_market_count,
                    avg(cur.price)/avg(prev.price)-1 common_market_wow_change
                FROM scoped cur JOIN scoped prev ON cur.market_id=prev.market_id
                    AND prev.quote_date=cur.quote_date-INTERVAL '7 days'
                WHERE cur.price IS NOT NULL AND prev.price IS NOT NULL GROUP BY cur.quote_date""",
                scoped_params,
            )
            by_date = {row["quote_date"]: row for row in common_rows}
            for row in result:
                common_point = by_date.get(row["quote_date"])
                row["common_market_count"] = (
                    common_point["common_market_count"] if common_point else 0
                )
                row["common_market_wow_change"] = (
                    common_point["common_market_wow_change"] if common_point else None
                )
        return result

    def comparison(self, product_id, quote_date, district=None, market_id=None):
        self.scope(district, market_id)
        params = [product_id, quote_date, district, district, market_id, market_id]
        items = self.rows(
            """WITH scoped AS (
            SELECT m.*,q.price,q.price_raw,coalesce(q.quote_status,'not_observed') quote_status,
              q.source_row_number,coalesce(q.review_flags,[]::VARCHAR[]) review_flags
            FROM markets m LEFT JOIN quotes q ON m.market_id=q.market_id
              AND q.product_id=? AND q.quote_date=?
            WHERE (? IS NULL OR m.district=?) AND (? IS NULL OR m.market_id=?)
        ) SELECT *,CASE WHEN price IS NOT NULL AND count(price) OVER()>1
          THEN rank() OVER(ORDER BY price ASC NULLS LAST) ELSE NULL END rank
          FROM scoped ORDER BY price ASC NULLS LAST,district,market""",
            params,
        )
        where, conditions = self.filters(product_id, quote_date, quote_date, district, market_id)
        stats = self.rows(
            f"""SELECT count(*) observed_market_count,count(price) quoted_market_count,
            count(*) FILTER(WHERE quote_status='missing') missing_market_count,
            round(avg(price),4)::DECIMAL(16,4) mean_price,
            median(price)::DECIMAL(16,4) median_price,min(price) min_price,max(price) max_price,
            max(price)-min(price) price_range,count(price)>1 ranking_available
            FROM quotes WHERE {where}""",
            conditions,
        )[0]
        stats.update(
            {
                "expected_market_count": len(items),
                "absent_market_count": len(items) - stats["observed_market_count"],
                "market_coverage": stats["quoted_market_count"] / len(items) if items else 0,
            }
        )
        pair_where, pair_params = self.filters(product_id, district=district, market_id=market_id)
        common = self.rows(
            f"""WITH scoped AS (SELECT * FROM quotes WHERE {pair_where})
            SELECT count(*) common_market_count,
                avg(cur.price)/avg(prev.price)-1 common_market_wow_change
            FROM scoped cur JOIN scoped prev ON cur.market_id=prev.market_id
                AND prev.quote_date=cur.quote_date-INTERVAL '7 days'
            WHERE cur.quote_date=? AND cur.price IS NOT NULL AND prev.price IS NOT NULL""",
            [*pair_params, quote_date],
        )[0]
        return {"items": items, "summary": stats, **common}


@contextmanager
def open_snapshot(run_id=None):
    explicit_run = run_id is not None
    root = (
        Path(os.getenv("MARKET_RUNTIME_DIR", Path(__file__).resolve().parents[2] / "runtime"))
        / "warehouse"
    )
    try:
        if run_id is None:
            pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
            if pointer.get("verified") is not True:
                raise ValueError("Current warehouse is unverified")
            run_id = pointer["run_id"]
            if pointer["relative_path"] != f"runs/{run_id}":
                raise ValueError("Current warehouse pointer is inconsistent")
        folder = run_folder(root, run_id).resolve()
        if not folder.is_relative_to((root / "runs").resolve()):
            raise ValueError("Snapshot path is outside warehouse")
        if not folder.is_dir():
            if explicit_run:
                raise DataError(404, "snapshot_not_found", "该数据批次不存在")
            raise DataError(503, "warehouse_unavailable", "当前数据批次不可用，请检查已验收数据")
        report = json.loads((folder / "verification.json").read_text(encoding="utf-8"))
        stage = json.loads((folder / "stage.json").read_text(encoding="utf-8"))
        if report.get("passed") is not True or report["run_id"] != run_id:
            raise ValueError("Snapshot verification is missing or mismatched")
        with duckdb.connect(str(folder / "warehouse.duckdb"), read_only=True) as db:
            metadata = db.execute("SELECT run_id,source_file_sha256 FROM build_metadata").fetchall()
            if metadata != [(run_id, report["source_file_sha256"])]:
                raise ValueError("Database metadata does not match verification")
            yield Snapshot(db, report, stage)
    except DataError:
        raise
    except (OSError, ValueError, KeyError, duckdb.Error) as exc:
        logger.error("Warehouse snapshot unavailable: %s", exc)
        raise DataError(
            503, "warehouse_unavailable", "数据仓库尚未就绪，请先完成全量清洗或检查已验收批次"
        ) from exc
