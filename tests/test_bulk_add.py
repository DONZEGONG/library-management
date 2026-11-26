"""MINI SPEC: bulk add books, duplicate handling, add/remove students, activity log."""
import io
import json
import time

import pytest
from fastapi.testclient import TestClient

from conftest import ROOT, FRIDAY
from library_agent import activity, approvals, catalogue, importer, roster, uploads
from library_agent.config import settings
from library_agent.tools import ToolContext, run_tool

WINGS_ISBN = "9788173711466"   # a valid ISBN-13


def row(title="", author="", copies="", **kw):
    return {"title": title, "author": author, "copies": copies, **kw}


# ---------------------------------------------------------------- checks and the preview

def test_isbn_check_digits():
    assert catalogue.isbn_valid("9788173711466")
    assert catalogue.isbn_valid("0306406152")
    assert not catalogue.isbn_valid("9788173711467")
    assert catalogue.isbn13("0306406152") == "9780306406157"


def test_statuses_new_exists_error(conn):
    result = catalogue.check(conn, [
        row("Harry Potter and the Philosopher's Stone", "J.K. Rowling", "5"),
        row("Wings of Fire", "A.P.J. Abdul Kalam", "3"),          # in the sample catalogue
        row("", "Someone"),                                       # title missing
        row("Odd Book", "X", isbn="12345"),                        # bad ISBN
        row("Zero", "Y", "0"),                                     # 0 copies rejected
    ])
    s = [r["status"] for r in result["rows"]]
    assert s == ["new", "exists", "error", "error", "error"]
    assert result["rows"][0]["copies"] == "5"
    assert "already in the library with 1 copy" in result["rows"][1]["message"]
    assert "Title is missing" in result["rows"][2]["message"]
    assert "not valid" in result["rows"][3]["message"]
    assert "0 and negative" in result["rows"][4]["message"]
    assert not result["ready"]


def test_empty_copies_defaults_to_one(conn):
    r = catalogue.check(conn, [row("New Title", "New Author")])["rows"][0]
    assert r["copies"] == "1" and r["status"] == "new"


def test_fuzzy_match_and_title_only_for_authorless_books(conn):
    conn.execute("INSERT INTO books (book_id, name) VALUES ('B-900', 'Panchatantra Stories')")
    rows = catalogue.check(conn, [row("Charlottes Web", "E B White"),
                                  row("Panchatantra Stories", "Vishnu Sharma")])["rows"]
    assert rows[0]["status"] == "exists" and rows[0]["match"]["total"] == 2
    assert rows[1]["status"] == "exists" and rows[1]["match"]["book_id"] == "B-900"


def test_same_book_twice_in_upload_is_merged(conn):
    result = catalogue.check(conn, [row("Brand New", "Author A", "2"), row("Brand new", "Author A", "3")])
    assert len(result["rows"]) == 1 and result["rows"][0]["copies"] == "5"
    assert "merged" in result["notices"][0]


def test_row_limit(conn):
    with pytest.raises(catalogue.CatalogueError):
        catalogue.check(conn, [row("T", "A")] * (uploads.MAX_ROWS + 1))


# ---------------------------------------------------------------- saving

def test_save_adds_copies_and_logs(conn):
    result = catalogue.save(conn, [row("Brand New", "Author A", "3", source="voice")], "Lib")
    assert result["added"] == 1 and result["added_copies"] == 3
    ids = [r[0] for r in conn.execute("SELECT book_id FROM books WHERE name = 'Brand New' ORDER BY copy_number")]
    assert ids == ["B-402", "B-402-2", "B-402-3"]
    log = activity.recent(conn)[0]
    assert log["action"] == "BULK_ADD" and log["source"] == "voice" and "Brand New" in log["details"]


def test_save_refuses_until_existing_book_answered(conn):
    with pytest.raises(catalogue.CatalogueError):
        catalogue.save(conn, [row("Wings of Fire", "A. P. J. Abdul Kalam")], "Lib")
    assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 5   # nothing saved


def test_set_total_and_add_more(conn):
    r = catalogue.save(conn, [row("Wings of Fire", "A. P. J. Abdul Kalam", decision={"mode": "set", "value": "3"})], "Lib")
    assert r["updated"] == 1
    assert conn.execute("SELECT COUNT(*) FROM books WHERE name = 'Wings of Fire' AND deleted_at IS NULL").fetchone()[0] == 3
    catalogue.save(conn, [row("Wings of Fire", "A. P. J. Abdul Kalam", decision={"mode": "add", "value": "2"})], "Lib")
    ids = sorted(r[0] for r in conn.execute("SELECT book_id FROM books WHERE name = 'Wings of Fire'"))
    assert ids == ["B-201", "B-201-2", "B-201-3", "B-201-4", "B-201-5"]
    assert activity.recent(conn)[0]["action"] == "UPDATE_COPIES"


def test_total_cannot_go_below_issued(conn):
    conn.execute("UPDATE books SET status = 'Borrowed' WHERE book_id IN ('B-101', 'B-102')")
    r = catalogue.check(conn, [row("Charlotte's Web", "E. B. White", decision={"mode": "set", "value": "1"})])["rows"][0]
    assert r["message"] == "2 copies are currently issued. Total can't be less than 2."


def test_lowering_total_removes_shelf_copies_only(conn):
    conn.execute("UPDATE books SET status = 'Borrowed' WHERE book_id = 'B-101'")
    catalogue.save(conn, [row("Charlotte's Web", "E. B. White", decision={"mode": "set", "value": "1"})], "Lib")
    left = [r[0] for r in conn.execute("SELECT book_id FROM books WHERE name = ? AND deleted_at IS NULL", ("Charlotte's Web",))]
    assert left == ["B-101"]


def test_skip_and_different_book(conn):
    r = catalogue.save(conn, [row("Wings of Fire", "A. P. J. Abdul Kalam", decision={"mode": "skip"}),
                              row("Malgudi Days", "R. K. Narayan", decision={"mode": "new"}),
                              row("Bad", "", skip=True)], "Lib")
    assert (r["added"], r["updated"], r["skipped"]) == (1, 0, 2)
    assert r["message"] == "1 added, 0 updated, 2 skipped."
    assert conn.execute("SELECT COUNT(*) FROM books WHERE name = 'Malgudi Days'").fetchone()[0] == 2


def test_hundred_rows_under_ten_seconds(conn):
    rows = [row(f"Speed Test Book {i}", f"Writer {i}", "2") for i in range(100)]
    start = time.monotonic()
    assert catalogue.check(conn, rows)["ready"]
    catalogue.save(conn, rows, "Lib")
    assert time.monotonic() - start < 10


# ---------------------------------------------------------------- file reading

def test_read_csv_and_guess_columns():
    data = "Book Name,Writer,No of Copies,ISBN\nWings of Fire,Kalam,3,9788173711466\n".encode()
    headers, rows = uploads.read_table("books.csv", data)
    guess = uploads.guess_columns(headers, catalogue.SYNONYMS)
    assert guess["title"] == 0 and guess["author"] == 1 and guess["copies"] == 2 and guess["isbn"] == 3
    assert rows == [["Wings of Fire", "Kalam", "3", "9788173711466"]]


def test_read_excel():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    wb.active.append(["Title", "Author", "Year", "ISBN"])
    wb.active.append(["A Book", "An Author", 2001, 9788173711466])
    buf = io.BytesIO()
    wb.save(buf)
    headers, rows = uploads.read_table("x.xlsx", buf.getvalue())
    assert rows == [["A Book", "An Author", "2001", "9788173711466"]]


# ---------------------------------------------------------------- students

def test_add_student_and_duplicate_id(conn):
    roster.add_student(conn, {"student_id": "s-500", "name": "New Kid", "grade": "5", "section": "B"}, "Lib")
    s = roster.get(conn, "S-500")
    assert s["status"] == "Active" and s["section"] == "B"
    with pytest.raises(roster.RosterError, match="Student ID already exists"):
        roster.add_student(conn, {"student_id": "S-500", "name": "Other"}, "Lib")
    assert activity.recent(conn)[0]["action"] == "ADD_STUDENT"


def test_bulk_students(conn):
    rows = [{"student_id": "S-600", "name": "A"}, {"student_id": "S-001", "name": "Dup"},
            {"student_id": "S-601", "name": ""}, {"student_id": "S-600", "name": "Twice"}]
    checked = roster.check_bulk(conn, rows)
    assert [r["status"] for r in checked] == ["new", "error", "error", "error"]
    assert roster.save_bulk(conn, rows, "Lib")["message"] == "1 added, 3 skipped."


def test_remove_blocked_by_loans_and_fines(conn, ctx):
    run_tool(ctx, "save_borrowing_details", {"book_id": "B-101", "student": "S-001"})
    approvals.approve(conn, 1, "Lib", today=FRIDAY)
    loan_id = conn.execute("SELECT loan_id FROM loans").fetchone()[0]
    conn.execute("INSERT INTO fines (loan_id, reason, amount, set_by, created_at) VALUES (?, 'late', 50, 'Lib', 'x')", (loan_id,))
    assert roster.blockers(conn, "S-001") == "Student has 1 book not returned and a ₹50 fine. Clear these first."
    with pytest.raises(roster.RosterError):
        roster.remove(conn, "S-001", "Lib")
    assert roster.get(conn, "S-001")["status"] == "Active"


def test_removed_student_kept_cannot_borrow_and_can_be_restored(conn, ctx):
    roster.remove(conn, "S-002", "Lib")
    s = roster.get(conn, "S-002")
    assert s["status"] == "Inactive" and s["removed_at"]
    result, is_error = run_tool(ctx, "save_borrowing_details", {"book_id": "B-201", "student": "S-002"})
    assert is_error and "inactive" in result["error"]
    result, is_error = run_tool(ctx, "reserve_book", {"book_id": "B-201", "student": "S-002"})
    assert is_error
    roster.restore(conn, "S-002", "Lib")
    assert roster.get(conn, "S-002")["status"] == "Active"
    assert [r["action"] for r in activity.recent(conn)][:2] == ["RESTORE_STUDENT", "REMOVE_STUDENT"]


def test_pending_loan_refused_if_student_removed_meanwhile(conn, ctx):
    run_tool(ctx, "save_borrowing_details", {"book_id": "B-201", "student": "S-003"})
    roster.remove(conn, "S-003", "Lib")
    with pytest.raises(approvals.ApprovalError, match="removed"):
        approvals.approve(conn, 1, "Lib", today=FRIDAY)


# ---------------------------------------------------------------- web

@pytest.fixture
def web(tmp_path, db_target, monkeypatch):
    members = tmp_path / "members.csv"
    members.write_text("name,email,role\nLib Rarian,lib@s.example,librarian\nMem Ber,mem@s.example,committee\n")
    monkeypatch.setattr(settings, "members_file", str(members))
    monkeypatch.setattr(settings, "members_csv", "")
    monkeypatch.setattr(settings, "auth_mode", "dev")
    from library_agent.db import connect
    from library_agent.web.app import create_app
    app = create_app(db_target, ai_enabled=False)
    c = connect(db_target)
    importer.import_books(c, str(ROOT / "sample_data" / "books.csv"))
    importer.import_students(c, str(ROOT / "sample_data" / "students.csv"))
    c.close()
    return TestClient(app)


def test_pages_are_librarian_only(web):
    web.post("/login", data={"email": "mem@s.example"})
    for path in ("/books/add", "/students", "/activity"):
        assert web.get(path).status_code == 403
    assert web.post("/api/books/check", json={"rows": []}).status_code == 403


def test_web_bulk_add_flow(web):
    web.post("/login", data={"email": "lib@s.example"})
    assert "Voice Add" in web.get("/books/add").text
    csv_bytes = b"Title,Author,Copies\nNew Book,New Author,2\nWings of Fire,A. P. J. Abdul Kalam,\n"
    parsed = web.post("/api/books/parse", files={"file": ("b.csv", csv_bytes, "text/csv")}).json()
    assert parsed["guess"]["title"] == 0 and len(parsed["rows"]) == 2
    rows = [{"title": r[0], "author": r[1], "copies": r[2], "source": "file"} for r in parsed["rows"]]
    checked = web.post("/api/books/check", json={"rows": rows}).json()
    assert [r["status"] for r in checked["rows"]] == ["new", "exists"]
    assert web.post("/api/books/save", json={"rows": rows}).status_code == 400
    rows[1]["decision"] = {"mode": "add", "value": "1"}
    saved = web.post("/api/books/save", json={"rows": rows}).json()
    assert saved["message"] == "1 added (2 copies), 1 updated, 0 skipped."
    assert "BULK_ADD" in web.get("/activity").text


def test_web_students_flow(web):
    web.post("/login", data={"email": "lib@s.example"})
    r = web.post("/students/add", data={"student_id": "S-001", "name": "Dup"})
    assert "Student ID already exists" in r.text
    r = web.post("/students/add", data={"student_id": "S-777", "name": "Lucky Seven", "grade": "4"})
    assert "Added Lucky Seven" in r.text
    up = web.post("/students/upload", files={"file": ("s.csv", b"ID,Full Name,Class\nS-800,Bulk One,3\n", "text/csv")})
    assert "Match the columns" in up.text
    form = {"rows": json.dumps([["S-800", "Bulk One", "3"]]), "col_student_id": "0", "col_name": "1", "col_grade": "2"}
    pv = web.post("/students/upload/preview", data=form)
    assert "1 new" in pv.text
    payload = json.dumps([{"student_id": "S-800", "name": "Bulk One", "grade": "3"}])
    assert "1 added" in web.post("/students/upload/save", data={"rows": payload}).text
    assert "Yes, remove" in web.get("/students/S-800/remove").text
    r = web.post("/students/S-800/remove")
    assert "inactive" in r.text
    assert "S-800" in web.get("/students?show=Inactive").text
    assert "restored" in web.post("/students/S-800/restore").text
