"""Web interface (PLAN Phase 5)."""
import csv
import json
from datetime import date
from pathlib import Path
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import approvals, librarian, register
from ..agent import LibraryAgent
from ..config import settings
from ..db import connect, init_db
from ..notifications import Mailer, PayupNotAllowed

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _redirect(path: str, message) -> RedirectResponse:
    return RedirectResponse(f"{path}?msg={quote(str(message))}", 303)


def load_members(path: str) -> dict[str, dict]:
    """members.csv: name,email,role (role is committee or librarian).
    The app keeps no passwords; sign-in is done by the school's accounts."""
    p = Path(path)
    if not p.exists():
        return {}
    with p.open(newline="", encoding="utf-8-sig") as f:
        return {r["email"].strip().lower(): {"name": r["name"].strip(), "email": r["email"].strip().lower(),
                                             "role": (r.get("role") or "committee").strip()}
                for r in csv.DictReader(f) if r.get("email")}


def create_app(db_path: str | None = None, agent_factory=None, mailer: Mailer | None = None) -> FastAPI:
    app = FastAPI(title="Library Management Agent")
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)
    db_path = db_path or settings.db_path
    mailer = mailer or Mailer()
    agent_factory = agent_factory or (lambda conn: LibraryAgent(conn))

    boot = connect(db_path)
    init_db(boot)
    boot.close()

    def get_conn():
        conn = connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def current_user(request: Request) -> dict:
        members = load_members(settings.members_file)
        if settings.auth_mode == "header":
            email = (request.headers.get(settings.auth_header) or "").strip().lower()
            user = members.get(email)
            if user is None:
                raise HTTPException(403, "You are not on the library committee list.")
            return user
        user = request.session.get("user")
        if user is None:
            raise HTTPException(303, headers={"Location": "/login"})
        return user

    def librarian_only(user: dict = Depends(current_user)) -> dict:
        if user["role"] != "librarian":
            raise HTTPException(403, "Only the librarian can use this page.")
        return user

    def page(request, name, user, **ctx):
        return TEMPLATES.TemplateResponse(request, name, {"user": user, **ctx})

    # ------------------------------------------------------------ sign in (dev mode)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if settings.auth_mode == "header":
            return RedirectResponse("/", 303)
        return TEMPLATES.TemplateResponse(request, "login.html",
                                          {"user": None, "members": load_members(settings.members_file).values()})

    @app.post("/login")
    def login(request: Request, email: str = Form(...)):
        member = load_members(settings.members_file).get(email.strip().lower())
        if member is None:
            raise HTTPException(403, "Not on the committee list.")
        request.session["user"] = member
        return RedirectResponse("/", 303)

    @app.get("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", 303)

    # ------------------------------------------------------------ chat (5.1)

    @app.get("/", response_class=HTMLResponse)
    def chat_page(request: Request, user=Depends(current_user), conn=Depends(get_conn)):
        agent = agent_factory(conn)
        sid = request.session.get("chat_session")
        if not sid or not conn.execute("SELECT 1 FROM chat_sessions WHERE session_id = ?", (sid,)).fetchone():
            sid = agent.new_session(user["name"])
            request.session["chat_session"] = sid
        return page(request, "chat.html", user, transcript=agent.transcript(sid),
                    pending=len(approvals.list_pending(conn)))

    @app.post("/chat")
    def chat(request: Request, message: str = Form(...), user=Depends(current_user), conn=Depends(get_conn)):
        sid = request.session.get("chat_session")
        if message.strip() and sid:
            agent = agent_factory(conn)
            try:
                agent.chat(sid, user["name"], message, role=user["role"])
            except Exception as e:  # show API problems instead of a blank error page
                raise HTTPException(502, f"The AI agent could not be reached: {e}")
        return RedirectResponse("/", 303)

    @app.post("/chat/new")
    def new_chat(request: Request, user=Depends(current_user)):
        request.session.pop("chat_session", None)
        return RedirectResponse("/", 303)

    # ------------------------------------------------------------ approvals (5.2, 5.3)

    @app.get("/approvals", response_class=HTMLResponse)
    def approvals_page(request: Request, user=Depends(current_user), conn=Depends(get_conn)):
        return page(request, "approvals.html", user, actions=approvals.list_pending(conn),
                    message=request.query_params.get("msg"))

    def _resume(session_id, event):
        conn = connect(db_path)
        try:
            agent_factory(conn).resume(session_id, event)
        except Exception:
            pass  # the decision is already saved; resuming the chat is a courtesy
        finally:
            conn.close()

    @app.post("/approvals/{action_id}/approve")
    def approve(action_id: int, background: BackgroundTasks, user=Depends(current_user), conn=Depends(get_conn)):
        action = approvals.get_action(conn, action_id)
        if action["tool"] == "escalate_to_librarian" and user["role"] != "librarian":
            raise HTTPException(403, "Only the librarian can close a referral.")
        try:
            result = approvals.approve(conn, action_id, user["name"])
        except approvals.ApprovalError as e:
            return _redirect("/approvals", e)
        if action["session_id"]:
            background.add_task(_resume, action["session_id"],
                                f"Action #{action_id} ({action['tool']}) was approved by {user['name']}. {result}")
        return _redirect("/approvals", result)

    @app.post("/approvals/{action_id}/reject")
    def reject(action_id: int, background: BackgroundTasks, reason: str = Form(""),
               user=Depends(current_user), conn=Depends(get_conn)):
        try:
            approvals.reject(conn, action_id, user["name"], reason)
        except approvals.ApprovalError as e:
            return _redirect("/approvals", e)
        action = approvals.get_action(conn, action_id)
        if action["session_id"]:
            background.add_task(_resume, action["session_id"],
                                f"Action #{action_id} ({action['tool']}) was rejected by {user['name']}. "
                                f"Reason: {reason}. Nothing was saved.")
        return _redirect("/approvals", f"Action #{action_id} rejected.")

    # ------------------------------------------------------------ register (5.4)

    @app.get("/register", response_class=HTMLResponse)
    def register_page(request: Request, user=Depends(current_user), conn=Depends(get_conn)):
        books = conn.execute(
            "SELECT b.*, l.due_date, s.name AS student_name, s.grade FROM books b"
            " LEFT JOIN loans l ON l.book_id = b.book_id AND l.return_date IS NULL AND l.deleted_at IS NULL"
            " LEFT JOIN students s ON s.student_id = l.student_id"
            " WHERE b.deleted_at IS NULL ORDER BY b.name, b.copy_number"
        ).fetchall()
        return page(request, "register.html", user, totals=register.totals(conn), books=books,
                    overdue=register.overdue_books(conn))

    # ------------------------------------------------------------ librarian tools (5.5)

    @app.get("/librarian", response_class=HTMLResponse)
    def librarian_page(request: Request, user=Depends(librarian_only), conn=Depends(get_conn)):
        fines = conn.execute(
            "SELECT f.*, b.name AS book_name, s.name AS student_name FROM fines f"
            " JOIN loans l ON l.loan_id = f.loan_id JOIN books b ON b.book_id = l.book_id"
            " JOIN students s ON s.student_id = l.student_id ORDER BY f.fine_id DESC LIMIT 50"
        ).fetchall()
        drafts = conn.execute(
            "SELECT n.*, b.name AS book_name FROM notifications n JOIN loans l ON l.loan_id = n.loan_id"
            " JOIN books b ON b.book_id = l.book_id WHERE n.kind = 'payup' AND n.status = 'draft'"
        ).fetchall()
        return page(request, "librarian.html", user, fines=fines, drafts=drafts,
                    message=request.query_params.get("msg"))

    def _do(fn, *args):
        try:
            fn(*args)
            return _redirect("/librarian", "Saved.")
        except (librarian.LibrarianError, PayupNotAllowed, ValueError, LookupError) as e:
            return _redirect("/librarian", e)

    @app.post("/librarian/fine")
    def set_fine(loan_id: int = Form(...), amount: float = Form(...), reason: str = Form(""),
                 user=Depends(librarian_only), conn=Depends(get_conn)):
        return _do(librarian.set_fine, conn, loan_id, amount, reason, user["name"], mailer)

    @app.post("/librarian/fine/{fine_id}")
    def update_fine(fine_id: int, status: str = Form(...), reason: str = Form(""),
                    new_amount: str = Form(""), user=Depends(librarian_only), conn=Depends(get_conn)):
        return _do(librarian.update_fine, conn, fine_id, status, reason, user["name"],
                   float(new_amount) if new_amount else None)

    @app.post("/librarian/due-date")
    def change_due(loan_id: int = Form(...), new_due: str = Form(...), reason: str = Form(""),
                   user=Depends(librarian_only), conn=Depends(get_conn)):
        return _do(librarian.change_due_date, conn, loan_id, date.fromisoformat(new_due), reason, user["name"])

    @app.post("/librarian/lost")
    def mark_lost(book_id: str = Form(...), reason: str = Form(""), user=Depends(librarian_only),
                  conn=Depends(get_conn)):
        return _do(librarian.mark_lost, conn, book_id, reason, user["name"])

    @app.post("/librarian/resolve")
    def resolve(book_id: str = Form(...), outcome: str = Form(...), reason: str = Form(""),
                user=Depends(librarian_only), conn=Depends(get_conn)):
        return _do(librarian.resolve_book, conn, book_id, outcome, reason, user["name"])

    @app.post("/librarian/correct")
    def correct(book_id: str = Form(...), new_status: str = Form(...), reason: str = Form(""),
                user=Depends(librarian_only), conn=Depends(get_conn)):
        return _do(librarian.correct_state, conn, book_id, new_status, reason, user["name"])

    @app.post("/librarian/delete")
    def delete(table: str = Form(...), record_id: str = Form(...), reason: str = Form(""),
               user=Depends(librarian_only), conn=Depends(get_conn)):
        return _do(librarian.delete_record, conn, table, record_id, reason, user["name"])

    # ------------------------------------------------------------ correction log (5.6)

    @app.get("/corrections", response_class=HTMLResponse)
    def corrections(request: Request, user=Depends(current_user), conn=Depends(get_conn)):
        rows = conn.execute("SELECT * FROM correction_log ORDER BY correction_id DESC").fetchall()
        return page(request, "corrections.html", user, rows=rows)

    app.state.mailer = mailer
    return app
