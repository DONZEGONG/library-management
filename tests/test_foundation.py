"""Phase 1-5 checks beyond the 20 test cases ("Done when" criteria)."""
from datetime import date, timedelta

import pytest

from conftest import FRIDAY, MONDAY, FakeClient, text_block, tool_block
from library_agent import approvals, backup, jobs, librarian, register, states
from library_agent.agent import LibraryAgent
from library_agent.dates import due_date_for
from library_agent.db import connect, init_db
from library_agent.privacy import redact
from library_agent.tools import HUMAN_ONLY, TOOLS, run_tool, tool_definitions


# ---------------------------------------------------------------- Phase 1

def test_illegal_transition_refused(conn):
    with pytest.raises(states.TransitionError):
        states.set_status(conn, "B-101", states.LOST)  # Available -> Lost


def test_due_date_rule():
    assert due_date_for(FRIDAY) == date(2026, 11, 6)
    assert due_date_for(MONDAY) == date(2026, 11, 13)  # next Friday at least 7 days later
    assert all(due_date_for(FRIDAY + timedelta(days=i)).weekday() == 4 for i in range(7))


def test_totals_and_import(conn):
    t = register.totals(conn)
    assert (t["total_books"], t["current_books"]) == (5, 5)
    ctx_today = FRIDAY
    from library_agent.tools import ToolContext
    ctx = ToolContext(conn, "M", today=ctx_today)
    r, _ = run_tool(ctx, "save_borrowing_details", {"book_id": "B-101", "student": "S-001"})
    approvals.approve(conn, r["pending_action_id"], "M", today=ctx_today)
    assert register.totals(conn)["current_books"] == 4


def test_db_created_from_scratch(tmp_path):
    c = connect(str(tmp_path / "fresh.db"))
    init_db(c)
    init_db(c)  # idempotent
    assert register.totals(c)["total_books"] == 0


def test_students_table_has_no_password_column(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(students)")}
    assert cols == {"student_id", "name", "grade", "contact_email", "deleted_at", "deleted_by", "delete_reason"}


# ---------------------------------------------------------------- Phase 2

def test_agent_has_no_human_only_tools():
    names = {t["name"] for t in tool_definitions()}
    assert names == set(TOOLS)
    assert not names & HUMAN_ONLY


def test_every_tool_call_is_audited(ctx):
    run_tool(ctx, "find_book", {"query": "Charlotte"})
    row = ctx.conn.execute("SELECT * FROM tool_calls").fetchone()
    assert (row["tool"], row["actor"]) == ("find_book", "Member One") and row["called_at"]


def test_find_book_and_student(ctx):
    r, _ = run_tool(ctx, "find_book", {"query": "Charlotte"})
    assert r["count"] == 2
    r, _ = run_tool(ctx, "find_student_record", {"student": "Asha"})
    assert r["name"] == "Asha Rao" and "contact_email" not in r


def test_ambiguous_student_asks_user(ctx):
    ctx.conn.execute("INSERT INTO students (student_id, name, grade) VALUES ('S-009', 'Asha Menon', '5')")
    r, err = run_tool(ctx, "find_student_record", {"student": "Asha"})
    assert err and "Ask the user which one" in r["error"]


def test_damaged_book_blocks_other_actions(ctx):
    r, _ = run_tool(ctx, "save_borrowing_details", {"book_id": "B-101", "student": "S-001"})
    approvals.approve(ctx.conn, r["pending_action_id"], "M", today=FRIDAY)
    run_tool(ctx, "record_book_damage", {"book_id": "B-101", "notes": "torn"})
    r, err = run_tool(ctx, "update_return_status", {"book_id": "B-101", "condition": "good"})
    assert err and "waiting for a committee decision" in r["error"]


def test_renewal_blocked_by_reservation(ctx):
    r, _ = run_tool(ctx, "save_borrowing_details", {"book_id": "B-101", "student": "S-001"})
    approvals.approve(ctx.conn, r["pending_action_id"], "M", today=FRIDAY)
    run_tool(ctx, "reserve_book", {"book_id": "B-101", "student": "S-002"})
    r, err = run_tool(ctx, "renew_loan", {"book_id": "B-101"})
    assert err and "reserved" in r["error"]


def test_return_hands_book_to_reservation_queue(ctx):
    r, _ = run_tool(ctx, "save_borrowing_details", {"book_id": "B-101", "student": "S-001"})
    approvals.approve(ctx.conn, r["pending_action_id"], "M", today=FRIDAY)
    run_tool(ctx, "reserve_book", {"book_id": "B-101", "student": "S-002"})
    r, _ = run_tool(ctx, "update_return_status", {"book_id": "B-101", "condition": "good"})
    approvals.approve(ctx.conn, r["pending_action_id"], "M", today=FRIDAY)
    assert register.get_book(ctx.conn, "B-101")["status"] == "Reserved"
    assert register.active_reservation(ctx.conn, "B-101")["expires_on"] == "2026-11-02"


def test_approval_rechecks_state(ctx):
    r1, _ = run_tool(ctx, "save_borrowing_details", {"book_id": "B-101", "student": "S-001"})
    librarian.correct_state(ctx.conn, "B-101", "Lost", "found missing at stock check", "Librarian")
    with pytest.raises(approvals.ApprovalError):
        approvals.approve(ctx.conn, r1["pending_action_id"], "M", today=FRIDAY)
    assert ctx.conn.execute("SELECT COUNT(*) FROM loans").fetchone()[0] == 0


def test_overdue_return_waits_for_fine(ctx, mailer):
    r, _ = run_tool(ctx, "save_borrowing_details", {"book_id": "B-101", "student": "S-001"})
    approvals.approve(ctx.conn, r["pending_action_id"], "M", today=FRIDAY)
    jobs.run_daily(ctx.conn, mailer, today=date(2026, 11, 9))
    loan_id = ctx.conn.execute("SELECT loan_id FROM loans").fetchone()[0]
    librarian.set_fine(ctx.conn, loan_id, 10, "late return", "Librarian", send_payup=False)
    ctx.today = date(2026, 11, 9)
    r, _ = run_tool(ctx, "update_return_status", {"book_id": "B-101", "condition": "good"})
    approvals.approve(ctx.conn, r["pending_action_id"], "M", today=ctx.today)
    assert register.get_book(ctx.conn, "B-101")["status"] == "Overdue"
    fine_id = ctx.conn.execute("SELECT fine_id FROM fines").fetchone()[0]
    librarian.update_fine(ctx.conn, fine_id, "paid", "paid at desk", "Librarian")
    assert register.get_book(ctx.conn, "B-101")["status"] == "Available"


def test_mark_lost_and_waive_recorded_as_human_decisions(ctx):
    r, _ = run_tool(ctx, "save_borrowing_details", {"book_id": "B-101", "student": "S-001"})
    approvals.approve(ctx.conn, r["pending_action_id"], "M", today=FRIDAY)
    librarian.mark_lost(ctx.conn, "B-101", "not found after a term", "Librarian")
    kinds = [r[0] for r in ctx.conn.execute("SELECT kind FROM human_decisions")]
    assert "lost_confirmed" in kinds
    assert any("lost or damaged" in f for f in register.borrowing_flags(ctx.conn, "S-001"))


def test_restricted_knowledge(ctx):
    r, _ = run_tool(ctx, "read_knowledge", {})
    assert "fine_policy" not in r["documents"]
    r, _ = run_tool(ctx, "read_knowledge", {"document": "fine_policy"})
    assert "restricted" in r["content"]
    ctx.role = "librarian"
    r, _ = run_tool(ctx, "read_knowledge", {"document": "fine_policy"})
    assert "Fines are set only by the librarian" in r["content"]


# ---------------------------------------------------------------- Phase 3

def test_simulated_week_sends_exactly_expected_messages(ctx, mailer):
    r, _ = run_tool(ctx, "save_borrowing_details", {"book_id": "B-101", "student": "S-001"})
    approvals.approve(ctx.conn, r["pending_action_id"], "M", today=FRIDAY)
    for i in range(1, 16):
        day = FRIDAY + timedelta(days=i)
        jobs.run_daily(ctx.conn, mailer, today=day)
        jobs.run_daily(ctx.conn, mailer, today=day)  # second run the same day sends nothing
    sent = [(m["To"], m["Subject"].split(":")[0]) for m in mailer.outbox]
    assert sent == [
        ("asha.rao@students.school.example", "Library reminder"),      # due 6 Nov
        ("library-committee@school.example", "RED ALERT"),             # 7 Nov
        ("asha.rao@students.school.example", "Library reminder"),      # overdue reminder
        ("office@school.example", "Library"),                          # 13 Nov, 7 days late
    ]


def test_privacy_redaction():
    assert redact("password: abc123") == "password: [REDACTED]"
    assert redact("My PIN is 4521") == "My PIN is [REDACTED]"
    assert redact("Is The Secret Garden in?") == "Is The Secret Garden in?"


# ---------------------------------------------------------------- Phase 4

def test_agent_loop_end_to_end_and_step_limit(conn):
    client = FakeClient([
        [tool_block("find_student_record", {"student": "S-001"}, "a")],
        [tool_block("save_borrowing_details", {"book_id": "B-102", "student": "S-001"}, "b")],
        [text_block("Action #1 is waiting for approval.")],
    ])
    agent = LibraryAgent(conn, client=client)
    sid = agent.new_session("Member One")
    reply = agent.chat(sid, "Member One", "Asha is borrowing B-102", today=FRIDAY)
    assert "waiting for approval" in reply
    assert approvals.list_pending(conn)[0]["session_id"] == sid
    req = client.requests[0]
    assert req["model"] == "claude-opus-5-5" and req["thinking"] == {"type": "adaptive"}

    looping = FakeClient([[tool_block("find_book", {"query": "x"}, f"id{i}")] for i in range(3)])
    agent = LibraryAgent(conn, client=looping, max_steps=3)
    reply = agent.chat(agent.new_session("M"), "M", "loop", today=FRIDAY)
    assert "stopped after 3 steps" in reply


def test_agent_resumes_after_approval(conn):
    client = FakeClient([
        [tool_block("save_borrowing_details", {"book_id": "B-102", "student": "S-001"})],
        [text_block("Waiting for approval as #1.")],
        [text_block("Approved: Asha has B-102 until 6 November.")],
    ])
    agent = LibraryAgent(conn, client=client)
    sid = agent.new_session("Member One")
    agent.chat(sid, "Member One", "Asha borrows B-102", today=FRIDAY)
    result = approvals.approve(conn, 1, "Member Two", today=FRIDAY)
    reply = agent.resume(sid, f"Action #1 was approved by Member Two. {result}", today=FRIDAY)
    assert "Approved" in reply
    assert "approved by Member Two" in client.requests[-1]["messages"][-1]["content"]


# ---------------------------------------------------------------- Phase 7

def test_backup_and_restore(tmp_path, conn):
    db_file = conn.execute("PRAGMA database_list").fetchone()["file"]
    path = backup.backup(db_file, str(tmp_path / "bk"))
    target = tmp_path / "restored.db"
    backup.restore(str(path), str(target))
    c = connect(str(target))
    assert register.totals(c)["total_books"] == 5
    c.close()
