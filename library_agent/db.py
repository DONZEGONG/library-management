"""Database connection and schema (PLAN 1.1).

Two databases are supported with the same tables:

- SQLite (the default): a single file, LIBRARY_DB (library.db).
- PostgreSQL: set DATABASE_URL to the connection string your provider gives
  you, for example postgresql://user:password@host:5432/library?sslmode=require

The rest of the app writes plain SQL with ? placeholders that works on both;
the PostgreSQL connection below translates the placeholders.
"""
import re
import sqlite3
from datetime import datetime, timezone

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    book_id         TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    author          TEXT NOT NULL DEFAULT '',
    category        TEXT NOT NULL DEFAULT '',
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

-- Committee sign-in passwords (AUTH_MODE=password). Only a salted hash is kept,
-- never the password itself.
-- Wrong-password counts, so the sign-in lock works across several servers.
CREATE TABLE IF NOT EXISTS login_attempts (
    attempt_key TEXT PRIMARY KEY,
    fails       INTEGER NOT NULL,
    since       REAL NOT NULL
);

-- Who added books, changed copy counts, or added and removed students (MINI SPEC 4).
CREATE TABLE IF NOT EXISTS activity_log (
    activity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_name   TEXT NOT NULL,
    action      TEXT NOT NULL,   -- BULK_ADD, UPDATE_COPIES, ADD_STUDENT, REMOVE_STUDENT, RESTORE_STUDENT
    details     TEXT NOT NULL,
    source      TEXT NOT NULL,   -- file, scan, voice or manual
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS member_passwords (
    member_key    TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    set_at        TEXT NOT NULL
);
"""

# Same tables for PostgreSQL. Dates stay as ISO text so every query and
# comparison behaves the same on both databases.
_ID = "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
POSTGRES_SCHEMA = (
    SQLITE_SCHEMA
    .split("CREATE TRIGGER IF NOT EXISTS correction_log_no_update")[0]
    + SQLITE_SCHEMA.split("BEGIN SELECT RAISE(ABORT, 'correction log entries cannot be deleted'); END;")[1]
).replace("INTEGER PRIMARY KEY AUTOINCREMENT", _ID).replace("REAL NOT NULL", "DOUBLE PRECISION NOT NULL") + """
CREATE OR REPLACE FUNCTION correction_log_locked() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'correction log entries cannot be edited';
    END IF;
    RAISE EXCEPTION 'correction log entries cannot be deleted';
END $$;
CREATE OR REPLACE TRIGGER correction_log_no_change
BEFORE UPDATE OR DELETE ON correction_log
FOR EACH ROW EXECUTE FUNCTION correction_log_locked();
"""

# Tables in the order they must be filled (foreign keys), and each table's
# auto-numbered column. Used by backup, restore and the move to PostgreSQL.
TABLES = ("books", "students", "loans", "reservations", "fines", "pending_actions",
          "human_decisions", "correction_log", "notifications", "tool_calls", "chat_sessions",
          "member_passwords", "activity_log")
AUTO_IDS = {"loans": "loan_id", "reservations": "reservation_id", "fines": "fine_id",
            "pending_actions": "action_id", "human_decisions": "decision_id",
            "correction_log": "correction_id", "notifications": "notification_id",
            "tool_calls": "call_id", "activity_log": "activity_id"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_postgres(target: str) -> bool:
    return str(target).startswith(("postgres://", "postgresql://"))


def describe(target: str) -> str:
    """The database location with any password hidden, safe to print or log."""
    return re.sub(r"(//[^:/@]+):[^@]*@", r"\1:***@", str(target))


# ---------------------------------------------------------------- SQLite

class SqliteConnection(sqlite3.Connection):
    dialect = "sqlite"


# ---------------------------------------------------------------- PostgreSQL

class Row(dict):
    """A result row readable by column name (row["name"]) or position
    (row[0]), like sqlite3.Row."""

    def __init__(self, names, values):
        super().__init__(zip(names, values))
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


def _row_factory(cursor):
    names = [c.name for c in cursor.description] if cursor.description else []
    return lambda values: Row(names, values)


def _to_pg(sql: str) -> str:
    # The app's SQL has no literal ? or % in it; values always go in params.
    return sql.replace("%", "%%").replace("?", "%s")


class PostgresConnection:
    """Wraps a psycopg connection so it can be used like sqlite3's."""

    dialect = "postgres"

    def __init__(self, url: str):
        try:
            import psycopg
        except ImportError as e:
            raise RuntimeError("DATABASE_URL is set but the PostgreSQL driver is missing. Run:\n"
                               "  .venv\\Scripts\\python.exe -m pip install -r requirements.txt") from e
        self._conn = psycopg.connect(url, row_factory=_row_factory)

    def execute(self, sql: str, params=()):
        if params:
            return self._conn.execute(_to_pg(sql), tuple(params))
        return self._conn.execute(sql)

    def executemany(self, sql: str, seq):
        cur = self._conn.cursor()
        cur.executemany(_to_pg(sql), [tuple(p) for p in seq])
        return cur

    def executescript(self, sql: str) -> None:
        self._conn.execute(sql)  # several statements are allowed when there are no params

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def _database_errors() -> tuple:
    errors = [sqlite3.DatabaseError]
    try:
        import psycopg

        errors.append(psycopg.DatabaseError)
    except ImportError:
        pass
    return tuple(errors)


# Catch this to handle an error from either database.
DatabaseError = _database_errors()


# ---------------------------------------------------------------- shared

def connect(target: str | None = None):
    """Open the database: a PostgreSQL URL or a SQLite file path. With no
    target, use DATABASE_URL if it is set, otherwise LIBRARY_DB."""
    from .config import settings

    target = target or settings.db_path
    if is_postgres(target):
        return PostgresConnection(target)
    conn = sqlite3.connect(target, check_same_thread=False, factory=SqliteConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn) -> None:
    conn.executescript(POSTGRES_SCHEMA if conn.dialect == "postgres" else SQLITE_SCHEMA)
    _add_missing_columns(conn)
    conn.commit()


# Columns added after the first release. CREATE TABLE IF NOT EXISTS leaves an
# existing table alone, so older databases get these added here.
NEW_COLUMNS = {
    "books": {"category": "TEXT NOT NULL DEFAULT ''",
              # Bulk add (MINI SPEC 4). Each row is still one physical copy.
              "isbn": "TEXT NOT NULL DEFAULT ''",
              "publisher": "TEXT NOT NULL DEFAULT ''",
              "year": "TEXT NOT NULL DEFAULT ''",
              "created_at": "TEXT",
              "updated_at": "TEXT"},
    "students": {"section": "TEXT NOT NULL DEFAULT ''",
                 "phone": "TEXT NOT NULL DEFAULT ''",
                 "photo_url": "TEXT NOT NULL DEFAULT ''",
                 # Removing a student makes them Inactive; the record and history stay.
                 "status": "TEXT NOT NULL DEFAULT 'Active'",
                 "created_at": "TEXT",
                 "removed_at": "TEXT"},
}


def _add_missing_columns(conn) -> None:
    for table, columns in NEW_COLUMNS.items():
        for name, ddl in columns.items():
            if conn.dialect == "postgres":
                conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {ddl}")
                continue
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def is_empty(conn) -> bool:
    return all(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0 for t in TABLES)


def copy_tables(src, dst) -> int:
    """Copy every row from src into dst, which must have the tables and be
    empty. Keeps the original IDs. Returns the number of rows copied."""
    total = 0
    for table in TABLES:
        if src.dialect == "sqlite" and not src.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
            continue  # an older backup made before this table existed
        rows = src.execute(f"SELECT * FROM {table}").fetchall()
        if not rows:
            continue
        cols = list(rows[0].keys())
        dst.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        [tuple(r[c] for c in cols) for r in rows])
        total += len(rows)
    if dst.dialect == "postgres":
        # Start new IDs after the copied ones.
        for table, col in AUTO_IDS.items():
            dst.execute(f"SELECT setval(pg_get_serial_sequence('{table}', '{col}'),"
                        f" COALESCE((SELECT MAX({col}) FROM {table}), 0) + 1, false)")
    return total
