"""Read-side queries over the register (PLAN 1.4 and the read tools)."""
import sqlite3

from .states import IN_LIBRARY, OVERDUE


def totals(conn: sqlite3.Connection) -> dict:
    placeholders = ",".join("?" * len(IN_LIBRARY))
    total = conn.execute("SELECT COUNT(*) FROM books WHERE deleted_at IS NULL").fetchone()[0]
    current = conn.execute(
        f"SELECT COUNT(*) FROM books WHERE deleted_at IS NULL AND status IN ({placeholders})",
        IN_LIBRARY,
    ).fetchone()[0]
    by_status = {
        r["status"]: r["n"]
        for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM books WHERE deleted_at IS NULL GROUP BY status"
        )
    }
    return {"total_books": total, "current_books": current, "by_status": by_status}


def get_book(conn, book_id: str):
    return conn.execute(
        "SELECT * FROM books WHERE book_id = ? AND deleted_at IS NULL", (book_id,)
    ).fetchone()


def get_student(conn, student_id: str):
    return conn.execute(
        "SELECT * FROM students WHERE student_id = ? AND deleted_at IS NULL", (student_id,)
    ).fetchone()


def find_books(conn, query: str, limit: int = 20) -> list[dict]:
    like = f"%{query.strip()}%"
    rows = conn.execute(
        "SELECT book_id, name, author, copy_number, status FROM books"
        " WHERE deleted_at IS NULL AND (book_id = ? OR name LIKE ? OR author LIKE ?)"
        " ORDER BY name, copy_number LIMIT ?",
        (query.strip(), like, like, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def find_students(conn, query: str, limit: int = 10) -> list:
    like = f"%{query.strip()}%"
    return conn.execute(
        "SELECT * FROM students WHERE deleted_at IS NULL AND (student_id = ? OR name LIKE ?)"
        " ORDER BY name LIMIT ?",
        (query.strip(), like, limit),
    ).fetchall()


def open_loan_for_book(conn, book_id: str):
    return conn.execute(
        "SELECT * FROM loans WHERE book_id = ? AND return_date IS NULL AND deleted_at IS NULL"
        " ORDER BY loan_id DESC LIMIT 1",
        (book_id,),
    ).fetchone()


def open_loans_for_student(conn, student_id: str) -> list:
    return conn.execute(
        "SELECT l.*, b.name AS book_name, b.status AS book_status FROM loans l"
        " JOIN books b ON b.book_id = l.book_id"
        " WHERE l.student_id = ? AND l.return_date IS NULL AND l.deleted_at IS NULL",
        (student_id,),
    ).fetchall()


def active_reservation(conn, book_id: str):
    """The reservation that is currently holding the book (or first in the queue)."""
    return conn.execute(
        "SELECT * FROM reservations WHERE book_id = ? AND active = 1"
        " ORDER BY reserved_on, reservation_id LIMIT 1",
        (book_id,),
    ).fetchone()


def unpaid_fines(conn, student_id: str) -> list:
    return conn.execute(
        "SELECT f.* FROM fines f JOIN loans l ON l.loan_id = f.loan_id"
        " WHERE l.student_id = ? AND f.status IN ('unpaid', 'reduced')",
        (student_id,),
    ).fetchall()


def lost_or_damaged_history(conn, student_id: str) -> list:
    return conn.execute(
        "SELECT kind, summary, decided_at FROM human_decisions"
        " WHERE student_id = ? AND kind IN ('lost_confirmed', 'damage_assessment')"
        " ORDER BY decided_at DESC",
        (student_id,),
    ).fetchall()


def overdue_history_count(conn, student_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM loans WHERE student_id = ? AND deleted_at IS NULL"
        " AND return_date IS NOT NULL AND return_date > due_date",
        (student_id,),
    ).fetchone()[0]


def availability(conn, book_id: str) -> dict | None:
    book = get_book(conn, book_id)
    if book is None:
        return None
    result = {"book_id": book_id, "name": book["name"], "status": book["status"]}
    loan = open_loan_for_book(conn, book_id)
    if loan:
        student = conn.execute(
            "SELECT name, grade FROM students WHERE student_id = ?", (loan["student_id"],)
        ).fetchone()
        result["with_student"] = f"{student['name']} (grade {student['grade']})"
        result["due_date"] = loan["due_date"]
    res = active_reservation(conn, book_id)
    if res:
        result["reserved"] = True
        result["reservation_expires_on"] = res["expires_on"]
    return result


def borrowing_flags(conn, student_id: str) -> list[str]:
    """Warnings a human should see before approving a loan (PLAN 4.4).
    These never refuse a loan on their own."""
    from .config import settings

    flags = []
    loans = open_loans_for_student(conn, student_id)
    if len(loans) >= settings.loan_limit:
        flags.append(f"Student already has {len(loans)} book(s); the loan limit is {settings.loan_limit}.")
    overdue = [l for l in loans if l["book_status"] == OVERDUE]
    if overdue:
        flags.append(f"Student has {len(overdue)} overdue book(s): "
                     + ", ".join(l["book_name"] for l in overdue) + ".")
    fines = unpaid_fines(conn, student_id)
    if fines:
        flags.append(f"Student has {len(fines)} unpaid fine(s) totalling {sum(f['amount'] for f in fines):.2f}.")
    history = lost_or_damaged_history(conn, student_id)
    if history:
        flags.append(f"Student has {len(history)} past lost or damaged book report(s).")
    return flags


def overdue_books(conn) -> list:
    return conn.execute(
        "SELECT b.book_id, b.name, l.loan_id, l.due_date, s.name AS student_name, s.grade"
        " FROM books b JOIN loans l ON l.book_id = b.book_id AND l.return_date IS NULL"
        " AND l.deleted_at IS NULL JOIN students s ON s.student_id = l.student_id"
        " WHERE b.status = ? AND b.deleted_at IS NULL ORDER BY l.due_date",
        (OVERDUE,),
    ).fetchall()
