"""Message templates and sending (PLAN 3.1, 3.3, 3.4).

Wording is neutral and never blames a student (Layer 7).
"""
import smtplib
import sqlite3
from email.message import EmailMessage

from .config import settings
from .db import now

TEMPLATES = {
    "due_reminder": (
        "Library reminder: {book_name} is due today",
        "Hello {student_name},\n\nThis is a friendly reminder that \"{book_name}\" ({book_id}) "
        "is due back at the library today, {due_date}. If you need more time, please ask a "
        "library committee member about a renewal.\n\nThank you,\nSchool Library",
    ),
    "overdue_reminder": (
        "Library reminder: {book_name} is past its due date",
        "Hello {student_name},\n\nOur records show that \"{book_name}\" ({book_id}) was due on "
        "{due_date} and has not been checked in yet. Please return it when you can, or speak to "
        "a library committee member if something has come up.\n\nThank you,\nSchool Library",
    ),
    "red_alert": (
        "RED ALERT: {book_name} ({book_id}) not returned",
        "\"{book_name}\" ({book_id}) borrowed by {student_name} (grade {grade}) was due on "
        "{due_date} and is now marked Overdue. A reminder has been sent to the student.",
    ),
    "school_escalation": (
        "Library: {count} book(s) not returned after {days} days",
        "Dear School Office,\n\n{count} library book(s) have not been returned more than "
        "{days} days after their due date:\n\n{lines}\n\nThe library committee will follow up. "
        "This message is for information only.\n\nSchool Library",
    ),
    "payup": (
        "Library: repair or replacement charge for {book_name}",
        "Hello {student_name},\n\n\"{book_name}\" ({book_id}) was checked in with damage: "
        "{notes}. Following the library's fine policy, the charge set by the librarian is "
        "{amount}. Please pay at the library desk, or speak to the librarian if you have "
        "any questions.\n\nThank you,\nSchool Library",
    ),
}


class Mailer:
    """Sends over SMTP when SMTP_HOST is set; otherwise only records the
    message in the notifications table (useful for testing and the trial)."""

    def __init__(self):
        self.outbox: list[EmailMessage] = []

    def send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = settings.mail_from, to, subject
        msg.set_content(body)
        self.outbox.append(msg)
        if not settings.smtp_host:
            return
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
            smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)


def render(kind: str, **values) -> tuple[str, str]:
    subject, body = TEMPLATES[kind]
    return subject.format(**values), body.format(**values)


def notify(conn: sqlite3.Connection, mailer: Mailer, kind: str, to: str, *,
           dedup_key: str | None = None, loan_id: int | None = None, **values) -> bool:
    """Send once. Returns False if a message with this dedup key already exists."""
    if dedup_key and conn.execute(
        "SELECT 1 FROM notifications WHERE dedup_key = ?", (dedup_key,)
    ).fetchone():
        return False
    subject, body = render(kind, **values)
    status = "sent"
    try:
        mailer.send(to, subject, body)
    except (OSError, smtplib.SMTPException):
        status = "failed"
        dedup_key = None  # allow a retry on the next run
    conn.execute(
        "INSERT INTO notifications (kind, dedup_key, recipient, subject, body, loan_id, status,"
        " created_at, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, dedup_key, to, subject, body, loan_id, status, now(),
         now() if status == "sent" else None),
    )
    return status == "sent"


def draft_payup(conn, loan_id: int, notes: str) -> int:
    """Draft the pay-up message after damage is recorded. The amount is left
    blank; it can only be sent once a human sets the fine (PLAN 3.3)."""
    row = _loan_view(conn, loan_id)
    subject = f"Library: repair or replacement charge for {row['book_name']}"
    body = f"(Draft) Damage notes: {notes}. Waiting for the librarian to set the amount."
    cur = conn.execute(
        "INSERT INTO notifications (kind, recipient, subject, body, loan_id, status, created_at)"
        " VALUES ('payup', ?, ?, ?, ?, 'draft', ?)",
        (row["contact_email"], subject, body, loan_id, now()),
    )
    return cur.lastrowid


class PayupNotAllowed(Exception):
    pass


def send_payup(conn, mailer: Mailer, loan_id: int) -> None:
    fine = conn.execute(
        "SELECT * FROM fines WHERE loan_id = ? AND status IN ('unpaid', 'reduced')"
        " ORDER BY fine_id DESC LIMIT 1",
        (loan_id,),
    ).fetchone()
    if fine is None:
        raise PayupNotAllowed("The pay-up message cannot be sent until a human sets the fine amount.")
    draft = conn.execute(
        "SELECT * FROM notifications WHERE loan_id = ? AND kind = 'payup' AND status = 'draft'"
        " ORDER BY notification_id DESC LIMIT 1",
        (loan_id,),
    ).fetchone()
    row = _loan_view(conn, loan_id)
    notes = row["condition_notes"] or "see librarian"
    subject, body = render("payup", student_name=row["student_name"], book_name=row["book_name"],
                           book_id=row["book_id"], notes=notes, amount=f"{fine['amount']:.2f}")
    mailer.send(row["contact_email"], subject, body)
    if draft:
        conn.execute(
            "UPDATE notifications SET subject = ?, body = ?, status = 'sent', sent_at = ?"
            " WHERE notification_id = ?",
            (subject, body, now(), draft["notification_id"]),
        )
    else:
        conn.execute(
            "INSERT INTO notifications (kind, recipient, subject, body, loan_id, status, created_at,"
            " sent_at) VALUES ('payup', ?, ?, ?, ?, 'sent', ?, ?)",
            (row["contact_email"], subject, body, loan_id, now(), now()),
        )


def _loan_view(conn, loan_id: int):
    return conn.execute(
        "SELECT l.*, b.name AS book_name, b.condition_notes, s.name AS student_name, s.grade,"
        " s.contact_email FROM loans l JOIN books b ON b.book_id = l.book_id"
        " JOIN students s ON s.student_id = l.student_id WHERE l.loan_id = ?",
        (loan_id,),
    ).fetchone()
