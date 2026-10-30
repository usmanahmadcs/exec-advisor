"""SQLite persistence layer.

SQLite (WAL mode) keeps the prototype dependency-free; every query goes through
this module, so swapping in PostgreSQL later only touches this file.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY,
    username        TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until    REAL NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY,
    filename        TEXT NOT NULL,
    title           TEXT NOT NULL,
    category        TEXT NOT NULL CHECK (category IN ('finance', 'hr', 'general', 'auto')),
    classification  TEXT NOT NULL CHECK (classification IN ('public', 'confidential')),
    status          TEXT NOT NULL DEFAULT 'processing'
                    CHECK (status IN ('processing', 'ready', 'failed')),
    error           TEXT,
    sha256          TEXT NOT NULL,
    stored_path     TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    num_chunks      INTEGER NOT NULL DEFAULT 0,
    effective_date  TEXT,
    period          TEXT,
    summary         TEXT,
    uploaded_by     TEXT NOT NULL,
    uploaded_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id              INTEGER PRIMARY KEY,
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ord             INTEGER NOT NULL,
    page            INTEGER,
    text            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chunks_doc ON chunks(document_id);

-- Structured facts extracted at ingest time (deterministic, no LLM involved)
CREATE TABLE IF NOT EXISTS financial_facts (
    id              INTEGER PRIMARY KEY,
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_id        INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    metric          TEXT NOT NULL,
    period          TEXT,
    value           TEXT NOT NULL,        -- Decimal stored as text: no float rounding
    currency        TEXT,
    raw             TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_facts (
    id              INTEGER PRIMARY KEY,
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_id        INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    topic           TEXT NOT NULL,
    value           TEXT NOT NULL,
    unit            TEXT NOT NULL,
    raw             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS automation_runs (
    id              INTEGER PRIMARY KEY,
    document_id     INTEGER,
    step            TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('success', 'failed', 'skipped')),
    attempts        INTEGER NOT NULL DEFAULT 1,
    detail          TEXT,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY,
    ts              REAL NOT NULL,
    username        TEXT,
    role            TEXT,
    action          TEXT NOT NULL,
    resource        TEXT,
    outcome         TEXT NOT NULL,       -- allowed | denied | error
    detail          TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log(ts);

CREATE TABLE IF NOT EXISTS queries (
    id              INTEGER PRIMARY KEY,
    ts              REAL NOT NULL,
    username        TEXT NOT NULL,
    advisor         TEXT NOT NULL,
    status          TEXT NOT NULL,
    question        TEXT NOT NULL,
    answer          TEXT NOT NULL,
    documents_used  TEXT NOT NULL,       -- JSON list of document ids
    flags           TEXT NOT NULL,       -- JSON: warnings/conflicts/missing
    latency_ms      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self.connect() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ generic
    def query(self, sql: str, params: tuple | list = ()) -> list[dict]:
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | list = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        with self.connect() as c:
            cur = c.execute(sql, params)
            return cur.lastrowid

    # ------------------------------------------------------------------ corpus version
    def corpus_version(self) -> int:
        row = self.one("SELECT value FROM meta WHERE key = 'corpus_version'")
        return int(row["value"]) if row else 0

    def bump_corpus_version(self) -> None:
        with self._lock, self.connect() as c:
            c.execute(
                "INSERT INTO meta(key, value) VALUES ('corpus_version', '1') "
                "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1"
            )

    # ------------------------------------------------------------------ audit / logs
    def audit(self, user: dict | None, action: str, resource: str | None, outcome: str,
              detail: dict | str | None = None) -> None:
        if isinstance(detail, dict):
            detail = json.dumps(detail, default=str)
        self.execute(
            "INSERT INTO audit_log(ts, username, role, action, resource, outcome, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), (user or {}).get("username"), (user or {}).get("role"), action,
             resource, outcome, detail),
        )

    def record_run(self, document_id: int | None, step: str, status: str, attempts: int,
                   detail: str = "") -> None:
        self.execute(
            "INSERT INTO automation_runs(document_id, step, status, attempts, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (document_id, step, status, attempts, detail[:2000], time.time()),
        )
