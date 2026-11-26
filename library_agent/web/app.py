"""Web interface (PLAN Phase 5)."""
import csv
import hashlib
import hmac
import io
import json
import secrets
import smtplib
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote

from fastapi import BackgroundTasks, Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import activity, approvals, catalogue, librarian, passwords, register, roster, uploads
from ..agent import LibraryAgent
from ..config import settings
from ..db import connect, init_db
from ..notifications import Mailer, PayupNotAllowed
from ..tools import ToolContext, run_tool

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _redirect(path: str, message) -> RedirectResponse:
    return RedirectResponse(f"{path}?msg={quote(str(message))}", 303)


def member_list(path: str) -> list[dict]:
    """members.csv: name,email,role (role is committee or librarian).
    With AUTH_MODE=password each member's password hash is kept in the database
    (see passwords.py); other modes keep no passwords. Several members may share one email; they choose their name after signing in."""
    if settings.members_csv.strip():
        # A single-line setting may write line breaks as \n.
        text = settings.members_csv.replace("\\n", "\n").lstrip("﻿")
    else:
        p = Path(path)
        if not p.exists():
            return []
        text = p.read_text(encoding="utf-8-sig")
    return [{"name": r["name"].strip(), "email": r["email"].strip().lower(),
             "role": (r.get("role") or "committee").strip()}
            for r in csv.DictReader(io.StringIO(text)) if r.get("email") and (r.get("name") or "").strip()]


def load_members(path: str) -> dict[str, dict]:
    """The first member listed for each email."""
    members: dict[str, dict] = {}
    for m in member_list(path):
        members.setdefault(m["email"], m)
    return members


def members_for(path: str, email: str) -> list[dict]:
    return [m for m in member_list(path) if m["email"] == email.strip().lower()]


CHOOSE_WITHIN = 10 * 60  # seconds to pick a name after a shared email's code


class LoginCodes:
    """One-time sign-in codes, kept in memory only (a restart cancels them).
    Only a hash of each code is held."""
    LIFETIME = 10 * 60    # seconds a code stays valid
    RESEND_AFTER = 60     # seconds before another code can be sent
    MAX_TRIES = 5         # wrong guesses before the code is cancelled

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._codes: dict[str, dict] = {}

    @staticmethod
    def _hash(code: str) -> str:
        return hashlib.sha256(code.encode()).hexdigest()

    def issue(self, email: str) -> str | None:
        now = self.clock()
        old = self._codes.get(email)
        if old and now - old["issued"] < self.RESEND_AFTER:
            return None
        code = f"{secrets.randbelow(1_000_000):06d}"
        self._codes[email] = {"hash": self._hash(code), "issued": now, "tries": 0}
        return code

    def check(self, email: str, code: str) -> bool:
        entry = self._codes.get(email)
        if entry is None:
            return False
        if self.clock() - entry["issued"] > self.LIFETIME:
            self.discard(email)
            return False
        entry["tries"] += 1
        if hmac.compare_digest(entry["hash"], self._hash(code.strip().replace(" ", ""))):
            self.discard(email)
            return True
        if entry["tries"] >= self.MAX_TRIES:
            self.discard(email)
        return False

    def discard(self, email: str) -> None:
        self._codes.pop(email, None)


DESK_TOOLS = {"borrow": "save_borrowing_details", "return": "update_return_status",
              "renew": "renew_loan", "reserve": "reserve_book", "refer": "escalate_to_librarian"}
DESK_BOOK_LIMIT = 100  # enough to list the largest category (Stories) in full
STUDENT_LABELS = {"student_id": "Student ID", "name": "Full name", "grade": "Class/Grade",
                  "section": "Section", "phone": "Phone", "email": "Email", "photo_url": "Photo link"}


def create_app(db_path: str | None = None, agent_factory=None, mailer: Mailer | None = None,
               ai_enabled: bool | None = None) -> FastAPI:
    app = FastAPI(title="LibraMind")
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)
    # The school logo and other images.
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    db_path = db_path or settings.db_path
    if ai_enabled is None:
        ai_enabled = settings.ai_provider == "anthropic"
    TEMPLATES.env.globals["ai_enabled"] = ai_enabled
    TEMPLATES.env.globals["auth_mode"] = settings.auth_mode
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
        return TEMPLATES.TemplateResponse(request, name, {"user": user,
                                                          "shared_password": settings.shared_password, **ctx})

    # ------------------------------------------------------------ sign in

    codes = LoginCodes()

    attempts = passwords.Attempts(connect=lambda: connect(db_path))

    def login_page(request, **ctx):
        members = member_list(settings.members_file)
        if settings.auth_mode == "password":
            conn = connect(db_path)
            try:
                members = [{**m, "has_password": passwords.has_password(conn, m)} for m in members]
            finally:
                conn.close()
        return TEMPLATES.TemplateResponse(request, "login.html", {
            "user": None, "mode": settings.auth_mode, "can_send": mailer.configured,
            "members": members, "min_length": passwords.MIN_LENGTH,
            "shared_password": settings.shared_password, **ctx})

    def _wait_message(seconds: int) -> str:
        minutes = max(1, (seconds + 59) // 60)
        return (f"Too many wrong passwords. Please wait {minutes} minute{'s' if minutes > 1 else ''} "
                "and try again.")

    @app.post("/login/password", response_class=HTMLResponse)
    def login_password(request: Request, member: str = Form(""), password: str = Form("")):
        if settings.auth_mode != "password":
            raise HTTPException(404)
        everyone = member_list(settings.members_file)
        if not (member.isdigit() and int(member) < len(everyone)):
            return login_page(request, error="Choose your name from the list.")
        chosen, index = everyone[int(member)], int(member)
        key = passwords.key_for(chosen)
        wait = attempts.locked(key)
        if wait:
            return login_page(request, selected=index, error=_wait_message(wait))
        conn = connect(db_path)
        try:
            if not passwords.has_password(conn, chosen):
                # First sign-in: this member now creates their password.
                request.session["setup"] = {"index": index, "key": key,
                                            "until": time.time() + CHOOSE_WITHIN}
                return login_page(request, setup=chosen, suggestions=passwords.suggestions())
            ok = passwords.verify(conn, chosen, password)
        finally:
            conn.close()
        if not ok:
            attempts.failed(key)
            return login_page(request, selected=index, error="That password is wrong. Try again.")
        attempts.succeeded(key)
        request.session["user"] = chosen
        return RedirectResponse("/", 303)

    @app.post("/login/setup", response_class=HTMLResponse)
    def login_setup(request: Request, new_password: str = Form(""), confirm_password: str = Form("")):
        pending = request.session.get("setup")
        if settings.auth_mode != "password" or not pending or time.time() > pending["until"]:
            request.session.pop("setup", None)
            return RedirectResponse("/login", 303)
        everyone = member_list(settings.members_file)
        chosen = everyone[pending["index"]] if pending["index"] < len(everyone) else None
        if chosen is None or passwords.key_for(chosen) != pending["key"]:
            request.session.pop("setup", None)
            return RedirectResponse("/login", 303)
        error = passwords.problem(new_password, confirm_password)
        if error:
            return login_page(request, setup=chosen, suggestions=passwords.suggestions(), error=error)
        conn = connect(db_path)
        try:
            if passwords.has_password(conn, chosen):
                # Someone else finished setting it up first; sign in normally instead.
                request.session.pop("setup", None)
                return login_page(request, selected=pending["index"],
                                  error="A password has already been made. Sign in with it.")
            passwords.set_password(conn, chosen, new_password)
        finally:
            conn.close()
        request.session.pop("setup", None)
        request.session["user"] = chosen
        return _redirect("/desk", "The password is saved. Everyone now signs in with it."
                         if settings.shared_password else
                         "Your password is saved. Use it every time you sign in.")

    @app.get("/account/password", response_class=HTMLResponse)
    def change_password_form(request: Request, user=Depends(current_user)):
        if settings.auth_mode != "password":
            raise HTTPException(404)
        return page(request, "password.html", user, min_length=passwords.MIN_LENGTH,
                    suggestions=passwords.suggestions())

    @app.post("/account/password", response_class=HTMLResponse)
    def change_password(request: Request, current_password: str = Form(""),
                        new_password: str = Form(""), confirm_password: str = Form(""),
                        user=Depends(current_user), conn=Depends(get_conn)):
        if settings.auth_mode != "password":
            raise HTTPException(404)

        def again(error):
            return page(request, "password.html", user, error=error, min_length=passwords.MIN_LENGTH,
                        suggestions=passwords.suggestions())

        key = passwords.key_for(user)
        wait = attempts.locked(key)
        if wait:
            return again(_wait_message(wait))
        if not passwords.verify(conn, user, current_password):
            attempts.failed(key)
            return again("Your current password is wrong, so the password was not changed.")
        attempts.succeeded(key)
        error = passwords.problem(new_password, confirm_password)
        if error:
            return again(error)
        if new_password == current_password:
            return again("The new password is the same as the current one. Choose a different one.")
        passwords.set_password(conn, user, new_password)
        return _redirect("/desk", "The password has been changed for everyone. Tell the others the new one."
                         if settings.shared_password else
                         "Your password has been changed. Use the new one next time you sign in.")

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if settings.auth_mode == "header":
            return RedirectResponse("/", 303)
        return login_page(request)

    @app.post("/login/code", response_class=HTMLResponse)
    def login_send_code(request: Request, email: str = Form("")):
        if settings.auth_mode != "code":
            raise HTTPException(404)
        email = email.strip().lower()
        matches = members_for(settings.members_file, email)
        member = matches[0] if matches else None
        if not mailer.configured:
            return login_page(request, error="Sign-in emails are not set up on this computer yet. "
                                             "Ask the librarian.")
        if member is None:
            return login_page(request, email=email,
                              error="That email is not on the library committee list.")
        code = codes.issue(email)
        if code is None:
            return login_page(request, email=email, sent=True,
                              error="A code was sent less than a minute ago. Check your inbox.")
        try:
            mailer.send(email, "Library app sign-in code",
                        f"Hello {' / '.join(m['name'] for m in matches)},\n\nYour sign-in code for the library app is {code}.\n"
                        f"It works once, for {LoginCodes.LIFETIME // 60} minutes.\n\n"
                        "If you did not ask for it, you can ignore this email.\n\nSchool Library")
        except (OSError, smtplib.SMTPException):
            codes.discard(email)
            return login_page(request, email=email,
                              error="The email could not be sent. Try again, or ask the librarian.")
        return login_page(request, email=email, sent=True)

    @app.post("/login/verify", response_class=HTMLResponse)
    def login_verify(request: Request, email: str = Form(""), code: str = Form("")):
        if settings.auth_mode != "code":
            raise HTTPException(404)
        email = email.strip().lower()
        matches = members_for(settings.members_file, email)
        if not matches or not codes.check(email, code):
            return login_page(request, email=email, sent=True,
                              error="That code is wrong or has expired. Check it, or ask for a new one.")
        if len(matches) == 1:
            request.session["user"] = matches[0]
            return RedirectResponse("/", 303)
        # A shared email: the code proves the inbox, then the person says who they are.
        request.session["choose"] = {"email": email, "until": time.time() + CHOOSE_WITHIN}
        return login_page(request, choose=matches)

    @app.post("/login/choose")
    def login_choose(request: Request, name: str = Form("")):
        pending = request.session.pop("choose", None)
        if settings.auth_mode != "code" or not pending or time.time() > pending["until"]:
            return RedirectResponse("/login", 303)
        member = next((m for m in members_for(settings.members_file, pending["email"])
                       if m["name"] == name), None)
        if member is None:
            return RedirectResponse("/login", 303)
        request.session["user"] = member
        return RedirectResponse("/", 303)

    @app.post("/login")
    def login(request: Request, email: str = Form(""), member: str = Form("")):
        # Picking a name is for local testing only.
        if settings.auth_mode != "dev":
            raise HTTPException(404)
        everyone = member_list(settings.members_file)
        if member.isdigit() and int(member) < len(everyone):
            member = everyone[int(member)]
        else:
            member = load_members(settings.members_file).get(email.strip().lower())
        if member is None:
            raise HTTPException(403, "Not on the committee list.")
        request.session["user"] = member
        return RedirectResponse("/", 303)

    @app.get("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", 303)

    # ------------------------------------------------------------ scheduled job (Vercel cron)

    @app.get("/cron/daily-job")
    def cron_daily_job(request: Request, conn=Depends(get_conn)):
        """Vercel calls this once a day. It needs CRON_SECRET, so nobody else can run it."""
        sent = request.headers.get("authorization", "")
        if not settings.cron_secret or not hmac.compare_digest(sent, f"Bearer {settings.cron_secret}"):
            raise HTTPException(401, "Not allowed.")
        from .. import jobs
        return jobs.run_daily(conn, mailer)

    # ------------------------------------------------------------ desk (no AI needed)

    @app.get("/desk", response_class=HTMLResponse)
    def desk_page(request: Request, q: str = "", category: str = "",
                  user=Depends(current_user), conn=Depends(get_conn)):
        books = students = None
        if q.strip() or category:
            books = register.find_books(conn, q, limit=DESK_BOOK_LIMIT + 1, category=category)
        if q.strip():
            students = [{"student_id": s["student_id"], "name": s["name"], "grade": s["grade"]}
                        for s in register.find_students(conn, q)]
        more = books is not None and len(books) > DESK_BOOK_LIMIT
        return page(request, "desk.html", user, q=q, category=category,
                    categories=register.categories(conn),
                    books=books[:DESK_BOOK_LIMIT] if more else books, more_books=more,
                    students=students,
                    pending=len(approvals.list_pending(conn)), message=request.query_params.get("msg"))

    @app.post("/desk/{task}")
    def desk_entry(task: str, book_id: str = Form(""), student: str = Form(""),
                   condition: str = Form("good"), notes: str = Form(""),
                   user=Depends(current_user), conn=Depends(get_conn)):
        if task not in DESK_TOOLS:
            raise HTTPException(404)
        book_id = book_id.strip().upper()
        args = {"book_id": book_id}
        if task in ("borrow", "reserve"):
            args["student"] = student.strip()
        elif task == "return":
            args.update(condition=condition, notes=notes.strip())
        elif task == "refer":
            args = {"summary": notes.strip(), "book_id": book_id, "student_id": student.strip()}
            if not args["summary"]:
                return _redirect("/desk", "Write what the librarian needs to know.")
        missing = [] if task == "refer" else [k for k in ("book_id", "student") if k in args and not args[k]]
        if missing:
            return _redirect("/desk", f"Please fill in: {', '.join(m.replace('_', ' ') for m in missing)}.")
        ctx = ToolContext(conn, actor=user["name"], role=user["role"], via="desk")
        result, is_error = run_tool(ctx, DESK_TOOLS[task], args)
        if is_error:
            return _redirect("/desk", f"Not saved: {result['error']}")
        return _redirect("/desk", result["message"])

    # ------------------------------------------------------------ chat (5.1, only when AI is on)

    @app.get("/", response_class=HTMLResponse)
    def chat_page(request: Request, user=Depends(current_user), conn=Depends(get_conn)):
        if not ai_enabled:
            return RedirectResponse("/desk", 303)
        agent = agent_factory(conn)
        sid = request.session.get("chat_session")
        if not sid or not conn.execute("SELECT 1 FROM chat_sessions WHERE session_id = ?", (sid,)).fetchone():
            sid = agent.new_session(user["name"])
            request.session["chat_session"] = sid
        return page(request, "chat.html", user, transcript=agent.transcript(sid),
                    pending=len(approvals.list_pending(conn)))

    @app.post("/chat")
    def chat(request: Request, message: str = Form(...), user=Depends(current_user), conn=Depends(get_conn)):
        if not ai_enabled:
            raise HTTPException(404, "The AI chat is switched off. Use the Desk page.")
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
        if action["session_id"] and ai_enabled:
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
        if action["session_id"] and ai_enabled:
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
        return page(request, "librarian.html", user, fines=fines, drafts=drafts, max_fine=settings.max_fine,
                    message=request.query_params.get("msg"))

    def _do(fn, *args):
        try:
            result = fn(*args)
            return _redirect("/librarian", result if isinstance(result, str) else "Saved.")
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

    # ------------------------------------------------------------ add books (MINI SPEC 3.1, 3.4, 3.5)

    @app.get("/books/add", response_class=HTMLResponse)
    def books_add_page(request: Request, user=Depends(librarian_only), conn=Depends(get_conn)):
        return page(request, "books_add.html", user, categories=register.categories(conn),
                    max_rows=uploads.MAX_ROWS)

    async def _read_upload(file: UploadFile, synonyms: dict) -> dict:
        data = await file.read()
        if len(data) > 5 * 1024 * 1024:
            raise uploads.UploadError("The file is bigger than 5 MB. Split it into smaller files.")
        headers, rows = uploads.read_table(file.filename or "", data)
        return {"headers": headers, "rows": rows, "guess": uploads.guess_columns(headers, synonyms)}

    @app.post("/api/books/parse")
    async def books_parse(file: UploadFile = File(...), user=Depends(librarian_only)):
        try:
            return await _read_upload(file, catalogue.SYNONYMS)
        except uploads.UploadError as e:
            return JSONResponse({"error": str(e)}, 400)

    @app.post("/api/books/check")
    def books_check(payload: dict = Body(...), user=Depends(librarian_only), conn=Depends(get_conn)):
        try:
            return catalogue.public(catalogue.check(conn, list(payload.get("rows") or [])))
        except catalogue.CatalogueError as e:
            return JSONResponse({"error": str(e)}, 400)

    @app.post("/api/books/save")
    def books_save(payload: dict = Body(...), user=Depends(librarian_only), conn=Depends(get_conn)):
        try:
            return catalogue.save(conn, list(payload.get("rows") or []), user["name"])
        except catalogue.CatalogueError as e:
            return JSONResponse({"error": str(e)}, 400)

    # ------------------------------------------------------------ students (MINI SPEC 3.2, 3.3)

    @app.get("/students", response_class=HTMLResponse)
    def students_page(request: Request, q: str = "", show: str = "Active",
                      user=Depends(librarian_only), conn=Depends(get_conn)):
        show = show if show in (roster.ACTIVE, roster.INACTIVE, "All") else roster.ACTIVE
        return page(request, "students.html", user, q=q, show=show, students=roster.search(conn, q, show),
                    message=request.query_params.get("msg"), error=request.query_params.get("error"),
                    form=request.session.pop("student_form", None) or {})

    @app.post("/students/add")
    def students_add(request: Request, student_id: str = Form(""), name: str = Form(""),
                     grade: str = Form(""), section: str = Form(""), phone: str = Form(""),
                     email: str = Form(""), photo_url: str = Form(""),
                     user=Depends(librarian_only), conn=Depends(get_conn)):
        data = {"student_id": student_id, "name": name, "grade": grade, "section": section,
                "phone": phone, "email": email, "photo_url": photo_url}
        try:
            row = roster.add_student(conn, data, user["name"])
        except roster.RosterError as e:
            request.session["student_form"] = data  # keep what was typed
            return RedirectResponse(f"/students?error={quote(str(e))}#add", 303)
        return _redirect("/students", f"Added {row['name']} ({row['student_id']}).")

    @app.post("/students/upload", response_class=HTMLResponse)
    async def students_upload(request: Request, file: UploadFile = File(...), user=Depends(librarian_only)):
        try:
            parsed = await _read_upload(file, roster.SYNONYMS)
        except uploads.UploadError as e:
            return RedirectResponse(f"/students?error={quote(str(e))}#bulk", 303)
        return page(request, "students_map.html", user, parsed=parsed, fields=roster.FIELDS,
                    labels=STUDENT_LABELS, rows_json=json.dumps(parsed["rows"]), filename=file.filename)

    def _mapped_rows(form) -> list[dict]:
        raw = json.loads(form.get("rows") or "[]")
        cols = {f: str(form.get(f"col_{f}", "")) for f in roster.FIELDS}
        return [{f: (r[int(c)] if c.isdigit() and int(c) < len(r) else "") for f, c in cols.items()}
                for r in raw]

    @app.post("/students/upload/preview", response_class=HTMLResponse)
    async def students_preview(request: Request, user=Depends(librarian_only), conn=Depends(get_conn)):
        form = await request.form()
        try:
            rows = roster.check_bulk(conn, _mapped_rows(form))
        except (roster.RosterError, ValueError) as e:
            return RedirectResponse(f"/students?error={quote(str(e))}#bulk", 303)
        return page(request, "students_preview.html", user, rows=rows, labels=STUDENT_LABELS,
                    fields=roster.FIELDS,
                    payload=json.dumps([{f: r[f] for f in roster.FIELDS} for r in rows]),
                    new=sum(1 for r in rows if r["status"] == "new"))

    @app.post("/students/upload/save")
    def students_save(rows: str = Form("[]"), user=Depends(librarian_only), conn=Depends(get_conn)):
        try:
            result = roster.save_bulk(conn, json.loads(rows), user["name"])
        except (roster.RosterError, ValueError) as e:
            return RedirectResponse(f"/students?error={quote(str(e))}", 303)
        return _redirect("/students", result["message"])

    @app.get("/students/{student_id}/edit", response_class=HTMLResponse)
    def student_edit_page(request: Request, student_id: str, user=Depends(librarian_only),
                          conn=Depends(get_conn)):
        s = roster.get(conn, student_id)
        if s is None:
            raise HTTPException(404, f"No student {student_id}.")
        form = request.session.pop("student_edit", None) or {
            "name": s["name"], "grade": s["grade"], "section": s["section"], "phone": s["phone"],
            "email": s["contact_email"], "photo_url": s["photo_url"]}
        return page(request, "student_edit.html", user, s=s, form=form, error=request.query_params.get("error"))

    @app.post("/students/{student_id}/edit")
    def student_edit(request: Request, student_id: str, name: str = Form(""), grade: str = Form(""),
                     section: str = Form(""), phone: str = Form(""), email: str = Form(""),
                     photo_url: str = Form(""), user=Depends(librarian_only), conn=Depends(get_conn)):
        data = {"name": name, "grade": grade, "section": section, "phone": phone, "email": email,
                "photo_url": photo_url}
        try:
            result = roster.edit_student(conn, student_id, data, user["name"])
        except roster.RosterError as e:
            request.session["student_edit"] = data  # keep what was typed
            return RedirectResponse(f"/students/{quote(student_id)}/edit?error={quote(str(e))}", 303)
        if not result["changed"]:
            return _redirect("/students", f"No changes for {student_id.upper()}.")
        return _redirect("/students", f"Saved changes for {student_id.upper()} ({name.strip()}).")

    @app.get("/students/{student_id}/remove", response_class=HTMLResponse)
    def student_remove_page(request: Request, student_id: str, user=Depends(librarian_only),
                            conn=Depends(get_conn)):
        s = roster.get(conn, student_id)
        if s is None:
            raise HTTPException(404, f"No student {student_id}.")
        return page(request, "student_remove.html", user, s=s, blocked=roster.blockers(conn, s["student_id"]),
                    loans=register.open_loans_for_student(conn, s["student_id"]),
                    fines=register.unpaid_fines(conn, s["student_id"]))

    @app.post("/students/{student_id}/remove")
    def student_remove(student_id: str, user=Depends(librarian_only), conn=Depends(get_conn)):
        try:
            roster.remove(conn, student_id, user["name"])
        except roster.RosterError as e:
            return RedirectResponse(f"/students?error={quote(str(e))}", 303)
        return _redirect("/students", f"{student_id.upper()} removed. They are now inactive; "
                                      "their history is kept and they can be restored.")

    @app.post("/students/{student_id}/restore")
    def student_restore(student_id: str, user=Depends(librarian_only), conn=Depends(get_conn)):
        try:
            roster.restore(conn, student_id, user["name"])
        except roster.RosterError as e:
            return RedirectResponse(f"/students?show=Inactive&error={quote(str(e))}", 303)
        return _redirect("/students", f"{student_id.upper()} restored and can borrow again.")

    # ------------------------------------------------------------ activity log (MINI SPEC 4)

    @app.get("/activity", response_class=HTMLResponse)
    def activity_page(request: Request, action: str = "", user=Depends(librarian_only), conn=Depends(get_conn)):
        action = action if action in activity.ACTIONS else ""
        return page(request, "activity.html", user, rows=activity.recent(conn, action=action),
                    action=action, actions=activity.ACTIONS)

    app.state.mailer = mailer
    return app
