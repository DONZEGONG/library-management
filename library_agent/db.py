"""Database connection and schema (PLAN 1.1)."""
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    book_id         TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    author          TEXT NOT NULL DEFAULT '',
    copy_number     INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'Available',
    condition_notes TEXT NOT NULL DEFAULT '',
    deleted_at      TEXT,
    deleted_by      TEXT,
    delete_reason   TEXT
);

-- Layer 4: only what library work needs. No passwords.
CREATE TABLE IF NOT EXISTS students (
    student_id    TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    grade         TEXT NOT NULL,
    contact_email TEXT NOT NULL DEFAULT '',
    deleted_at    TEXT,
    deleted_by    TEXT,
    delete_reason TEXT
);

CREATE TABLE IF NOT EXISTS loans (
    loan_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id       TEXT NOT NULL REFERENCES books(book_id),
    student_id    TEXT NOT NULL REFERENCES students(student_id),
    take_date     TEXT NOT NULL,
    due_date      TEXT NOT NULL,
    return_date   TEXT,
    return_condition TEXT,
    renewals      INTEGER NOT NULL DEFAULT 0,
    approved_by   TEXT NOT NULL,
    deleted_at    TEXT,
    deleted_by    TEXT,
    delete_reason TEXT
);

CREATE TABLE IF NOT EXISTS reservations (
    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id     TEXT NOT NULL REFERENCES books(book_id),
    student_id  TEXT NOT NULL REFERENCES students(student_id),
    reserved_on TEXT NOT NULL,
    expires_on  TEXT,            -- NULL while queued behind a borrowed copy
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS fines (
    fine_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_id  INTEGER NOT NULL REFERENCES loans(loan_id),
    reason   TEXT NOT NULL,
    amount   REAL NOT NULL,
    set_by   TEXT NOT NULL,
    status   TEXT NOT NULL DEFAULT 'unpaid'
             CHECK (status IN ('unpaid', 'paid', 'waived', 'reduced')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_actions (
    action_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    tool         TEXT NOT NULL,
    details      TEXT NOT NULL,   -- JSON
    suggested_by TEXT NOT NULL,
    reason       TEXT NOT NULL,
    flags        TEXT NOT NULL DEFAULT '[]',  -- JSON list of warnings for the human
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'approved', 'rejected')),
    decided_by   TEXT,
    decided_at   TEXT,
    decision_reason TEXT,
    session_id   TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS human_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    book_id     TEXT,
    student_id  TEXT,
    loan_id     INTEGER,
    summary     TEXT NOT NULL,
    decided_by  TEXT NOT NULL,
    decided_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS correction_log (
    correction_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    original_decision   TEXT NOT NULL,
    human_correction    TEXT NOT NULL,
    reason              TEXT NOT NULL,
    corrected_by        TEXT NOT NULL,
    date                TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS correction_log_no_update
BEFORE UPDATE ON correction_log
BEGIN SELECT RAISE(ABORT, 'correction log entries cannot be edited'); END;
CREATE TRIGGER IF NOT EXISTS correction_log_no_delete
BEFORE DELETE ON correction_log
BEGIN SELECT RAISE(ABORT, 'correction log entries cannot be deleted'); END;

CREATE TABLE IF NOT EXISTS notifications (
    notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    dedup_key  TEXT UNIQUE,      -- stops the daily job sending twice (PLAN 3.4)
    recipient  TEXT NOT NULL,
    subject    TEXT NOT NULL,
    body       TEXT NOT NULL,
    loan_id    INTEGER,
    status     TEXT NOT NULL DEFAULT 'sent' CHECK (status IN ('draft', 'sent', 'failed')),
    created_at TEXT NOT NULL,
    sent_at    TEXT
);

CREATE TABLE IF NOT EXISTS tool_calls (
    call_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    tool      TEXT NOT NULL,
    arguments TEXT NOT NULL,
    actor     TEXT NOT NULL,
    called_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id TEXT PRIMARY KEY,
    user_name  TEXT NOT NULL,
    messages   TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | None = None) -> sqlite3.Connection:
    from .config import settings

    conn = sqlite3.connect(path or settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
