"""Add, remove and restore students (MINI SPEC 3.2 and 3.3).

Removing a student never erases them: their status becomes Inactive, their
borrowing history stays in the reports, and they cannot borrow. The
librarian can restore them later.
"""
import re

from . import activity, register
from .db import now
from .uploads import MAX_ROWS

ACTIVE, INACTIVE = "Active", "Inactive"
FIELDS = ("student_id", "name", "grade", "section", "phone", "email", "photo_url")
SYNONYMS = {
    "student_id": ("student id", "studentid", "id", "admission no", "admission number", "adm no",
                   "roll no", "roll number", "sno", "s no"),
    "name": ("full name", "name", "student name", "name of the student"),
    "grade": ("class", "grade", "std", "standard", "class grade"),
    "section": ("section", "sec", "division", "div"),
    "phone": ("phone", "phone number", "mobile", "mobile number", "contact number", "parent phone"),
    "email": ("email", "e mail", "email address", "contact email", "mail"),
    "photo_url": ("photo", "photo url", "picture", "image"),
}
DUPLICATE = "Student ID already exists."


class RosterError(ValueError):
    pass


def _text(value, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def clean(row: dict) -> dict:
    out = {f: _text(row.get(f)) for f in FIELDS}
    out["student_id"] = out["student_id"].upper()
    out["email"] = out["email"].lower()
    return out


def problems(conn, row: dict) -> list[str]:
    errors = []
    if not row["student_id"]:
        errors.append("Student ID is missing.")
    elif conn.execute("SELECT 1 FROM students WHERE student_id = ?", (row["student_id"],)).fetchone():
        errors.append(DUPLICATE)  # also counts removed students, so an ID is never reused
    if not row["name"]:
        errors.append("Full name is missing.")
    if row["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", row["email"]):
        errors.append(f"Email {row['email']} does not look right.")
    if row["phone"] and not re.fullmatch(r"[+\d][\d\s()-]{5,19}", row["phone"]):
        errors.append(f"Phone {row['phone']} does not look like a phone number.")
    if row["photo_url"] and not row["photo_url"].startswith(("https://", "http://")):
        errors.append("Photo must be a web link starting with https://.")
    return errors


def _insert(conn, row: dict, stamp: str) -> None:
    conn.execute(
        "INSERT INTO students (student_id, name, grade, contact_email, section, phone, photo_url, status,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (row["student_id"], row["name"], row["grade"], row["email"], row["section"], row["phone"],
         row["photo_url"], ACTIVE, stamp),
    )


def add_student(conn, data: dict, user: str) -> dict:
    row = clean(data)
    errors = problems(conn, row)
    if errors:
        raise RosterError(" ".join(errors))
    _insert(conn, row, now())
    activity.log(conn, user, "ADD_STUDENT", f"{row['student_id']} {row['name']} (class {row['grade'] or '-'}"
                 f"{' ' + row['section'] if row['section'] else ''})", "manual")
    conn.commit()
    return row


def check_bulk(conn, rows: list[dict]) -> list[dict]:
    if len(rows) > MAX_ROWS:
        raise RosterError(f"The most one upload can take is {MAX_ROWS:,} rows.")
    out, seen = [], set()
    for raw in rows:
        row = clean(raw)
        errors = problems(conn, row)
        if row["student_id"] and row["student_id"] in seen:
            errors.append("This Student ID is in the file twice.")
        seen.add(row["student_id"])
        row["errors"] = errors
        row["status"] = "error" if errors else "new"
        out.append(row)
    return out


def save_bulk(conn, rows: list[dict], user: str) -> dict:
    """Add the rows marked New; rows with errors are skipped. One step, or nothing."""
    checked = check_bulk(conn, rows)
    new = [r for r in checked if r["status"] == "new"]
    stamp = now()
    try:
        for r in new:
            _insert(conn, r, stamp)
        if new:
            names = "; ".join(f"{r['student_id']} {r['name']}" for r in new)
            activity.log(conn, user, "ADD_STUDENT", f"Added {len(new)} student(s) from a file: {names}"[:4000], "file")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    skipped = len(checked) - len(new)
    return {"added": len(new), "skipped": skipped,
            "message": f"{len(new)} added, {skipped} skipped."}


def get(conn, student_id: str):
    return conn.execute("SELECT * FROM students WHERE student_id = ? AND deleted_at IS NULL",
                        (student_id.strip().upper(),)).fetchone()


def search(conn, query: str = "", status: str = ACTIVE, limit: int = 500) -> list:
    sql, params = "SELECT * FROM students WHERE deleted_at IS NULL", []
    if status in (ACTIVE, INACTIVE):
        sql += " AND status = ?"
        params.append(status)
    q = query.strip()
    if q:
        sql += " AND (UPPER(student_id) = UPPER(?) OR LOWER(name) LIKE LOWER(?))"
        params += [q, f"%{q}%"]
    return conn.execute(sql + " ORDER BY student_id LIMIT ?", (*params, limit)).fetchall()


def _money(amount: float) -> str:
    return f"₹{amount:,.0f}" if float(amount).is_integer() else f"₹{amount:,.2f}"


def blockers(conn, student_id: str) -> str:
    """Why the student cannot be removed yet, or "" if they can."""
    loans = register.open_loans_for_student(conn, student_id)
    fines = register.unpaid_fines(conn, student_id)
    parts = []
    if loans:
        parts.append(f"{len(loans)} book{'s' if len(loans) != 1 else ''} not returned")
    if fines:
        parts.append(f"a {_money(sum(f['amount'] for f in fines))} fine"
                     + (f" ({len(fines)} unpaid)" if len(fines) > 1 else ""))
    if not parts:
        return ""
    return f"Student has {' and '.join(parts)}. Clear these first."


def remove(conn, student_id: str, user: str) -> None:
    s = get(conn, student_id)
    if s is None:
        raise RosterError(f"No student {student_id}.")
    if s["status"] == INACTIVE:
        raise RosterError(f"{s['name']} has already been removed.")
    reason = blockers(conn, s["student_id"])
    if reason:
        raise RosterError(reason)
    conn.execute("UPDATE students SET status = ?, removed_at = ? WHERE student_id = ?",
                 (INACTIVE, now(), s["student_id"]))
    _cancel_reservations(conn, s["student_id"])
    activity.log(conn, user, "REMOVE_STUDENT", f"{s['student_id']} {s['name']} set to Inactive", "manual")
    conn.commit()


def _cancel_reservations(conn, student_id: str) -> None:
    """A removed student cannot borrow, so their reservations end. A book
    being held for them goes to the next student in the queue, or back on
    the shelf (the same as an expired hold)."""
    from datetime import date, timedelta

    from . import states
    from .config import settings

    for res in conn.execute("SELECT * FROM reservations WHERE student_id = ? AND active = 1",
                            (student_id,)).fetchall():
        conn.execute("UPDATE reservations SET active = 0 WHERE reservation_id = ?", (res["reservation_id"],))
        book = register.get_book(conn, res["book_id"])
        if res["expires_on"] and book and book["status"] == states.RESERVED:
            states.set_status(conn, res["book_id"], states.AVAILABLE)
            nxt = register.active_reservation(conn, res["book_id"])
            if nxt:
                states.set_status(conn, res["book_id"], states.RESERVED)
                conn.execute("UPDATE reservations SET expires_on = ? WHERE reservation_id = ?",
                             ((date.today() + timedelta(days=settings.reservation_hold_days)).isoformat(),
                              nxt["reservation_id"]))


def restore(conn, student_id: str, user: str) -> None:
    s = get(conn, student_id)
    if s is None:
        raise RosterError(f"No student {student_id}.")
    if s["status"] != INACTIVE:
        raise RosterError(f"{s['name']} is already active.")
    conn.execute("UPDATE students SET status = ?, removed_at = NULL WHERE student_id = ?",
                 (ACTIVE, s["student_id"]))
    activity.log(conn, user, "RESTORE_STUDENT", f"{s['student_id']} {s['name']} restored to Active", "manual")
    conn.commit()


def inactive_message(student) -> str:
    return (f"{student['name']} ({student['student_id']}) was removed and is inactive, so they cannot "
            "borrow or reserve books. The librarian can restore them on the Students page.")
