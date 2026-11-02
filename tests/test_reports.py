"""PLAN 6.2, 6.3 and 7.4: shelf-check export and the period report."""
import csv
from datetime import date, timedelta

from conftest import FRIDAY
from library_agent import approvals, librarian, reports
from library_agent.__main__ import main
from library_agent.tools import run_tool

TODAY = date.today()


def _borrow(ctx, conn, book_id, student):
    r, _ = run_tool(ctx, "save_borrowing_details", {"book_id": book_id, "student": student})
    return r["pending_action_id"]


def test_shelf_check_lists_every_book_with_holder(ctx, conn, tmp_path):
    approvals.approve(conn, _borrow(ctx, conn, "B-101", "S-001"), "M", today=FRIDAY)
    out = tmp_path / "shelf.csv"
    assert reports.write_shelf_check(conn, str(out)) == 5
    rows = {r["book_id"]: r for r in csv.DictReader(out.open(encoding="utf-8"))}
    assert rows["B-101"]["status"] == "Borrowed"
    assert rows["B-101"]["with_student"] and rows["B-101"]["due_date"]
    assert rows["B-102"]["with_student"] == ""
    assert list(rows["B-101"]) == reports.SHELF_CHECK_COLUMNS


def test_period_report_counts_rejections_and_corrections(ctx, conn):
    approvals.approve(conn, _borrow(ctx, conn, "B-101", "S-001"), "M", today=FRIDAY)
    approvals.reject(conn, _borrow(ctx, conn, "B-102", "S-002"), "M", "Wrong student")
    librarian.correct_state(conn, "B-201", "Damaged", "Found torn on the shelf", "Librarian")

    r = reports.period_report(conn, TODAY - timedelta(days=1), TODAY)
    assert r["proposals_by_tool"]["save_borrowing_details"] == {"approved": 1, "rejected": 1}
    assert (r["proposals_decided"], r["proposals_rejected"], r["rejection_rate"]) == (2, 1, 0.5)
    assert r["corrections"] == 2  # the rejection and the librarian override
    assert r["median_approval_wait_minutes"] is not None
    text = reports.format_report(r)
    assert "Wrong student" in text and "Found torn on the shelf" in text


def test_period_report_excludes_other_dates(ctx, conn):
    approvals.reject(conn, _borrow(ctx, conn, "B-102", "S-002"), "M", "Wrong student")
    r = reports.period_report(conn, TODAY - timedelta(days=40), TODAY - timedelta(days=10))
    assert r["proposals_decided"] == 0 and r["corrections"] == 0
    assert "none" in reports.format_report(r)


def test_cli_report_and_shelf_check(conn, tmp_path, monkeypatch, capsys):
    from library_agent import config
    db = conn.execute("PRAGMA database_list").fetchone()["file"]
    monkeypatch.setattr(config.settings, "db_path", db)
    main(["report"])
    assert "Library agent report" in capsys.readouterr().out
    main(["shelf-check", str(tmp_path / "s.csv")])
    assert "Wrote 5 book(s)" in capsys.readouterr().out
