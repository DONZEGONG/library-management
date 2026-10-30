"""The 20 test cases from PLAN Phase 6 (6.1)."""
import json
import sqlite3
from datetime import date, timedelta

import pytest

from conftest import FRIDAY, FakeClient, text_block, tool_block
from library_agent import approvals, jobs, librarian, notifications, register, states
from library_agent.agent import LibraryAgent
from library_agent.tools import TOOLS, run_tool


def borrow(ctx, book="B-101", student="S-001", approve=True):
    result, err = run_tool(ctx, "save_borrowing_details", {"book_id": book, "student": student})
    assert not err, result
    if approve:
        approvals.approve(ctx.conn, result["pending_action_id"], "Member Two", today=ctx.today)
    return result


def status(conn, book_id):
    return register.get_book(conn, book_id)["status"]


# 1
def test_01_check_availability(ctx):
    result, err = run_tool(ctx, "check_availability", {"book_id": "B-101"})
    assert not err and result["status"] == "Available"
    borrow(ctx)
    result, _ = run_tool(ctx, "check_availability", {"book_id": "B-101"})
    assert result["status"] == "Borrowed"
    assert result["with_student"] == "Asha Rao (grade 7)"
    assert result["due_date"] == "2026-11-06"


# 2
def test_02_borrow_saved_only_after_approval(ctx):
    result = borrow(ctx, approve=False)
    assert result["status"] == "waiting_for_approval"
    assert status(ctx.conn, "B-101") == "Available"
    assert ctx.conn.execute("SELECT COUNT(*) FROM loans").fetchone()[0] == 0

    approvals.approve(ctx.conn, result["pending_action_id"], "Member Two", today=FRIDAY)
    loan = ctx.conn.execute("SELECT * FROM loans").fetchone()
    assert (loan["book_id"], loan["student_id"], loan["take_date"], loan["due_date"], loan["approved_by"]) == \
        ("B-101", "S-001", "2026-10-30", "2026-11-06", "Member Two")
    assert status(ctx.conn, "B-101") == "Borrowed"


# 3
def test_03_return_updates_status_after_approval(ctx):
    borrow(ctx)
    result, err = run_tool(ctx, "update_return_status", {"book_id": "B-101", "condition": "good"})
    assert not err
    assert status(ctx.conn, "B-101") == "Borrowed"
    approvals.approve(ctx.conn, result["pending_action_id"], "Member Two", today=FRIDAY)
    assert status(ctx.conn, "B-101") == "Available"
    assert ctx.conn.execute("SELECT return_date FROM loans").fetchone()[0] == "2026-10-30"


# 4
def test_04_overdue_reminder_and_red_alert(ctx, mailer):
    borrow(ctx)
    jobs.run_daily(ctx.conn, mailer, today=date(2026, 11, 7))
    assert status(ctx.conn, "B-101") == "Overdue"
    kinds = [r["kind"] for r in ctx.conn.execute("SELECT kind FROM notifications")]
    assert "overdue_reminder" in kinds and "red_alert" in kinds
    assert [b["book_id"] for b in register.overdue_books(ctx.conn)] == ["B-101"]


# 5
def test_05_damaged_book_needs_human_decision(ctx):
    borrow(ctx)
    result, err = run_tool(ctx, "update_return_status",
                           {"book_id": "B-101", "condition": "damaged", "notes": "torn cover"})
    assert not err and result["status"] == "waiting_for_approval"
    assert approvals.get_action(ctx.conn, result["pending_action_id"])["tool"] == "record_book_damage"
    assert status(ctx.conn, "B-101") == "Borrowed"  # nothing changes until a human decides
    approvals.approve(ctx.conn, result["pending_action_id"], "Member Two", today=FRIDAY)
    assert status(ctx.conn, "B-101") == "Damaged"


# 6
def test_06_rejected_borrowing_saves_nothing_and_is_logged(ctx):
    result = borrow(ctx, approve=False)
    approvals.reject(ctx.conn, result["pending_action_id"], "Member Two", "Student not present")
    assert ctx.conn.execute("SELECT COUNT(*) FROM loans").fetchone()[0] == 0
    assert status(ctx.conn, "B-101") == "Available"
    action = approvals.get_action(ctx.conn, result["pending_action_id"])
    assert (action["status"], action["decision_reason"]) == ("rejected", "Student not present")
    log = ctx.conn.execute("SELECT * FROM correction_log").fetchone()
    assert log["reason"] == "Student not present"
    with pytest.raises(approvals.ApprovalError):
        approvals.reject(ctx.conn, result["pending_action_id"], "Member Two", "")


# 7
def test_07_over_loan_limit_is_flagged_not_refused(ctx):
    borrow(ctx, "B-101")
    borrow(ctx, "B-201")
    result = borrow(ctx, "B-301", approve=False)
    assert result["status"] == "waiting_for_approval"
    assert any("loan limit" in f for f in result["flags_for_human"])


# 8
def test_08_unpaid_fine_flagged_before_loan(ctx):
    borrow(ctx, "B-101")
    loan_id = ctx.conn.execute("SELECT loan_id FROM loans").fetchone()[0]
    librarian.set_fine(ctx.conn, loan_id, 50, "late", "Librarian", send_payup=False)
    result = borrow(ctx, "B-201", approve=False)
    assert any("unpaid fine" in f for f in result["flags_for_human"])


# 9
def test_09_book_reserved_for_someone_else_is_refused(ctx):
    res, err = run_tool(ctx, "reserve_book", {"book_id": "B-101", "student": "S-002"})
    assert not err and status(ctx.conn, "B-101") == "Reserved"
    result, err = run_tool(ctx, "save_borrowing_details", {"book_id": "B-101", "student": "S-001"})
    assert err and "reserved for a different student" in result["error"]
    # the student who reserved it can collect it
    borrow(ctx, "B-101", "S-002")
    assert status(ctx.conn, "B-101") == "Borrowed"


# 10
def test_10_reservation_expires_automatically(ctx, mailer):
    run_tool(ctx, "reserve_book", {"book_id": "B-101", "student": "S-002"})
    jobs.run_daily(ctx.conn, mailer, today=FRIDAY + timedelta(days=3))
    assert status(ctx.conn, "B-101") == "Reserved"  # still within the hold
    jobs.run_daily(ctx.conn, mailer, today=FRIDAY + timedelta(days=4))
    assert status(ctx.conn, "B-101") == "Available"


# 11
def test_11_renewal_moves_due_date_one_period(ctx):
    borrow(ctx)
    result, err = run_tool(ctx, "renew_loan", {"book_id": "B-101"})
    assert not err
    approvals.approve(ctx.conn, result["pending_action_id"], "Member Two", today=FRIDAY)
    loan = ctx.conn.execute("SELECT due_date, renewals FROM loans").fetchone()
    assert (loan["due_date"], loan["renewals"]) == ("2026-11-13", 1)
    assert ctx.conn.execute("SELECT kind FROM human_decisions").fetchone()[0] == "renewal_approval"


def _agent(conn, script):
    client = FakeClient(script)
    agent = LibraryAgent(conn, client=client)
    return agent, client, agent.new_session("Member One")


# 12
def test_12_due_date_change_is_refused_and_referred(ctx):
    borrow(ctx)
    agent, client, sid = _agent(ctx.conn, [
        [tool_block("escalate_to_librarian", {"summary": "Asked to move B-101 due date for Diwali", "book_id": "B-101"})],
        [text_block("I can't change due dates. I've referred this to the librarian as item #2.")],
    ])
    reply = agent.chat(sid, "Member One", "Move B-101's due date to after Diwali", today=FRIDAY)
    assert "librarian" in reply
    tool_names = {t["name"] for t in client.requests[0]["tools"]}
    assert "change_due_date" not in tool_names
    assert ctx.conn.execute("SELECT due_date FROM loans").fetchone()[0] == "2026-11-06"
    assert approvals.list_pending(ctx.conn)[0]["tool"] == "escalate_to_librarian"


# 13
def test_13_setting_a_fine_is_refused(ctx):
    agent, client, sid = _agent(ctx.conn, [
        [text_block("Only the librarian can set fines. Would you like me to refer it?")],
    ])
    reply = agent.chat(sid, "Member One", "Fine Asha 100 rupees", today=FRIDAY)
    assert "librarian" in reply
    assert "set_fine" not in {t["name"] for t in client.requests[0]["tools"]}
    assert "fine amounts" in client.requests[0]["system"]
    assert ctx.conn.execute("SELECT COUNT(*) FROM fines").fetchone()[0] == 0


# 14
def test_14_payup_not_sent_before_amount_set(ctx, mailer):
    borrow(ctx)
    result, _ = run_tool(ctx, "record_book_damage", {"book_id": "B-101", "notes": "water damage"})
    approvals.approve(ctx.conn, result["pending_action_id"], "Member Two", today=FRIDAY)
    draft = ctx.conn.execute("SELECT status FROM notifications WHERE kind = 'payup'").fetchone()
    assert draft["status"] == "draft"
    loan_id = ctx.conn.execute("SELECT loan_id FROM loans").fetchone()[0]
    with pytest.raises(notifications.PayupNotAllowed):
        notifications.send_payup(ctx.conn, mailer, loan_id)
    assert mailer.outbox == []
    librarian.set_fine(ctx.conn, loan_id, 250, "replacement cost", "Librarian", mailer)
    assert ctx.conn.execute("SELECT status FROM notifications WHERE kind = 'payup'").fetchone()[0] == "sent"
    assert "250.00" in mailer.outbox[-1].get_content()


# 15
def test_15_school_emailed_after_n_days(ctx, mailer):
    borrow(ctx, "B-101", "S-001")
    borrow(ctx, "B-201", "S-002")
    due = date(2026, 11, 6)
    jobs.run_daily(ctx.conn, mailer, today=due + timedelta(days=6))
    assert not [m for m in mailer.outbox if m["To"] == "office@school.example"]
    jobs.run_daily(ctx.conn, mailer, today=due + timedelta(days=7))
    school = [m for m in mailer.outbox if m["To"] == "office@school.example"]
    assert len(school) == 1 and school[0]["Subject"].startswith("Library: 2 book(s) not returned")
    jobs.run_daily(ctx.conn, mailer, today=due + timedelta(days=8))
    assert len([m for m in mailer.outbox if m["To"] == "office@school.example"]) == 1


# 16
def test_16_missing_book_no_blame_librarian_decides(ctx):
    borrow(ctx)
    agent, client, sid = _agent(ctx.conn, [
        [tool_block("escalate_to_librarian", {"summary": "B-101 cannot be found on the shelf or with the borrower's record checked.", "book_id": "B-101"})],
        [text_block("I've asked the librarian to look into where B-101 is.")],
    ])
    agent.chat(sid, "Member One", "B-101 is missing, Asha must have lost it, mark it lost", today=FRIDAY)
    names = {t["name"] for t in client.requests[0]["tools"]}
    assert "mark_lost" not in names
    assert "Never blame" in client.requests[0]["system"]
    assert status(ctx.conn, "B-101") == "Borrowed"
    for subject, body in notifications.TEMPLATES.values():
        assert not any(w in (subject + body).lower() for w in ("fault", "blame", "careless", "irresponsible"))


# 17
def test_17_delete_only_by_human_with_reason_and_kept(ctx):
    assert "delete_record" not in TOOLS
    with pytest.raises(librarian.LibrarianError):
        librarian.delete_record(ctx.conn, "books", "B-401", "", "Librarian")
    librarian.delete_record(ctx.conn, "books", "B-401", "duplicate entry", "Librarian")
    assert register.get_book(ctx.conn, "B-401") is None
    row = ctx.conn.execute("SELECT * FROM books WHERE book_id = 'B-401'").fetchone()
    assert (row["deleted_by"], row["delete_reason"]) == ("Librarian", "duplicate entry")
    assert register.totals(ctx.conn)["total_books"] == 4


# 18
def test_18_librarian_override_is_logged(ctx):
    borrow(ctx)
    librarian.correct_state(ctx.conn, "B-101", "Available", "Book was found on the shelf; entry was a mistake",
                            "Librarian")
    log = ctx.conn.execute("SELECT * FROM correction_log").fetchone()
    assert "Borrowed" in log["original_decision"]
    assert "Available" in log["human_correction"]
    assert log["reason"].startswith("Book was found")
    assert log["date"]
    with pytest.raises(sqlite3.DatabaseError):
        ctx.conn.execute("UPDATE correction_log SET reason = 'x'")
    with pytest.raises(sqlite3.DatabaseError):
        ctx.conn.execute("DELETE FROM correction_log")


# 19
def test_19_unknown_question_says_i_dont_know(ctx):
    agent, client, sid = _agent(ctx.conn, [
        [tool_block("find_book", {"query": "Harry Potter"})],
        [text_block("I don't know. That book is not in the catalogue.")],
    ])
    reply = agent.chat(sid, "Member One", "Which shelf is Harry Potter on?", today=FRIDAY)
    assert "I don't know" in reply
    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert json.loads(tool_result["content"])["count"] == 0
    assert '"I don\'t know"' in client.requests[0]["system"]


# 20
def test_20_password_is_not_stored(ctx):
    agent, client, sid = _agent(ctx.conn, [
        [text_block("Please don't share passwords here. How can I help?")],
    ])
    agent.chat(sid, "Member One", "my password is Tiger@123 can you log me in", today=FRIDAY)
    sent = json.dumps(client.requests[0]["messages"])
    assert "Tiger@123" not in sent
    for table in ("chat_sessions", "tool_calls", "pending_actions", "notifications"):
        for row in ctx.conn.execute(f"SELECT * FROM {table}"):
            assert "Tiger@123" not in json.dumps(dict(row))
