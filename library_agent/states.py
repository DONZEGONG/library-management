"""Book states and allowed transitions (SPEC Layer 3, PLAN 1.2)."""
import json
import sqlite3

from .db import now

AVAILABLE = "Available"
BORROWED = "Borrowed"
RESERVED = "Reserved"
OVERDUE = "Overdue"
RETURNED = "Returned"
LOST = "Lost"
DAMAGED = "Damaged"

ALL_STATES = (AVAILABLE, BORROWED, RESERVED, OVERDUE, RETURNED, LOST, DAMAGED)

# Books physically in the library (PLAN 1.4 "current books").
IN_LIBRARY = (AVAILABLE, RESERVED, RETURNED, DAMAGED)

# (from, to) -> who may make the change. "auto" is the daily job.
TRANSITIONS = {
    (AVAILABLE, BORROWED): "human",   # agent records, human approves
    (AVAILABLE, RESERVED): "agent",
    (RESERVED, BORROWED): "human",
    (RESERVED, AVAILABLE): "auto",
    (BORROWED, AVAILABLE): "human",   # agent records, human approves
    (BORROWED, OVERDUE): "auto",
    (BORROWED, BORROWED): "human",    # renewal
    (BORROWED, LOST): "human",
    (BORROWED, DAMAGED): "human",
    (OVERDUE, AVAILABLE): "human",
    (LOST, AVAILABLE): "human",
    (DAMAGED, AVAILABLE): "human",
    (DAMAGED, LOST): "human",
    (DAMAGED, DAMAGED): "human",      # cannot be repaired, stays out of circulation
    # Not in the SPEC table; added so an overdue book can be reported lost or
    # returned damaged without a librarian override. Needs committee sign-off.
    (OVERDUE, LOST): "human",
    (OVERDUE, DAMAGED): "human",
}


class TransitionError(ValueError):
    pass


def check_transition(current: str, new: str) -> None:
    if (current, new) not in TRANSITIONS:
        raise TransitionError(f"A book cannot go from {current} to {new}.")


def set_status(conn: sqlite3.Connection, book_id: str, new: str) -> None:
    """Change a book's status, refusing any transition not in the table."""
    row = conn.execute("SELECT status FROM books WHERE book_id = ?", (book_id,)).fetchone()
    if row is None:
        raise LookupError(f"No book with ID {book_id}.")
    check_transition(row["status"], new)
    conn.execute("UPDATE books SET status = ? WHERE book_id = ?", (new, book_id))


def librarian_correct(conn: sqlite3.Connection, book_id: str, new: str,
                      reason: str, librarian: str, original: str | None = None) -> None:
    """Librarian override: any state to any state, always logged (PLAN 2.13)."""
    if new not in ALL_STATES:
        raise TransitionError(f"Unknown state {new}.")
    if not reason.strip():
        raise ValueError("A reason is required for a correction.")
    row = conn.execute("SELECT status FROM books WHERE book_id = ?", (book_id,)).fetchone()
    if row is None:
        raise LookupError(f"No book with ID {book_id}.")
    conn.execute("UPDATE books SET status = ? WHERE book_id = ?", (new, book_id))
    log_correction(conn, original or f"Book {book_id} status was {row['status']}",
                   f"Book {book_id} status set to {new}", reason, librarian)


def log_correction(conn, original: str, correction: str, reason: str, by: str) -> None:
    conn.execute(
        "INSERT INTO correction_log (original_decision, human_correction, reason, corrected_by, date)"
        " VALUES (?, ?, ?, ?, ?)",
        (original, correction, reason, by, now()),
    )


def record_decision(conn, kind: str, summary: str, by: str, *, book_id=None,
                    student_id=None, loan_id=None) -> None:
    conn.execute(
        "INSERT INTO human_decisions (kind, book_id, student_id, loan_id, summary, decided_by, decided_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (kind, book_id, student_id, loan_id, summary, by, now()),
    )


def audit_tool_call(conn, tool: str, arguments: dict, actor: str) -> None:
    conn.execute(
        "INSERT INTO tool_calls (tool, arguments, actor, called_at) VALUES (?, ?, ?, ?)",
        (tool, json.dumps(arguments, default=str), actor, now()),
    )
