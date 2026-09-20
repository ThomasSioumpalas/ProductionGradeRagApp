"""SQLite persistence for a single local worker; jobs survive browser refreshes."""

import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "jobs.sqlite3"
        with self.connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, updated REAL, payload TEXT)"
            )

    def connect(self):
        return sqlite3.connect(self.db, timeout=30)

    def get(self, job_id):
        with self.connect() as c:
            row = c.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, job):
        job["updated"] = time.time()
        with self.connect() as c:
            c.execute(
                "INSERT INTO jobs VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,payload=excluded.payload",
                (job["id"], job["updated"], json.dumps(job, ensure_ascii=False)),
            )

    def all(self):
        with self.connect() as c:
            return [
                json.loads(r[0])
                for r in c.execute("SELECT payload FROM jobs ORDER BY updated DESC")
            ]

    def delete(self, job_id):
        with self.connect() as c:
            c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
