# coding: utf-8
"""Watermark + rate-limit state store for the capture pipeline.

A tiny, DEDICATED sqlite file (default db/capture-state.db, configurable via
[capture].watermark_store) — deliberately NOT a table inside engine.db, so the
tailer never contends with the daemon's WAL writer and capture state has
zero migration coupling to the corpus schema. Same resumability contract as
the crag Anchor daemon itself: restart-proof, per-file byte-offset watermark.

House style: pure functions taking a resolved db path (opened per-call,
short-lived connections — this is a low-frequency, single-writer store, no
need for a persistent connection pool).
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS capture_watermarks (
    file_path   TEXT PRIMARY KEY,
    byte_offset INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capture_rate_limit (
    session     TEXT NOT NULL,
    run_date    TEXT NOT NULL,   -- UTC YYYY-MM-DD
    emitted     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session, run_date)
);
CREATE TABLE IF NOT EXISTS capture_emitted (
    span_id     TEXT PRIMARY KEY,   -- dedup: never re-extract the same span
    session     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def get_watermark(db_path: str, file_path: str) -> int:
    """Byte offset already processed for `file_path`. 0 if never seen."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT byte_offset FROM capture_watermarks WHERE file_path=?", (file_path,)
        ).fetchone()
        return int(row["byte_offset"]) if row else 0
    finally:
        conn.close()


def set_watermark(db_path: str, file_path: str, byte_offset: int) -> None:
    conn = _connect(db_path)
    try:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn.execute(
            "INSERT INTO capture_watermarks (file_path, byte_offset, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(file_path) DO UPDATE SET byte_offset=excluded.byte_offset, "
            "updated_at=excluded.updated_at",
            (file_path, byte_offset, now),
        )
        conn.commit()
    finally:
        conn.close()


def span_already_processed(db_path: str, span_id: str) -> bool:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM capture_emitted WHERE span_id=?", (span_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def mark_span_processed(db_path: str, span_id: str, session: str) -> None:
    conn = _connect(db_path)
    try:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn.execute(
            "INSERT OR IGNORE INTO capture_emitted (span_id, session, created_at) "
            "VALUES (?, ?, ?)",
            (span_id, session, now),
        )
        conn.commit()
    finally:
        conn.close()


def rate_limit_check_and_increment(db_path: str, session: str, budget: int) -> bool:
    """Anti-storm per-session-per-UTC-day emit budget. Returns True if the
    caller MAY emit one more candidate for `session` today (and increments
    the counter atomically); False if the budget is already exhausted."""
    conn = _connect(db_path)
    try:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        row = conn.execute(
            "SELECT emitted FROM capture_rate_limit WHERE session=? AND run_date=?",
            (session, today),
        ).fetchone()
        emitted = int(row["emitted"]) if row else 0
        if emitted >= budget:
            return False
        conn.execute(
            "INSERT INTO capture_rate_limit (session, run_date, emitted) VALUES (?, ?, 1) "
            "ON CONFLICT(session, run_date) DO UPDATE SET emitted=emitted+1",
            (session, today),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def rate_limit_remaining(db_path: str, session: str, budget: int) -> int:
    conn = _connect(db_path)
    try:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        row = conn.execute(
            "SELECT emitted FROM capture_rate_limit WHERE session=? AND run_date=?",
            (session, today),
        ).fetchone()
        emitted = int(row["emitted"]) if row else 0
        return max(0, budget - emitted)
    finally:
        conn.close()
