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
    app = create_app(db, agent_factory=lambda conn: LibraryAgent(conn, client=fake))
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
