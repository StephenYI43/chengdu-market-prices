"""Durable import state, inbox and replayable UI events; no DuckDB file is mutated."""

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from market_prices.curation import write_json
from market_prices.warehouse import DataError

TERMINAL = {"succeeded", "no_op", "failed"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS previews(id TEXT PRIMARY KEY, document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,status TEXT NOT NULL,document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inbox(job_id TEXT,event_id TEXT,payload TEXT NOT NULL,
  PRIMARY KEY(job_id,event_id));
CREATE TABLE IF NOT EXISTS seals(job_id TEXT PRIMARY KEY,document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,
  job_id TEXT,document TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS leases(name TEXT PRIMARY KEY,owner TEXT NOT NULL,
  heartbeat REAL NOT NULL,phase TEXT NOT NULL);
"""


def now():
    return datetime.now(UTC).isoformat()


def runtime_root():
    return Path(os.getenv("MARKET_RUNTIME_DIR", Path(__file__).resolve().parents[2] / "runtime"))


def public_job(document):
    return {
        key: value
        for key, value in document.items()
        if key not in {"expected_digest", "publisher_owner"}
    }


class JobStore:
    def __init__(self, root=None):
        self.root = Path(root) if root else runtime_root()
        self.path = self.root / "imports/state.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
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

    def event(self, db, kind, document, job_id=None):
        db.execute(
            "INSERT INTO events(kind,job_id,document,created_at) VALUES(?,?,?,?)",
            [kind, job_id, json.dumps(document, ensure_ascii=False), now()],
        )

    def save_preview(self, document):
        with self.connection(True) as db:
            db.execute(
                "INSERT INTO previews VALUES(?,?)",
                [document["preview_id"], json.dumps(document, ensure_ascii=False)],
            )

    def preview(self, preview_id):
        with self.connection() as db:
            row = db.execute("SELECT document FROM previews WHERE id=?", [preview_id]).fetchone()
        if not row:
            raise DataError(404, "preview_not_found", "预览不存在，请重新上传文件")
        return json.loads(row[0])

    def job(self, job_id):
        with self.connection() as db:
            row = db.execute("SELECT document FROM jobs WHERE id=?", [job_id]).fetchone()
        if not row:
            raise DataError(404, "job_not_found", "导入任务不存在")
        return json.loads(row[0])

    def jobs(self, limit=20):
        with self.connection() as db:
            return [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT document FROM jobs ORDER BY rowid DESC LIMIT ?", [limit]
                )
            ]

    def create_job(self, document):
        with self.connection(True) as db:
            existing = db.execute(
                "SELECT document FROM jobs WHERE id=?", [document["id"]]
            ).fetchone()
            if existing:
                return json.loads(existing[0]), False
            db.execute(
                "INSERT INTO jobs VALUES(?,?,?)",
                [document["id"], document["status"], json.dumps(document, ensure_ascii=False)],
            )
            self.event(db, "job.updated", public_job(document), document["id"])
        return document, True

    def update_job(self, job_id, changes, only_status=None):
        with self.connection(True) as db:
            row = db.execute("SELECT document FROM jobs WHERE id=?", [job_id]).fetchone()
            if not row:
                return None
            document = json.loads(row[0])
            if only_status and document["status"] not in only_status:
                return document
            document.update(changes)
            document["updated_at"] = now()
            db.execute(
                "UPDATE jobs SET status=?,document=? WHERE id=?",
                [document["status"], json.dumps(document, ensure_ascii=False), job_id],
            )
            self.event(db, "job.updated", public_job(document), job_id)
            return document

    def claim_publish(self, owner):
        with self.connection(True) as db:
            row = db.execute(
                "SELECT document FROM jobs WHERE status='queued' ORDER BY rowid LIMIT 1"
            ).fetchone()
            if not row:
                return None
            job = json.loads(row[0])
            job.update(
                status="publishing", publisher_owner=owner, updated_at=now(), started_at=now()
            )
            db.execute(
                "UPDATE jobs SET status=?,document=? WHERE id=?",
                [job["status"], json.dumps(job, ensure_ascii=False), job["id"]],
            )
            self.event(db, "job.updated", public_job(job), job["id"])
            return job

    def ingest(self, envelopes):
        touched = set()
        with self.connection(True) as db:
            for envelope in envelopes:
                job_id = envelope.get("job_id")
                row = db.execute("SELECT status FROM jobs WHERE id=?", [job_id]).fetchone()
                if not row or row[0] in TERMINAL:
                    continue
                if envelope.get("kind") == "quote":
                    record = envelope["record"]
                    db.execute(
                        "INSERT OR IGNORE INTO inbox VALUES(?,?,?)",
                        [
                            job_id,
                            record["event_id"],
                            json.dumps(record, ensure_ascii=False, sort_keys=True),
                        ],
                    )
                elif envelope.get("kind") == "seal":
                    db.execute(
                        "INSERT OR REPLACE INTO seals VALUES(?,?)", [job_id, json.dumps(envelope)]
                    )
                touched.add(job_id)
        for job_id in touched:
            with self.connection() as db:
                count = db.execute(
                    "SELECT count(*) FROM inbox WHERE job_id=?", [job_id]
                ).fetchone()[0]
            self.update_job(
                job_id, {"received_rows": count}, only_status={"publishing", "streaming"}
            )

    def ready_jobs(self):
        with self.connection() as db:
            rows = db.execute("""SELECT j.document FROM jobs j JOIN seals s ON j.id=s.job_id
                WHERE j.status IN ('streaming','publishing') ORDER BY j.rowid""").fetchall()
            return [json.loads(row[0]) for row in rows]

    def received_records(self, job_id):
        with self.connection() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT payload FROM inbox WHERE job_id=? ORDER BY event_id", [job_id]
                )
            ]

    def events(self, after=0, limit=100):
        with self.connection() as db:
            return [
                {"id": r["seq"], "event": r["kind"], "data": json.loads(r["document"])}
                for r in db.execute(
                    "SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", [after, limit]
                )
            ]

    def worker_lease(self, owner, phase="online"):
        stamp = time.time()
        with self.connection(True) as db:
            row = db.execute("SELECT * FROM leases WHERE name='stream'").fetchone()
            if row and row["owner"] != owner and stamp - row["heartbeat"] < 30:
                return False
            db.execute(
                "INSERT OR REPLACE INTO leases VALUES('stream',?,?,?)", [owner, stamp, phase]
            )
        return True

    def worker_status(self):
        with self.connection() as db:
            row = db.execute("SELECT * FROM leases WHERE name='stream'").fetchone()
        return {
            "online": bool(
                row and time.time() - row["heartbeat"] < 30 and row["phase"] == "online"
            ),
            "phase": row["phase"] if row and time.time() - row["heartbeat"] < 30 else "offline",
            "last_heartbeat": datetime.fromtimestamp(row["heartbeat"], UTC).isoformat()
            if row
            else None,
        }

    def recover(self):
        for job in self.jobs(1000):
            if job["status"] == "publishing":
                self.update_job(
                    job["id"],
                    {"status": "queued", "recovery_count": job.get("recovery_count", 0) + 1},
                )
            elif job["status"] in {"building", "verifying"}:
                self.update_job(
                    job["id"],
                    {"status": "streaming", "recovery_count": job.get("recovery_count", 0) + 1},
                )


def publish_snapshot(root, run_id, source_hash, expected_parent):
    store = JobStore(root)
    pointer = Path(root) / "warehouse/current.json"
    with store.connection(True) as db:
        current = json.loads(pointer.read_text(encoding="utf-8")) if pointer.exists() else None
        actual_parent = current["run_id"] if current else None
        if actual_parent != expected_parent:
            raise DataError(409, "snapshot_changed", "数据批次已变化，本次不会覆盖，请重新运行任务")
        document = {
            "run_id": run_id,
            "relative_path": f"runs/{run_id}",
            "source_file_sha256": source_hash,
            "verified": True,
        }
        temporary = pointer.with_name(f"current-{run_id}.tmp")
        write_json(temporary, document)
        temporary.replace(pointer)
        store.event(db, "snapshot.published", document)
