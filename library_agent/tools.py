"""The agent's tools (SPEC Layer 6, PLAN Phase 2).

Read tools answer directly. Write tools never change loans, returns or
damage records themselves: they create a pending action that a committee
member approves or rejects (see approvals.py). Reserving is the one write the
SPEC lets the agent do alone (Available -> Reserved).

Human-only actions (fines, due-date changes, mark lost, delete, state
corrections, approving actions) live in librarian.py and approvals.py and
are deliberately NOT in TOOLS.
"""
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import register, states
from .config import settings
from .dates import due_date_for
from .db import now
from .privacy import redact_obj


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    actor: str                       # signed-in committee member using the agent
    role: str = "committee"          # "committee" or "librarian"
    session_id: str | None = None
    today: date = field(default_factory=date.today)


class ToolError(Exception):
    """Returned to the agent as an error result, never a crash."""


# ---------------------------------------------------------------- helpers

def _resolve_student(conn, student: str):
    row = register.get_student(conn, student)
    if row:
        return row
    matches = register.find_students(conn, student)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ToolError(f"No student found for '{student}'. Ask the user for the student ID.")
    names = "; ".join(f"{m['student_id']}: {m['name']} (grade {m['grade']})" for m in matches)
    raise ToolError(f"More than one student matches '{student}': {names}. Ask the user which one.")


def _resolve_book(conn, book_id: str):
    row = register.get_book(conn, book_id)
    if row is None:
        raise ToolError(f"No book with ID '{book_id}'. Use find_book to look up the ID.")
    return row


def _pending_for_book(conn, book_id: str):
    for row in conn.execute("SELECT * FROM pending_actions WHERE status = 'pending'"):
        if json.loads(row["details"]).get("book_id") == book_id:
            return row
    return None


def _ensure_no_pending(conn, book_id: str):
    p = _pending_for_book(conn, book_id)
    if p:
        raise ToolError(f"Book {book_id} already has action #{p['action_id']} ({p['tool']}) "
                        "waiting for a committee decision. Nothing new can be proposed until it is decided.")


def propose(ctx: ToolContext, tool: str, details: dict, reason: str, flags: list[str] | None = None) -> dict:
    cur = ctx.conn.execute(
        "INSERT INTO pending_actions (tool, details, suggested_by, reason, flags, session_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tool, json.dumps(details), f"AI agent (for {ctx.actor})", reason,
         json.dumps(flags or []), ctx.session_id, now()),
    )
    ctx.conn.commit()
    return {
        "pending_action_id": cur.lastrowid,
        "status": "waiting_for_approval",
        "flags_for_human": flags or [],
        "message": f"Action #{cur.lastrowid} is waiting for a committee member to approve it. "
                   "Nothing has been saved yet.",
    }


# ---------------------------------------------------------------- read tools

def find_book(ctx: ToolContext, query: str) -> dict:
    books = register.find_books(ctx.conn, query)
    return {"matches": books, "count": len(books)}


def check_availability(ctx: ToolContext, book_id: str) -> dict:
    info = register.availability(ctx.conn, book_id)
    if info is None:
        raise ToolError(f"No book with ID '{book_id}'. Use find_book to look up the ID.")
    pending = _pending_for_book(ctx.conn, book_id)
    if pending:
        info["pending_action"] = f"#{pending['action_id']} ({pending['tool']}) waiting for approval"
    return info


def find_student_record(ctx: ToolContext, student: str) -> dict:
    s = _resolve_student(ctx.conn, student)
    loans = register.open_loans_for_student(ctx.conn, s["student_id"])
    fines = register.unpaid_fines(ctx.conn, s["student_id"])
    # Only what the task needs: no contact details (PLAN 2.3).
    return {
        "student_id": s["student_id"],
        "name": s["name"],
        "grade": s["grade"],
        "current_loans": [
            {"loan_id": l["loan_id"], "book_id": l["book_id"], "book_name": l["book_name"],
             "due_date": l["due_date"], "status": l["book_status"]}
            for l in loans
        ],
        "unpaid_fines": {"count": len(fines), "total": round(sum(f["amount"] for f in fines), 2)},
        "flags": register.borrowing_flags(ctx.conn, s["student_id"]),
    }


def read_knowledge(ctx: ToolContext, document: str = "") -> dict:
    from .knowledge import list_documents, read_document

    if not document:
        return {"documents": list_documents(ctx.role)}
    return {"document": document, "content": read_document(document, ctx.role)}


def search_past_decisions(ctx: ToolContext, kind: str = "", student_id: str = "", book_id: str = "") -> dict:
    sql, params = "SELECT kind, book_id, student_id, summary, decided_by, decided_at FROM human_decisions WHERE 1=1", []
    for col, val in (("kind", kind), ("student_id", student_id), ("book_id", book_id)):
        if val:
            sql += f" AND {col} = ?"
            params.append(val)
    rows = ctx.conn.execute(sql + " ORDER BY decided_at DESC LIMIT 10", params).fetchall()
    return {
        "decisions": [dict(r) for r in rows],
        "note": "Past decisions are guidance only. Always re-check the current status, "
                "reservations and due date before suggesting anything.",
    }


# ---------------------------------------------------------------- write tools (pending actions)

def save_borrowing_details(ctx: ToolContext, book_id: str, student: str) -> dict:
    book = _resolve_book(ctx.conn, book_id)
    s = _resolve_student(ctx.conn, student)
    _ensure_no_pending(ctx.conn, book_id)
    if book["status"] == states.RESERVED:
        res = register.active_reservation(ctx.conn, book_id)
        if res and res["student_id"] != s["student_id"]:
            raise ToolError(f"Book {book_id} is reserved for a different student until "
                            f"{res['expires_on']}. It cannot be lent to {s['name']}.")
    elif book["status"] != states.AVAILABLE:
        raise ToolError(f"Book {book_id} is {book['status']}, so it cannot be borrowed right now.")

    take = ctx.today
    due = due_date_for(take, settings.loan_period_days)
    flags = register.borrowing_flags(ctx.conn, s["student_id"])
    details = {"book_id": book_id, "book_name": book["name"], "student_id": s["student_id"],
               "student_name": s["name"], "grade": s["grade"],
               "take_date": take.isoformat(), "due_date": due.isoformat()}
    reason = f"{s['name']} (grade {s['grade']}) wants to borrow \"{book['name']}\"."
    if flags:
        reason += " Needs a human look because of the flags below."
    return propose(ctx, "save_borrowing_details", details, reason, flags)


def update_return_status(ctx: ToolContext, book_id: str, condition: str = "good", notes: str = "") -> dict:
    if condition not in ("good", "damaged"):
        raise ToolError("condition must be 'good' or 'damaged'.")
    if condition == "damaged":
        return record_book_damage(ctx, book_id, notes or "Damage reported at return.")
    book = _resolve_book(ctx.conn, book_id)
    loan = register.open_loan_for_book(ctx.conn, book_id)
    if loan is None or book["status"] not in (states.BORROWED, states.OVERDUE):
        raise ToolError(f"Book {book_id} is {book['status']} and has no open loan to return.")
    _ensure_no_pending(ctx.conn, book_id)
    s = register.get_student(ctx.conn, loan["student_id"])
    details = {"book_id": book_id, "book_name": book["name"], "loan_id": loan["loan_id"],
               "student_id": s["student_id"], "student_name": s["name"], "grade": s["grade"],
               "take_date": loan["take_date"], "due_date": loan["due_date"],
               "return_date": ctx.today.isoformat(), "condition": "good", "notes": notes}
    late = ctx.today.isoformat() > loan["due_date"]
    reason = f"\"{book['name']}\" was brought back in good condition" + (" after its due date." if late else ".")
    return propose(ctx, "update_return_status", details, reason)


def record_book_damage(ctx: ToolContext, book_id: str, notes: str) -> dict:
    """High risk (raised from medium, open decision 4). The book stays out of
    circulation: no other action can be proposed until a human decides."""
    book = _resolve_book(ctx.conn, book_id)
    if not notes.strip():
        raise ToolError("Describe the damage in notes.")
    loan = register.open_loan_for_book(ctx.conn, book_id)
    if loan is None or book["status"] not in (states.BORROWED, states.OVERDUE):
        raise ToolError(f"Book {book_id} is {book['status']}. Damage found on the shelf must be "
                        "recorded by the librarian. Use escalate_to_librarian.")
    _ensure_no_pending(ctx.conn, book_id)
    s = register.get_student(ctx.conn, loan["student_id"])
    details = {"book_id": book_id, "book_name": book["name"], "loan_id": loan["loan_id"],
               "student_id": s["student_id"], "student_name": s["name"], "grade": s["grade"],
               "take_date": loan["take_date"], "due_date": loan["due_date"],
               "return_date": ctx.today.isoformat(), "condition": "damaged", "notes": notes}
    reason = (f"\"{book['name']}\" came back with damage: {notes}. A human must assess it; "
              "the librarian sets any charge.")
    return propose(ctx, "record_book_damage", details, reason)


def renew_loan(ctx: ToolContext, book_id: str) -> dict:
    book = _resolve_book(ctx.conn, book_id)
    loan = register.open_loan_for_book(ctx.conn, book_id)
    if loan is None:
        raise ToolError(f"Book {book_id} has no open loan.")
    if book["status"] != states.BORROWED:
        raise ToolError(f"Book {book_id} is {book['status']}. Only a borrowed book that is not "
                        "overdue can be renewed; refer this to the librarian.")
    _ensure_no_pending(ctx.conn, book_id)
    if loan["renewals"] >= settings.max_renewals:
        raise ToolError(f"This loan has already been renewed {loan['renewals']} time(s); "
                        f"the limit is {settings.max_renewals}. Refer it to the librarian.")
    res = register.active_reservation(ctx.conn, book_id)
    if res and res["student_id"] != loan["student_id"]:
        raise ToolError("Another student has reserved this book, so it cannot be renewed.")
    s = register.get_student(ctx.conn, loan["student_id"])
    new_due = (date.fromisoformat(loan["due_date"]) + timedelta(days=settings.loan_period_days)).isoformat()
    details = {"book_id": book_id, "book_name": book["name"], "loan_id": loan["loan_id"],
               "student_id": s["student_id"], "student_name": s["name"], "grade": s["grade"],
               "take_date": loan["take_date"], "due_date": loan["due_date"], "new_due_date": new_due}
    reason = f"Renew \"{book['name']}\" for {s['name']}: due date {loan['due_date']} -> {new_due}."
    return propose(ctx, "renew_loan", details, reason)


def escalate_to_librarian(ctx: ToolContext, summary: str, book_id: str = "", student_id: str = "") -> dict:
    details = {"summary": summary}
    if book_id:
        details["book_id"] = book_id
    if student_id:
        details["student_id"] = student_id
    result = propose(ctx, "escalate_to_librarian", details, summary)
    result["message"] = f"Referred to the librarian as item #{result['pending_action_id']}."
    return result


# ---------------------------------------------------------------- agent-allowed direct write

def reserve_book(ctx: ToolContext, book_id: str, student: str) -> dict:
    book = _resolve_book(ctx.conn, book_id)
    s = _resolve_student(ctx.conn, student)
    if ctx.conn.execute(
        "SELECT 1 FROM reservations WHERE book_id = ? AND student_id = ? AND active = 1",
        (book_id, s["student_id"]),
    ).fetchone():
        raise ToolError(f"{s['name']} already has an active reservation for this book.")
    if book["status"] in (states.LOST, states.DAMAGED):
        raise ToolError(f"Book {book_id} is {book['status']} and cannot be reserved.")
    if book["status"] == states.AVAILABLE and not _pending_for_book(ctx.conn, book_id):
        expires = (ctx.today + timedelta(days=settings.reservation_hold_days)).isoformat()
        states.set_status(ctx.conn, book_id, states.RESERVED)
        msg = f"Reserved for {s['name']} until {expires}."
    else:
        expires = None  # queued: the hold starts when the book comes back
        msg = (f"Book is {book['status']}. {s['name']} has been added to the reservation queue; "
               "the hold starts when it is returned.")
    ctx.conn.execute(
        "INSERT INTO reservations (book_id, student_id, reserved_on, expires_on) VALUES (?, ?, ?, ?)",
        (book_id, s["student_id"], ctx.today.isoformat(), expires),
    )
    ctx.conn.commit()
    return {"status": "reserved", "expires_on": expires, "message": msg}


# ---------------------------------------------------------------- registry

def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


_S = {"type": "string"}

TOOLS = {
    "find_book": (find_book, "low", "Search the catalogue by book name, author or book ID.",
                  _schema({"query": _S}, ["query"])),
    "check_availability": (check_availability, "low",
                           "Status of one book copy: in the library or with whom, and the due date.",
                           _schema({"book_id": _S}, ["book_id"])),
    "find_student_record": (find_student_record, "medium",
                            "A student's name, grade, current loans, unpaid fines and any flags. "
                            "Accepts a student ID or name.",
                            _schema({"student": _S}, ["student"])),
    "read_knowledge": (read_knowledge, "low",
                       "Read library rules and policies. Call with no document to list them.",
                       _schema({"document": _S}, [])),
    "search_past_decisions": (search_past_decisions, "low",
                              "Past human decisions (special approvals, renewals, fines waived, lost "
                              "confirmations, damage assessments). Guidance only.",
                              _schema({"kind": _S, "student_id": _S, "book_id": _S}, [])),
    "save_borrowing_details": (save_borrowing_details, "high",
                               "Propose a new loan. Creates a pending action for a committee member "
                               "to approve; the due date is set by the library rule.",
                               _schema({"book_id": _S, "student": _S}, ["book_id", "student"])),
    "update_return_status": (update_return_status, "high",
                             "Propose checking a book back in. Creates a pending action.",
                             _schema({"book_id": _S,
                                      "condition": {"type": "string", "enum": ["good", "damaged"]},
                                      "notes": _S}, ["book_id", "condition"])),
    "record_book_damage": (record_book_damage, "high",
                           "Propose recording damage on a book being returned. Creates a pending "
                           "action; the book stays out of circulation until a human decides.",
                           _schema({"book_id": _S, "notes": _S}, ["book_id", "notes"])),
    "renew_loan": (renew_loan, "high",
                   "Propose renewing a loan by one loan period. Creates a pending action.",
                   _schema({"book_id": _S}, ["book_id"])),
    "reserve_book": (reserve_book, "medium",
                     "Reserve a book for a student, or join the queue if it is out.",
                     _schema({"book_id": _S, "student": _S}, ["book_id", "student"])),
    "escalate_to_librarian": (escalate_to_librarian, "low",
                              "Refer something to the librarian: fines, due-date changes, lost "
                              "books, corrections, deletions, or anything you are unsure about.",
                              _schema({"summary": _S, "book_id": _S, "student_id": _S}, ["summary"])),
}

# Actions the agent must never have (PLAN 2.9-2.14). Used by tests.
HUMAN_ONLY = {"set_fine", "waive_fine", "reduce_fine", "change_due_date", "correct_state",
              "mark_lost", "delete_record", "approve_action", "reject_action"}


def tool_definitions() -> list[dict]:
    return [
        {"name": name, "description": f"[{risk} risk] {desc}", "input_schema": schema}
        for name, (_, risk, desc, schema) in TOOLS.items()
    ]


def run_tool(ctx: ToolContext, name: str, arguments: dict) -> tuple[dict, bool]:
    """Run a tool and audit the call. Returns (result, is_error)."""
    arguments = redact_obj(arguments or {})
    states.audit_tool_call(ctx.conn, name, arguments, ctx.actor)
    ctx.conn.commit()
    if name not in TOOLS:
        return {"error": f"Unknown tool {name}."}, True
    fn = TOOLS[name][0]
    try:
        return fn(ctx, **arguments), False
    except (ToolError, states.TransitionError, LookupError, TypeError) as e:
        ctx.conn.rollback()
        return {"error": str(e)}, True
