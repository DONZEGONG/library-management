"""Phase 5: register a borrowing through the chat, approve it, see the
register update."""
import pytest
from fastapi.testclient import TestClient

from conftest import ROOT, FakeClient, text_block, tool_block
from library_agent import importer
from library_agent.agent import LibraryAgent
from library_agent.config import settings
from library_agent.db import connect


@pytest.fixture
def web(tmp_path, monkeypatch):
    members = tmp_path / "members.csv"
    members.write_text("name,email,role\nLib Rarian,lib@s.example,librarian\nMem Ber,mem@s.example,committee\n")
    monkeypatch.setattr(settings, "members_file", str(members))
    monkeypatch.setattr(settings, "auth_mode", "dev")
    db = str(tmp_path / "web.db")
    fake = FakeClient([
        [tool_block("save_borrowing_details", {"book_id": "B-101", "student": "S-001"})],
        [text_block("Action #1 is waiting for approval.")],
        [text_block("Approved.")],
    ])
    from library_agent.web.app import create_app
    app = create_app(db, agent_factory=lambda conn: LibraryAgent(conn, client=fake), ai_enabled=True)
    c = connect(db)
    importer.import_books(c, str(ROOT / "sample_data" / "books.csv"))
    importer.import_students(c, str(ROOT / "sample_data" / "students.csv"))
    c.close()
    return TestClient(app)


def test_borrow_approve_register_flow(web):
    assert web.get("/register", follow_redirects=False).status_code == 303  # must sign in
    web.post("/login", data={"email": "mem@s.example"})
    r = web.post("/chat", data={"message": "Asha Rao is borrowing B-101"})
    assert "waiting for approval" in r.text
    r = web.get("/approvals")
    assert "Asha Rao" in r.text and "Charlotte" in r.text and "2026" in r.text
    r = web.post("/approvals/1/approve")
    assert "Loan saved" in r.text
    r = web.get("/register")
    assert "Borrowed" in r.text and "Asha Rao" in r.text


def test_librarian_page_restricted(web):
    web.post("/login", data={"email": "mem@s.example"})
    assert web.get("/librarian").status_code == 403
    web.get("/logout")
    web.post("/login", data={"email": "lib@s.example"})
    assert web.get("/librarian").status_code == 200
    r = web.post("/librarian/delete", data={"table": "books", "record_id": "B-401", "reason": ""})
    assert r.status_code == 422 or "reason is required" in r.text


def test_reject_needs_reason(web):
    web.post("/login", data={"email": "mem@s.example"})
    web.post("/chat", data={"message": "Asha Rao is borrowing B-101"})
    r = web.post("/approvals/1/reject", data={"reason": ""})
    assert "reason is required" in r.text
    r = web.post("/approvals/1/reject", data={"reason": "wrong student"})
    assert "rejected" in r.text
    assert "wrong student" in web.get("/corrections").text


# ---------------------------------------------------------------- Desk (no AI)

@pytest.fixture
def desk(tmp_path, monkeypatch):
    members = tmp_path / "members.csv"
    members.write_text("name,email,role\nLib Rarian,lib@s.example,librarian\nMem Ber,mem@s.example,committee\n")
    monkeypatch.setattr(settings, "members_file", str(members))
    monkeypatch.setattr(settings, "auth_mode", "dev")
    db = str(tmp_path / "desk.db")

    def no_ai(conn):
        raise AssertionError("the AI must not be used when it is switched off")

    from library_agent.web.app import create_app
    app = create_app(db, agent_factory=no_ai, ai_enabled=False)
    c = connect(db)
    importer.import_books(c, str(ROOT / "sample_data" / "books.csv"))
    importer.import_students(c, str(ROOT / "sample_data" / "students.csv"))
    c.close()
    client = TestClient(app)
    client.post("/login", data={"email": "mem@s.example"})
    return client


def test_ai_off_home_goes_to_desk_and_chat_hidden(desk):
    r = desk.get("/")
    assert r.url.path == "/desk" and "Library desk" in r.text
    assert 'href="/">Chat' not in r.text
    assert desk.post("/chat", data={"message": "hi"}).status_code == 404


def test_desk_borrow_waits_for_approval_then_saves(desk):
    r = desk.post("/desk/borrow", data={"book_id": "b-101", "student": "S-001"})
    assert "waiting for a committee member" in r.text
    r = desk.get("/approvals")
    assert "Asha Rao" in r.text and "Desk entry by Mem Ber" in r.text
    assert "Borrowed" not in desk.get("/register").text.split("Asha Rao")[0][-200:]
    assert "Loan saved" in desk.post("/approvals/1/approve").text
    r = desk.get("/register")
    assert "Borrowed" in r.text and "Asha Rao" in r.text


def test_desk_return_and_renew_go_through_checks(desk):
    desk.post("/desk/borrow", data={"book_id": "B-101", "student": "S-001"})
    # the loan is not saved until approved, so it cannot be renewed yet
    r = desk.post("/desk/renew", data={"book_id": "B-101"})
    assert "Not saved" in r.text and "no open loan" in r.text
    desk.post("/approvals/1/approve")
    assert "waiting for a committee member" in desk.post("/desk/renew", data={"book_id": "B-101"}).text
    desk.post("/approvals/2/approve")
    r = desk.post("/desk/return", data={"book_id": "B-101", "condition": "damaged", "notes": "torn cover"})
    assert "waiting for a committee member" in r.text
    assert "torn cover" in desk.get("/approvals").text


def test_desk_errors_and_lookup(desk):
    assert "Please fill in: student" in desk.post("/desk/borrow", data={"book_id": "B-101"}).text
    assert "No book with ID" in desk.post("/desk/borrow", data={"book_id": "X-999", "student": "S-001"}).text
    r = desk.get("/desk", params={"q": "Asha"})
    assert "S-001" in r.text
    assert "Reserved for" in desk.post("/desk/reserve", data={"book_id": "B-101", "student": "S-001"}).text
    r = desk.post("/desk/refer", data={"notes": "Fine question for Asha", "student": "S-001"})
    assert "Referred to the librarian" in r.text
    assert desk.post("/desk/delete", data={"book_id": "B-101"}).status_code == 404
