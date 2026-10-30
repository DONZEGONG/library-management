"""Human-only actions (PLAN 2.9-2.13, 5.5). The agent has no access to
these. Every action needs a reason and is recorded."""
import sqlite3
from datetime import date

from . import notifications, register, states
from .db import now


class LibrarianError(Exception):
    pass


def _need_reason(reason: str):
    if not reason or not reason.strip():
        raise LibrarianError("A reason is required.")


def _latest_loan(conn, book_id):
    return conn.execute(
        "SELECT * FROM loans WHERE book_id = ? AND deleted_at IS NULL ORDER BY loan_id DESC LIMIT 1",
        (book_id,),
    ).fetchone()


def set_fine(conn: sqlite3.Connection, loan_id: int, amount: float, reason: str, librarian: str,
             mailer: notifications.Mailer | None = None, send_payup: bool = True) -> int:
    """Set a fine amount. If a pay-up message was drafted for this loan, it
    can now be sent (open decision 2)."""
    _need_reason(reason)
    if amount <= 0:
        raise LibrarianError("The fine amount must be more than zero.")
    loan = conn.execute("SELECT * FROM loans WHERE loan_id = ?", (loan_id,)).fetchone()
    if loan is None:
        raise LibrarianError(f"No loan #{loan_id}.")
    cur = conn.execute(
        "INSERT INTO fines (loan_id, reason, amount, set_by, created_at) VALUES (?, ?, ?, ?, ?)",
        (loan_id, reason, amount, librarian, now()),
    )
    if send_payup and mailer is not None:
        notifications.send_payup(conn, mailer, loan_id)
    conn.commit()
    return cur.lastrowid


def update_fine(conn, fine_id: int, status: str, reason: str, librarian: str,
                new_amount: float | None = None) -> None:
    """Mark a fine paid, waived or reduced. Resolving the last open fine on a
    returned overdue book puts the book back on the shelf."""
    _need_reason(reason)
    if status not in ("paid", "waived", "reduced"):
        raise LibrarianError("Status must be paid, waived or reduced.")
    fine = conn.execute("SELECT * FROM fines WHERE fine_id = ?", (fine_id,)).fetchone()
    if fine is None:
        raise LibrarianError(f"No fine #{fine_id}.")
    if status == "reduced":
        if new_amount is None or not 0 < new_amount < fine["amount"]:
            raise LibrarianError("A reduced fine needs a new, lower amount.")
        conn.execute("UPDATE fines SET status = 'reduced', amount = ? WHERE fine_id = ?", (new_amount, fine_id))
    else:
        conn.execute("UPDATE fines SET status = ? WHERE fine_id = ?", (status, fine_id))
    loan = conn.execute("SELECT * FROM loans WHERE loan_id = ?", (fine["loan_id"],)).fetchone()
    if status in ("waived", "reduced"):
        summary = f"Fine #{fine_id} {status}" + (f" to {new_amount:.2f}" if new_amount else "") + f": {reason}"
        states.record_decision(conn, f"fine_{status}", summary, librarian,
                               book_id=loan["book_id"], student_id=loan["student_id"], loan_id=loan["loan_id"])
    book = register.get_book(conn, loan["book_id"])
    still_open = conn.execute(
        "SELECT 1 FROM fines WHERE loan_id = ? AND status IN ('unpaid', 'reduced')", (loan["loan_id"],)
    ).fetchone()
    if book and book["status"] == states.OVERDUE and loan["return_date"] and not still_open:
        states.set_status(conn, loan["book_id"], states.AVAILABLE)
    conn.commit()


def change_due_date(conn, loan_id: int, new_due: date, reason: str, librarian: str) -> None:
    """For example around festivals (open decision 6: librarian only)."""
    _need_reason(reason)
    loan = conn.execute("SELECT * FROM loans WHERE loan_id = ? AND return_date IS NULL", (loan_id,)).fetchone()
    if loan is None:
        raise LibrarianError(f"No open loan #{loan_id}.")
    conn.execute("UPDATE loans SET due_date = ? WHERE loan_id = ?", (new_due.isoformat(), loan_id))
    book = register.get_book(conn, loan["book_id"])
    if book["status"] == states.OVERDUE and new_due.isoformat() >= date.today().isoformat():
        conn.execute("UPDATE books SET status = ? WHERE book_id = ?", (states.BORROWED, loan["book_id"]))
    states.log_correction(conn, f"Loan #{loan_id} due date was {loan['due_date']}",
                          f"Due date changed to {new_due.isoformat()}", reason, librarian)
    conn.commit()


def mark_lost(conn, book_id: str, reason: str, librarian: str) -> None:
    """Human only. Records that the book is lost; it does not decide who is
    responsible (Layer 7)."""
    _need_reason(reason)
    book = register.get_book(conn, book_id)
    if book is None:
        raise LibrarianError(f"No book {book_id}.")
    states.set_status(conn, book_id, states.LOST)
    loan = register.open_loan_for_book(conn, book_id)
    if loan:
        conn.execute("UPDATE loans SET return_date = ?, return_condition = 'lost' WHERE loan_id = ?",
                     (date.today().isoformat(), loan["loan_id"]))
    states.record_decision(conn, "lost_confirmed", f"Book {book_id} confirmed lost: {reason}", librarian,
                           book_id=book_id, student_id=loan["student_id"] if loan else None,
                           loan_id=loan["loan_id"] if loan else None)
    conn.commit()


def resolve_book(conn, book_id: str, outcome: str, reason: str, librarian: str) -> None:
    """Close out a Lost or Damaged book: 'repaired', 'replaced' (back to
    Available), 'write_off' (Damaged -> Lost) or 'keep_damaged'."""
    _need_reason(reason)
    book = register.get_book(conn, book_id)
    if book is None:
        raise LibrarianError(f"No book {book_id}.")
    target = {"repaired": states.AVAILABLE, "replaced": states.AVAILABLE,
              "write_off": states.LOST, "keep_damaged": states.DAMAGED}.get(outcome)
    if target is None:
        raise LibrarianError("Unknown outcome.")
    states.set_status(conn, book_id, target)
    if target == states.AVAILABLE:
        conn.execute("UPDATE books SET condition_notes = ? WHERE book_id = ?", (f"{outcome}: {reason}", book_id))
    loan = _latest_loan(conn, book_id)
    states.record_decision(conn, "damage_assessment" if book["status"] == states.DAMAGED else "lost_resolved",
                           f"Book {book_id} {outcome}: {reason}", librarian, book_id=book_id,
                           student_id=loan["student_id"] if loan else None)
    conn.commit()


def correct_state(conn, book_id: str, new_status: str, reason: str, librarian: str) -> None:
    states.librarian_correct(conn, book_id, new_status, reason, librarian)
    conn.commit()


def delete_record(conn, table: str, record_id, reason: str, librarian: str) -> None:
    """Soft delete: hidden from the register but kept for the audit (PLAN 2.10)."""
    _need_reason(reason)
    keys = {"books": "book_id", "students": "student_id", "loans": "loan_id"}
    if table not in keys:
        raise LibrarianError("Only books, students and loans can be deleted.")
    cur = conn.execute(
        f"UPDATE {table} SET deleted_at = ?, deleted_by = ?, delete_reason = ?"
        f" WHERE {keys[table]} = ? AND deleted_at IS NULL",
        (now(), librarian, reason, record_id),
    )
    if cur.rowcount == 0:
        raise LibrarianError(f"No {table[:-1]} {record_id} to delete.")
    states.record_decision(conn, "record_deleted", f"{table[:-1]} {record_id} deleted: {reason}", librarian)
    conn.commit()
