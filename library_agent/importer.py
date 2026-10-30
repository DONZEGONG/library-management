"""Import the existing register from CSV (PLAN 1.5).

books.csv:    book_id,name,author,copy_number,condition_notes
students.csv: student_id,name,grade,contact_email
Re-importing updates names and details but never touches status or loans.
"""
import csv
import sqlite3


def _rows(path: str):
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            yield {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}


def import_books(conn: sqlite3.Connection, path: str) -> int:
    count = 0
    for r in _rows(path):
        if not r.get("book_id") or not r.get("name"):
            continue
        conn.execute(
            "INSERT INTO books (book_id, name, author, copy_number, condition_notes)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(book_id) DO UPDATE SET name = excluded.name, author = excluded.author,"
            " copy_number = excluded.copy_number, condition_notes = excluded.condition_notes",
            (r["book_id"], r["name"], r.get("author", ""), int(r.get("copy_number") or 1),
             r.get("condition_notes", "")),
        )
        count += 1
    conn.commit()
    return count


def import_students(conn: sqlite3.Connection, path: str) -> int:
    count = 0
    for r in _rows(path):
        if not r.get("student_id") or not r.get("name"):
            continue
        # Only the four fields library work needs are read; any other
        # columns in the spreadsheet are ignored (Layer 4).
        conn.execute(
            "INSERT INTO students (student_id, name, grade, contact_email) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(student_id) DO UPDATE SET name = excluded.name, grade = excluded.grade,"
            " contact_email = excluded.contact_email",
            (r["student_id"], r["name"], r.get("grade", ""), r.get("contact_email", "")),
        )
        count += 1
    conn.commit()
    return count
