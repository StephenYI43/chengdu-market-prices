"""Durable review notes and an append-only action history, separate from real prices."""

import json
import sqlite3
from contextlib import contextmanager

from market_prices.job_store import now, runtime_root
from market_prices.warehouse import DataError


class ReviewStore:
    def __init__(self):
        self.path = runtime_root() / "alerts/reviews.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""CREATE TABLE IF NOT EXISTS reviews(
                event_id TEXT PRIMARY KEY,reviewed INTEGER,note TEXT,version INTEGER,
                updated_at TEXT);
                CREATE TABLE IF NOT EXISTS actions(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT,run_id TEXT,
                reviewed INTEGER,note TEXT,version INTEGER,created_at TEXT,event_json TEXT);""")

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def decorate(self, events):
        with self.connection() as db:
            reviews = {r["event_id"]: dict(r) for r in db.execute("SELECT * FROM reviews")}
        for event in events:
            review = reviews.get(
                event["id"], {"reviewed": False, "note": "", "version": 0, "updated_at": None}
            )
            event["review"] = {
                key: bool(review[key]) if key == "reviewed" else review[key]
                for key in ["reviewed", "note", "version", "updated_at"]
            }
        return events

    def history(self, event_id):
        with self.connection() as db:
            return [
                {
                    **dict(row),
                    "reviewed": bool(row["reviewed"]),
                    "event": json.loads(row["event_json"]),
                }
                for row in db.execute(
                    "SELECT * FROM actions WHERE event_id=? ORDER BY seq DESC LIMIT 50", [event_id]
                )
            ]

    def update(self, event, run_id, reviewed, note, expected_version):
        with self.connection(True) as db:
            row = db.execute("SELECT * FROM reviews WHERE event_id=?", [event["id"]]).fetchone()
            current = (
                dict(row)
                if row
                else {"reviewed": False, "note": "", "version": 0, "updated_at": None}
            )
            if bool(current["reviewed"]) == reviewed and current["note"] == note:
                current["reviewed"] = bool(current["reviewed"])
                return {key: current[key] for key in ["reviewed", "note", "version", "updated_at"]}
            if current["version"] != expected_version:
                raise DataError(409, "review_changed", "复核记录已更新，请刷新详情后再操作")
            version, stamp = current["version"] + 1, now()
            db.execute(
                "INSERT OR REPLACE INTO reviews VALUES(?,?,?,?,?)",
                [event["id"], reviewed, note, version, stamp],
            )
            db.execute(
                """INSERT INTO actions(event_id,run_id,reviewed,note,version,created_at,event_json)
                VALUES(?,?,?,?,?,?,?)""",
                [
                    event["id"],
                    run_id,
                    reviewed,
                    note,
                    version,
                    stamp,
                    json.dumps(
                        {k: v for k, v in event.items() if k != "review"}, ensure_ascii=False
                    ),
                ],
            )
        return {"reviewed": reviewed, "note": note, "version": version, "updated_at": stamp}
