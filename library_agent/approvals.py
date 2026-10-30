"""Human approval of pending actions (PLAN 2.14, 5.2, 5.3; SPEC Layer 9).

Approving re-checks the current state before changing anything, because
the register may have changed since the agent made its suggestion.
Rejecting changes nothing, and is written to the correction log so the
committee can see where the agent gets things wrong (PLAN 7.4).
"""
import json
import sqlite3
from datetime import date, timedelta

from . import notifications, register, states
from .config import settings
from .db import now


class ApprovalError(Exception):
    pass


def get_action(conn, action_id: int):
    row = conn.execute("SELECT * FROM pending_actions WHERE action_id = ?", (action_id,)).fetchone()
    if row is None:
        raise ApprovalError(f"No pending action #{action_id}.")
    return row


def list_pending(conn) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM pending_actions WHERE status = 'pending' ORDER BY action_id"):
        d = dict(r)
        d["details"] = json.loads(r["details"])
        d["flags"] = json.loads(r["flags"])
        out.append(d)
    return out


def approve(conn: sqlite3.Connection, action_id: int, human: str, *, today: date | None = None,
            note: str = "") -> str:
    today = today or date.today()
    action = get_action(conn, action_id)
    if action["status"] != "pending":
        raise ApprovalError(f"Action #{action_id} was already {action['status']}.")
    details = json.loads(action["details"])
    flags = json.loads(action["flags"])
    executor = _EXECUTORS.get(action["tool"])
    if executor is None:
        raise ApprovalError(f"Unknown action type {action['tool']}.")
    try:
        result = executor(conn, details, human, today, flags)
        conn.execute(
            "UPDATE pending_actions SET status = 'approved', decided_by = ?, decided_at = ?,"
            " decision_reason = ? WHERE action_id = ?",
            (human, now(), note or None, action_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return result


def reject(conn: sqlite3.Connection, action_id: int, human: str, reason: str) -> None:
    if not reason.strip():
        raise ApprovalError("A reason is required to reject an action.")
    action = get_action(conn, action_id)
    if action["status"] != "pending":
        raise ApprovalError(f"Action #{action_id} was already {action['status']}.")
    conn.execute(
        "UPDATE pending_actions SET status = 'rejected', decided_by = ?, decided_at = ?,"
        " decision_reason = ? WHERE action_id = ?",
        (human, now(), reason, action_id),
    )
    if action["tool"] != "escalate_to_librarian":
        states.log_correction(conn, f"AI suggested {action['tool']} (#{action_id}): {action['reason']}",
                              "Rejected; nothing was saved", reason, human)
    conn.commit()


# ---------------------------------------------------------------- executors

def _borrow(conn, d, human, today, flags):
    book = register.get_book(conn, d["book_id"])
    student = register.get_student(conn, d["student_id"])
    if book is None or student is None:
        raise ApprovalError("The book or student no longer exists.")
    if book["status"] == states.RESERVED:
        res = register.active_reservation(conn, d["book_id"])
        if res is None or res["student_id"] != d["student_id"]:
            raise ApprovalError("The book is now reserved for another student.")
        conn.execute("UPDATE reservations SET active = 0 WHERE reservation_id = ?", (res["reservation_id"],))
    elif book["status"] != states.AVAILABLE:
        raise ApprovalError(f"The book is now {book['status']}; it cannot be lent.")
    states.set_status(conn, d["book_id"], states.BORROWED)
    cur = conn.execute(
        "INSERT INTO loans (book_id, student_id, take_date, due_date, approved_by) VALUES (?, ?, ?, ?, ?)",
        (d["book_id"], d["student_id"], d["take_date"], d["due_date"], human),
    )
    if flags:
        states.record_decision(conn, "special_borrowing_approval",
                               f"Loan approved despite: {' '.join(flags)}", human,
                               book_id=d["book_id"], student_id=d["student_id"], loan_id=cur.lastrowid)
    return f"Loan saved: {d['book_name']} to {d['student_name']}, due {d['due_date']}."


def _open_loan(conn, d):
    loan = conn.execute("SELECT * FROM loans WHERE loan_id = ?", (d["loan_id"],)).fetchone()
    if loan is None or loan["return_date"] is not None:
        raise ApprovalError("This loan is no longer open.")
    return loan


def _activate_next_reservation(conn, book_id, today):
    res = register.active_reservation(conn, book_id)
    if res is None:
        return ""
    expires = (today + timedelta(days=settings.reservation_hold_days)).isoformat()
    states.set_status(conn, book_id, states.RESERVED)
    conn.execute("UPDATE reservations SET expires_on = ? WHERE reservation_id = ?",
                 (expires, res["reservation_id"]))
    return f" Now held for the next reservation until {expires}."


def _return(conn, d, human, today, flags):
    loan = _open_loan(conn, d)
    book = register.get_book(conn, d["book_id"])
    conn.execute("UPDATE loans SET return_date = ?, return_condition = 'good' WHERE loan_id = ?",
                 (today.isoformat(), loan["loan_id"]))
    if book["status"] == states.OVERDUE:
        open_fine = conn.execute(
            "SELECT 1 FROM fines WHERE loan_id = ? AND status IN ('unpaid', 'reduced')", (loan["loan_id"],)
        ).fetchone()
        if open_fine:
            # Overdue -> Available needs the fine resolved (SPEC Layer 3).
            return f"Return saved for {d['book_name']}. It stays Overdue until the fine is resolved."
    states.set_status(conn, d["book_id"], states.AVAILABLE)
    return f"Return saved: {d['book_name']} is back in the library." + \
        _activate_next_reservation(conn, d["book_id"], today)


def _damage(conn, d, human, today, flags):
    loan = _open_loan(conn, d)
    conn.execute("UPDATE loans SET return_date = ?, return_condition = 'damaged' WHERE loan_id = ?",
                 (today.isoformat(), loan["loan_id"]))
    states.set_status(conn, d["book_id"], states.DAMAGED)
    conn.execute("UPDATE books SET condition_notes = ? WHERE book_id = ?", (d["notes"], d["book_id"]))
    states.record_decision(conn, "damage_assessment", f"Damage confirmed: {d['notes']}", human,
                           book_id=d["book_id"], student_id=d["student_id"], loan_id=loan["loan_id"])
    notifications.draft_payup(conn, loan["loan_id"], d["notes"])
    return (f"Damage recorded for {d['book_name']}; it is out of circulation. A pay-up message has "
            "been drafted and will only be sent after the librarian sets the amount.")


def _renew(conn, d, human, today, flags):
    loan = _open_loan(conn, d)
    if loan["due_date"] != d["due_date"]:
        raise ApprovalError("The due date has changed since this was suggested; ask the agent again.")
    if loan["renewals"] >= settings.max_renewals:
        raise ApprovalError("The renewal limit has been reached.")
    res = register.active_reservation(conn, d["book_id"])
    if res and res["student_id"] != loan["student_id"]:
        raise ApprovalError("Another student has reserved this book since the suggestion.")
    states.set_status(conn, d["book_id"], states.BORROWED)  # Borrowed -> Borrowed
    conn.execute("UPDATE loans SET due_date = ?, renewals = renewals + 1 WHERE loan_id = ?",
                 (d["new_due_date"], loan["loan_id"]))
    states.record_decision(conn, "renewal_approval",
                           f"Renewed {d['book_name']}: {d['due_date']} -> {d['new_due_date']}", human,
                           book_id=d["book_id"], student_id=d["student_id"], loan_id=loan["loan_id"])
    return f"Renewed: {d['book_name']} is now due {d['new_due_date']}."


def _escalation(conn, d, human, today, flags):
    return "Marked as handled by the librarian."


_EXECUTORS = {
    "save_borrowing_details": _borrow,
    "update_return_status": _return,
    "record_book_damage": _damage,
    "renew_loan": _renew,
    "escalate_to_librarian": _escalation,
}
