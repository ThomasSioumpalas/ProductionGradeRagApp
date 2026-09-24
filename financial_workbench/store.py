"""SQLite persistence for a single local worker; jobs survive browser refreshes."""

import json
import re
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
            c.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS evidence USING fts5("
                "job_id UNINDEXED, document_id UNINDEXED, file UNINDEXED, "
                "page UNINDEXED, side UNINDEXED, kind UNINDEXED, content, "
                "tokenize='unicode61')"
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
            c.execute("DELETE FROM evidence WHERE job_id=?", (job_id,))

    def index_pages(self, job_id, pages):
        """Keep each PDF panel and every recognized row searchable across restarts."""
        records = []
        for page in pages:
            panels = page.get("panels") or [{"side": "full", "text": page["text"], "rows": []}]
            for panel in panels:
                records.append((job_id, page["sha256"], page["file"], page["page"],
                                panel["side"], "panel", panel["text"]))
                for row in panel.get("rows", []):
                    records.append((job_id, page["sha256"], page["file"], page["page"],
                                    panel["side"], "row", row["label"] + " " + row["quote"]))
        with self.connect() as c:
            c.execute("DELETE FROM evidence WHERE job_id=?", (job_id,))
            c.executemany(
                "INSERT INTO evidence(job_id,document_id,file,page,side,kind,content) VALUES(?,?,?,?,?,?,?)",
                records,
            )

    def has_index(self, job_id):
        with self.connect() as c:
            return c.execute("SELECT 1 FROM evidence WHERE job_id=? LIMIT 1", (job_id,)).fetchone() is not None

    def search(self, job_id, query, limit=8):
        # Quote all terms before they reach FTS MATCH. The query is data, never
        # SQLite FTS syntax, and the job ID is enforced in the same SQL query.
        terms = re.findall(r"[^\W_]+", query.casefold(), flags=re.UNICODE)[:12]
        aliases = {"sales": "revenue", "turnover": "revenue", "cashflow": "cash",
                   "έσοδα": "revenue", "πωλήσεις": "revenue", "κέρδη": "profit",
                   "ενεργητικό": "assets", "υποχρεώσεις": "liabilities", "ροές": "flows"}
        stopwords = {"what", "was", "were", "the", "a", "an", "for", "in", "of", "is",
                     "and", "how", "much", "did", "company", "year", "το", "τα", "της",
                     "για", "και", "ποιο", "πόσο"}
        words = list(dict.fromkeys([t for t in terms if t not in stopwords]
                                   + [aliases[t] for t in terms if t in aliases]))
        if not words:
            return []
        expression = " OR ".join('"' + word.replace('"', '""') + '"' for word in words)
        with self.connect() as c:
            rows = c.execute(
                "SELECT document_id,file,page,side,kind,"
                "snippet(evidence,6,'','',' … ',75),bm25(evidence) "
                "FROM evidence WHERE evidence MATCH ? AND job_id=? "
                "ORDER BY bm25(evidence) LIMIT ?",
                (expression, job_id, max(1, min(limit, 30))),
            ).fetchall()
        return [dict(zip(("document_id", "file", "page", "side", "kind", "text", "rank"), row))
                for row in rows]
