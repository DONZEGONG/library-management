"""Reports for the trial week, success measures and the monthly review
(PLAN 6.2, 6.3 and 7.4).

- shelf_check_rows: every book with its status and holder, to tick off
  against the shelf during a spot-check.
- period_report: entries made, rejections, corrections and approval wait
  times over a date range, plus the correction log entries to review.
"""
import csv
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

SHELF_CHECK_COLUMNS = ["book_id", "name", "copy_number", "status", "with_student",
                       "due_date", "on_shelf_yes_no", "notes"]


def shelf_check_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT b.book_id, b.name, b.copy_number, b.status, s.name AS student_name,"
        " s.grade, l.due_date FROM books b"
        " LEFT JOIN loans l ON l.book_id = b.book_id AND l.return_date IS NULL"
        " AND l.deleted_at IS NULL"
        " LEFT JOIN students s ON s.student_id = l.student_id"
        " WHERE b.deleted_at IS NULL ORDER BY b.name, b.copy_number"
    ).fetchall()
    return [
        {
            "book_id": r["book_id"],
            "name": r["name"],
            "copy_number": r["copy_number"],
            "status": r["status"],
            "with_student": f"{r['student_name']} (grade {r['grade']})" if r["student_name"] else "",
            "due_date": r["due_date"] or "",
            "on_shelf_yes_no": "",
            "notes": "",
        }
        for r in rows
    ]


def write_shelf_check(conn: sqlite3.Connection, path: str) -> int:
    rows = shelf_check_rows(conn)
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SHELF_CHECK_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def _range(start: date, end: date) -> tuple[str, str]:
    # Timestamps are stored as ISO strings, so string comparison works.
    return start.isoformat(), (end + timedelta(days=1)).isoformat()


def period_report(conn: sqlite3.Connection, start: date, end: date) -> dict:
    lo, hi = _range(start, end)

    by_tool = {}
    for r in conn.execute(
        "SELECT tool, status, COUNT(*) AS n FROM pending_actions"
        " WHERE created_at >= ? AND created_at < ? GROUP BY tool, status",
        (lo, hi),
    ):
        by_tool.setdefault(r["tool"], {})[r["status"]] = r["n"]

    waits = [
        (datetime.fromisoformat(r["decided_at"]) - datetime.fromisoformat(r["created_at"])).total_seconds() / 60
        for r in conn.execute(
            "SELECT created_at, decided_at FROM pending_actions WHERE decided_at IS NOT NULL"
            " AND created_at >= ? AND created_at < ?",
            (lo, hi),
        )
    ]

    rejections = [
        dict(r) for r in conn.execute(
            "SELECT action_id, tool, decided_by, decision_reason, decided_at FROM pending_actions"
            " WHERE status = 'rejected' AND decided_at >= ? AND decided_at < ? ORDER BY decided_at",
            (lo, hi),
        )
    ]
    corrections = [
        dict(r) for r in conn.execute(
            "SELECT original_decision, human_correction, reason, corrected_by, date"
            " FROM correction_log WHERE date >= ? AND date < ? ORDER BY date",
            (lo, hi),
        )
    ]

    decided = sum(v.get("approved", 0) + v.get("rejected", 0) for v in by_tool.values())
    rejected = sum(v.get("rejected", 0) for v in by_tool.values())
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "proposals_by_tool": by_tool,
        "proposals_decided": decided,
        "proposals_rejected": rejected,
        "rejection_rate": round(rejected / decided, 3) if decided else None,
        "corrections": len(corrections),
        "median_approval_wait_minutes": round(sorted(waits)[len(waits) // 2], 1) if waits else None,
        "rejection_details": rejections,
        "correction_details": corrections,
    }


def format_report(r: dict) -> str:
    lines = [f"Library agent report {r['from']} to {r['to']}", ""]
    lines.append("Proposals by tool (approved / rejected / pending):")
    if not r["proposals_by_tool"]:
        lines.append("  none")
    for tool, counts in sorted(r["proposals_by_tool"].items()):
        lines.append(f"  {tool}: {counts.get('approved', 0)} / {counts.get('rejected', 0)}"
                     f" / {counts.get('pending', 0)}")
    rate = "n/a" if r["rejection_rate"] is None else f"{r['rejection_rate']:.0%}"
    wait = "n/a" if r["median_approval_wait_minutes"] is None else f"{r['median_approval_wait_minutes']} min"
    lines += [
        "",
        f"Rejected by a human: {r['proposals_rejected']} of {r['proposals_decided']} ({rate})",
        f"Librarian corrections: {r['corrections']}",
        f"Median wait for approval: {wait}",
    ]
    if r["rejection_details"]:
        lines += ["", "Rejections:"]
        lines += [f"  #{x['action_id']} {x['tool']} by {x['decided_by']}: {x['decision_reason']}"
                  for x in r["rejection_details"]]
    if r["correction_details"]:
        lines += ["", "Corrections to review:"]
        lines += [f"  {x['date'][:10]} {x['corrected_by']}: {x['original_decision']} -> "
                  f"{x['human_correction']} ({x['reason']})" for x in r["correction_details"]]
    return "\n".join(lines)
