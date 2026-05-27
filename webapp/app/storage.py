"""SQLite storage for runs (M1).

Schema: single `runs` table. WAL mode (decision #40). Single-process writer,
multi-thread readers — matches uvicorn single-process + worker thread (decision
#15) + global single-worker queue (decision #4).

Replaces M0 in-memory dict. API surface stays similar so router code barely changes.
"""
from __future__ import annotations
import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from .config import DB_PATH

# One connection per thread (sqlite3 requires same-thread by default).
_local = threading.local()


def _conn() -> sqlite3.Connection:
    if not hasattr(_local, "c"):
        c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.c = c
    return _local.c


def init_db() -> None:
    """Idempotent schema setup. Call on app startup."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(DB_PATH)) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id        TEXT PRIMARY KEY,
                owner_email   TEXT NOT NULL,
                mpns_json     TEXT NOT NULL,
                mpns_hash     TEXT NOT NULL,
                status        TEXT NOT NULL,
                phase         TEXT,
                submitted_at  TEXT NOT NULL,
                started_at    TEXT,
                finished_at   TEXT,
                api_batch     TEXT,
                scraper_batch TEXT,
                merge_batch   TEXT,
                row_count     INTEGER,
                error_text    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_runs_owner ON runs (owner_email, submitted_at DESC);
            CREATE INDEX IF NOT EXISTS idx_runs_hash ON runs (mpns_hash, status);

            CREATE TABLE IF NOT EXISTS magic_links (
                token       TEXT PRIMARY KEY,
                email       TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                expires_at  TEXT NOT NULL,
                consumed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ml_email ON magic_links (email, expires_at);

            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                email      TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_seen_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sess_email ON sessions (email, expires_at);
        """)


def hash_mpns(mpns: list[str]) -> str:
    """Stable hash of sorted MPN set for 24h cache lookup (decision #18)."""
    sorted_mpns = sorted(set(m.strip() for m in mpns if m.strip()))
    blob = "\n".join(sorted_mpns).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def new_run(run_id: str, mpns: list[str], owner_email: str) -> dict:
    rec = {
        "run_id": run_id,
        "owner_email": owner_email,
        "mpns_json": json.dumps(mpns, ensure_ascii=False),
        "mpns_hash": hash_mpns(mpns),
        "status": "queued",
        "phase": None,
        "submitted_at": datetime.now().isoformat(timespec="seconds"),
        "started_at": None,
        "finished_at": None,
        "api_batch": None,
        "scraper_batch": None,
        "merge_batch": None,
        "row_count": None,
        "error_text": None,
    }
    c = _conn()
    c.execute(
        """INSERT INTO runs (run_id, owner_email, mpns_json, mpns_hash, status, submitted_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (rec["run_id"], rec["owner_email"], rec["mpns_json"], rec["mpns_hash"],
         rec["status"], rec["submitted_at"]),
    )
    c.commit()
    return rec


def get_run(run_id: str) -> dict | None:
    c = _conn()
    row = c.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["mpns"] = json.loads(d["mpns_json"])
    return d


def list_runs(owner_email: str, limit: int = 100) -> list[dict]:
    """Decision #24: only own runs."""
    c = _conn()
    rows = c.execute(
        "SELECT * FROM runs WHERE owner_email=? ORDER BY submitted_at DESC LIMIT ?",
        (owner_email, limit),
    ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["mpns"] = json.loads(d["mpns_json"])
        result.append(d)
    return result


def set_status(run_id: str, status: str, phase: str | None = None) -> None:
    c = _conn()
    if phase is not None:
        c.execute("UPDATE runs SET status=?, phase=? WHERE run_id=?", (status, phase, run_id))
    else:
        c.execute("UPDATE runs SET status=? WHERE run_id=?", (status, run_id))
    c.commit()


def mark_started(run_id: str) -> None:
    c = _conn()
    c.execute(
        "UPDATE runs SET status='running', started_at=? WHERE run_id=?",
        (datetime.now().isoformat(timespec="seconds"), run_id),
    )
    c.commit()


def mark_done(run_id: str, status: str, *, api_batch: str | None,
              scraper_batch: str | None, merge_batch: str | None,
              row_count: int) -> None:
    """status = 'done' or 'done_empty'."""
    c = _conn()
    c.execute(
        """UPDATE runs SET status=?, finished_at=?, api_batch=?, scraper_batch=?,
                          merge_batch=?, row_count=? WHERE run_id=?""",
        (status, datetime.now().isoformat(timespec="seconds"),
         api_batch, scraper_batch, merge_batch, row_count, run_id),
    )
    c.commit()


def mark_failed(run_id: str, error_text: str) -> None:
    c = _conn()
    c.execute(
        "UPDATE runs SET status='failed', finished_at=?, error_text=? WHERE run_id=?",
        (datetime.now().isoformat(timespec="seconds"), error_text, run_id),
    )
    c.commit()


# ---------- Magic Link / Sessions (decision #10) ----------

def new_magic_link(token: str, email: str, ttl_minutes: int) -> None:
    from datetime import timedelta
    now = datetime.now()
    expires = now + timedelta(minutes=ttl_minutes)
    c = _conn()
    c.execute(
        "INSERT INTO magic_links (token, email, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, email.lower(), now.isoformat(timespec="seconds"),
         expires.isoformat(timespec="seconds")),
    )
    c.commit()


def consume_magic_link(token: str) -> str | None:
    """Verify token unused + not expired; mark consumed. Return email or None."""
    c = _conn()
    row = c.execute(
        """SELECT email FROM magic_links
           WHERE token=? AND consumed_at IS NULL
             AND datetime(expires_at) >= datetime('now', 'localtime')""",
        (token,),
    ).fetchone()
    if row is None:
        return None
    c.execute(
        "UPDATE magic_links SET consumed_at=? WHERE token=?",
        (datetime.now().isoformat(timespec="seconds"), token),
    )
    c.commit()
    return row["email"]


def new_session(session_id: str, email: str, ttl_days: int) -> None:
    from datetime import timedelta
    now = datetime.now()
    expires = now + timedelta(days=ttl_days)
    c = _conn()
    c.execute(
        """INSERT INTO sessions (session_id, email, created_at, expires_at, last_seen_at)
           VALUES (?, ?, ?, ?, ?)""",
        (session_id, email.lower(),
         now.isoformat(timespec="seconds"),
         expires.isoformat(timespec="seconds"),
         now.isoformat(timespec="seconds")),
    )
    c.commit()


def lookup_session(session_id: str) -> str | None:
    """Return email if session valid; bump last_seen_at."""
    c = _conn()
    row = c.execute(
        """SELECT email FROM sessions
           WHERE session_id=?
             AND datetime(expires_at) >= datetime('now', 'localtime')""",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    c.execute(
        "UPDATE sessions SET last_seen_at=? WHERE session_id=?",
        (datetime.now().isoformat(timespec="seconds"), session_id),
    )
    c.commit()
    return row["email"]


def revoke_session(session_id: str) -> None:
    c = _conn()
    c.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
    c.commit()


def find_cached_run(mpns_hash: str, hours: int = 24) -> dict | None:
    """Decision #18 Option (a): same hash, status=done, within last N hours."""
    c = _conn()
    row = c.execute(
        """SELECT * FROM runs
           WHERE mpns_hash=? AND status IN ('done','done_empty')
             AND datetime(finished_at) >= datetime('now', ?, 'utc')
           ORDER BY finished_at DESC LIMIT 1""",
        (mpns_hash, f"-{hours} hours"),
    ).fetchone()
    return dict(row) if row else None
