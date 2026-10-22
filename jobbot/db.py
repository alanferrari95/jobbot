"""SQLite store: jobs and their application status, AI news, LinkedIn posts, a task queue and run logs."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

from .config import apps_dir, db_path
from .sources import Job

STATUSES = ["new", "packed", "ready", "later", "applied", "interview", "offer", "rejected", "skipped"]
POST_STATUSES = ["draft", "approved", "published", "rejected"]
# Screening verdicts (jobbot/screening.py). ok_manual = the candidate checked the job and OKed it by hand.
VERDICTS = ["verified", "location_unclear", "unverified", "discarded", "ok_manual"]
_OLD_VERDICTS = {"verificada": "verified", "ubicacion_dudosa": "location_unclear", "sin_verificar": "unverified",
                 "descartada": "discarded", "ok_alan": "ok_manual"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source TEXT, company TEXT, title TEXT, location TEXT,
    url TEXT, apply_url TEXT, remote INTEGER,
    score REAL, reasons TEXT, missing TEXT,
    status TEXT DEFAULT 'new', note TEXT DEFAULT '',
    description TEXT, extra TEXT,
    first_seen TEXT, updated TEXT, applied_at TEXT,
    dedup_key TEXT,
    verdict TEXT, verdict_reasons TEXT, verified_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_dedup ON jobs(dedup_key);

CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY, source TEXT, title TEXT, url TEXT, summary TEXT,
    published TEXT, points INTEGER, first_seen TEXT, used INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT,                -- personal | frontier (frontier = disclosed as written by Claude Code)
    topic TEXT, text TEXT, sources TEXT, news_id TEXT,
    status TEXT DEFAULT 'draft',
    scheduled_for TEXT, published_at TEXT, created TEXT, updated TEXT, note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT,                -- tailor | draft_post
    ref TEXT, status TEXT DEFAULT 'pending', note TEXT DEFAULT '', created TEXT, updated TEXT,
    UNIQUE(kind, ref)
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT, author TEXT, url TEXT UNIQUE, text TEXT,
    emails TEXT, links TEXT, how TEXT, status TEXT DEFAULT 'new', note TEXT DEFAULT '',
    created TEXT, updated TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT, started TEXT, finished TEXT, summary TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    apps_dir().mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path(), timeout=30)
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    if cols and "dedup_key" not in cols:  # migrate DBs created before dedup existed
        con.execute("ALTER TABLE jobs ADD COLUMN dedup_key TEXT")
    for col in ("verdict", "verdict_reasons", "verified_at"):  # screening layer (jobbot/screening.py)
        if cols and col not in cols:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT")
    con.executescript(SCHEMA)
    if con.execute("PRAGMA user_version").fetchone()[0] < 1:
        _migrate_v1(con)
    if con.execute("SELECT 1 FROM jobs WHERE verdict IN (%s) LIMIT 1" % ",".join("?" * len(_OLD_VERDICTS)),
                   list(_OLD_VERDICTS)).fetchone():  # verdicts were stored in Spanish before v0.2
        con.executemany("UPDATE jobs SET verdict = ? WHERE verdict = ?", [(v, k) for k, v in _OLD_VERDICTS.items()])
        con.commit()
    return con


def _migrate_v1(con: sqlite3.Connection) -> None:
    """v0.2: dedup keys were computed with a regex that never matched; recompute them once."""
    rows = con.execute("SELECT id, company, title FROM jobs").fetchall()
    con.executemany("UPDATE jobs SET dedup_key = ? WHERE id = ?",
                    [(dedup_key(r["company"] or "", r["title"] or ""), r["id"]) for r in rows])
    con.execute("PRAGMA user_version = 1")
    con.commit()


# ------------------------------------------------------------------ jobs

def dedup_key(company: str, title: str) -> str:
    """Same company + same normalized title = same job, whatever board it came from."""
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    title = re.sub(r"\b(remote|latam|worldwide)\b", "", title, flags=re.I)
    return f"{norm(company)}|{norm(title)}"


def upsert(con: sqlite3.Connection, job: Job, score: float, reasons: list[str], missing: list[str]) -> bool:
    """Insert a new job or refresh an existing one. Returns True if it was new.
    A job already seen under another id (same company + title) is not inserted again."""
    key = dedup_key(job.company, job.title)
    row = con.execute("SELECT id, status FROM jobs WHERE id = ?", (job.id,)).fetchone()
    fields = dict(
        source=job.source, company=job.company, title=job.title, location=job.location,
        url=job.url, apply_url=job.apply_url, remote=int(job.remote), score=score,
        reasons="; ".join(reasons), missing=", ".join(missing), description=job.description,
        extra=json.dumps(job.extra), updated=now(), dedup_key=key,
    )
    if row:
        if row["status"] != "new":  # prepared jobs keep their (possibly corrected) application URL
            fields.pop("apply_url")
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        con.execute(f"UPDATE jobs SET {sets} WHERE id = :id", {**fields, "id": job.id})
        return False
    if con.execute("SELECT 1 FROM jobs WHERE dedup_key = ?", (key,)).fetchone():
        return False
    fields.update(id=job.id, first_seen=now(), status="new")
    cols = ", ".join(fields)
    con.execute(f"INSERT INTO jobs ({cols}) VALUES ({', '.join(':' + k for k in fields)})", fields)
    return True


def get(con: sqlite3.Connection, job_id: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        matches = con.execute("SELECT * FROM jobs WHERE id LIKE ?", (f"%{job_id}%",)).fetchall()
        if len(matches) == 1:
            return matches[0]
        raise SystemExit(f"Job not found (or ambiguous): {job_id}")
    return row


def set_status(con: sqlite3.Connection, job_id: str, status: str, note: str = "") -> None:
    if status not in STATUSES:
        raise SystemExit(f"Unknown status {status!r}. Use one of: {', '.join(STATUSES)}")
    extra = ", applied_at = :t" if status == "applied" else ""
    con.execute(
        f"UPDATE jobs SET status = :s, note = CASE WHEN :n = '' THEN note ELSE :n END, updated = :t{extra} WHERE id = :id",
        {"s": status, "n": note, "t": now(), "id": job_id},
    )
    con.commit()


# ------------------------------------------------------------------ news / posts / tasks / runs

def add_news(con: sqlite3.Connection, item: dict) -> bool:
    nid = hashlib.sha1(item["url"].encode()).hexdigest()[:12]
    if con.execute("SELECT 1 FROM news WHERE id = ?", (nid,)).fetchone():
        return False
    con.execute("INSERT INTO news (id, source, title, url, summary, published, points, first_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (nid, item["source"], item["title"], item["url"], item["summary"], item["published"],
                 item.get("points", 0), now()))
    return True


def enqueue(con: sqlite3.Connection, kind: str, ref: str) -> bool:
    cur = con.execute("INSERT OR IGNORE INTO tasks (kind, ref, created, updated) VALUES (?, ?, ?, ?)",
                      (kind, ref, now(), now()))
    con.commit()
    return cur.rowcount > 0


def finish_task(con: sqlite3.Connection, kind: str, ref: str, status: str = "done", note: str = "") -> None:
    con.execute("UPDATE tasks SET status = ?, note = ?, updated = ? WHERE kind = ? AND ref = ?",
                (status, note, now(), kind, ref))
    con.commit()


def add_post(con: sqlite3.Connection, kind: str, topic: str, text: str, sources: list[str],
             news_id: str | None = None) -> int:
    cur = con.execute("INSERT INTO posts (kind, topic, text, sources, news_id, created, updated) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (kind, topic, text, json.dumps(sources), news_id, now(), now()))
    if news_id:
        con.execute("UPDATE news SET used = 1 WHERE id = ?", (news_id,))
    con.commit()
    return cur.lastrowid


def log_run(con: sqlite3.Connection, kind: str, started: str, summary: str) -> None:
    con.execute("INSERT INTO runs (kind, started, finished, summary) VALUES (?, ?, ?, ?)",
                (kind, started, now(), summary))
    con.commit()
