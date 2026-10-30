"""The daily job (PLAN 3.2, 3.4). Safe to run more than once a day: every
message has a dedup key, so nothing is sent twice."""
import sqlite3
from datetime import date, timedelta

from . import notifications, register, states
from .config import settings


def run_daily(conn: sqlite3.Connection, mailer: notifications.Mailer, today: date | None = None) -> dict:
    today = today or date.today()
    t = today.isoformat()
    summary = {"due_reminders": 0, "overdue": 0, "school_emails": 0, "reservations_expired": 0}

    open_loans = conn.execute(
        "SELECT l.*, b.name AS book_name, b.status AS book_status, s.name AS student_name, s.grade,"
        " s.contact_email FROM loans l JOIN books b ON b.book_id = l.book_id"
        " JOIN students s ON s.student_id = l.student_id"
        " WHERE l.return_date IS NULL AND l.deleted_at IS NULL AND b.deleted_at IS NULL"
    ).fetchall()

    for loan in open_loans:
        values = dict(student_name=loan["student_name"], book_name=loan["book_name"],
                      book_id=loan["book_id"], due_date=loan["due_date"], grade=loan["grade"])
        # Standard due-date reminder: automatic (open decision 1).
        if loan["due_date"] == t and loan["contact_email"]:
            if notifications.notify(conn, mailer, "due_reminder", loan["contact_email"],
                                    dedup_key=f"due:{loan['loan_id']}:{loan['due_date']}",
                                    loan_id=loan["loan_id"], **values):
                summary["due_reminders"] += 1
        # Past due: Borrowed -> Overdue, red alert for the committee, reminder for the student.
        if loan["due_date"] < t and loan["book_status"] == states.BORROWED:
            states.set_status(conn, loan["book_id"], states.OVERDUE)
            summary["overdue"] += 1
            key = f"{loan['loan_id']}:{loan['due_date']}"
            notifications.notify(conn, mailer, "red_alert", settings.committee_email,
                                 dedup_key=f"red:{key}", loan_id=loan["loan_id"], **values)
            if loan["contact_email"]:
                notifications.notify(conn, mailer, "overdue_reminder", loan["contact_email"],
                                     dedup_key=f"overdue:{key}", loan_id=loan["loan_id"], **values)

    # School email after N overdue days, once per loan that crosses the line.
    late = [l for l in register.overdue_books(conn)
            if (today - date.fromisoformat(l["due_date"])).days >= settings.school_escalation_days]
    new = [l for l in late if not conn.execute(
        "SELECT 1 FROM notifications WHERE dedup_key = ?",
        (f"school:{l['loan_id']}:{l['due_date']}",)).fetchone()]
    if new:
        lines = "\n".join(f"- {l['name']} ({l['book_id']}), {l['student_name']}, grade {l['grade']}, "
                          f"due {l['due_date']}" for l in late)
        sent = notifications.notify(conn, mailer, "school_escalation", settings.school_email,
                                    dedup_key=f"school:{t}", count=len(late),
                                    days=settings.school_escalation_days, lines=lines)
        if sent:
            summary["school_emails"] += 1
            for l in new:
                conn.execute(
                    "INSERT OR IGNORE INTO notifications (kind, dedup_key, recipient, subject, body,"
                    " loan_id, status, created_at, sent_at) VALUES ('school_marker', ?, ?, '', '', ?,"
                    " 'sent', ?, ?)",
                    (f"school:{l['loan_id']}:{l['due_date']}", settings.school_email, l["loan_id"], t, t),
                )

    # Expire reservations whose hold has run out: Reserved -> Available (automatic).
    for res in conn.execute(
        "SELECT * FROM reservations WHERE active = 1 AND expires_on IS NOT NULL AND expires_on < ?", (t,)
    ).fetchall():
        conn.execute("UPDATE reservations SET active = 0 WHERE reservation_id = ?", (res["reservation_id"],))
        book = register.get_book(conn, res["book_id"])
        if book and book["status"] == states.RESERVED:
            states.set_status(conn, res["book_id"], states.AVAILABLE)
            nxt = register.active_reservation(conn, res["book_id"])
            if nxt:  # hand the hold to the next student in the queue
                states.set_status(conn, res["book_id"], states.RESERVED)
                conn.execute("UPDATE reservations SET expires_on = ? WHERE reservation_id = ?",
                             ((today + timedelta(days=settings.reservation_hold_days)).isoformat(),
                              nxt["reservation_id"]))
        summary["reservations_expired"] += 1

    conn.commit()
    return summary
