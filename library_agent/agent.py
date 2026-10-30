"""The AI agent (PLAN Phase 4): a Claude tool-use loop over the tools in
tools.py. Conversations are stored per chat session so a task can pause on a
pending action and continue once a human decides (PLAN 5.3)."""
import json
import sqlite3
import uuid
from datetime import date

from .config import settings
from .db import now
from .privacy import redact
from .tools import ToolContext, run_tool, tool_definitions

SYSTEM_PROMPT = """You are an AI library management agent for a school library. Library committee \
members talk to you by typing or by dictating. Manage book and borrowing records accurately. You can \
check availability, look up students, propose borrowings, returns, damage reports and renewals, \
reserve books, and refer matters to the librarian. Standard reminders are sent automatically by a \
daily job, not by you.

How changes work:
- Borrowings, returns, damage reports and renewals are never saved by you. The tool creates a pending \
action and a committee member approves or rejects it. Every time you create one, say clearly that it \
is waiting for approval and give its number. Never say it has been saved.
- Before proposing a loan, look at the student's record. If there are flags (loan limit, unpaid fines, \
overdue books, past lost or damaged reports), tell the user about them and still propose the loan so a \
human can decide. Do not refuse on your own because of them.
- Past human decisions are guidance only. Always re-check the current status, reservations and due \
date.

You must never decide, and must refer to the librarian with escalate_to_librarian:
- fine amounts, or waiving or reducing fines
- changing a borrowing or due date (including around festivals)
- deleting records, marking a book lost, or correcting a record
- who is responsible for a missing or damaged book. Never blame or accuse a student.

When you are unsure: search the register and rules, ask the user, check the database with your tools, \
then suggest escalating to the librarian. If it is still unresolved, say "I don't know". Never guess \
or make up books, students, dates or policies.

Privacy: never ask for, repeat or store passwords or login details, and do not ask for personal \
information the library does not need (name, grade and student ID are enough).

Keep replies short and plain. Committee members are students and teachers working at a busy desk."""

_EFFORT = "medium"


def _block_to_dict(block) -> dict:
    if isinstance(block, dict):
        return block
    if hasattr(block, "to_dict"):
        return block.to_dict()
    return block.model_dump(exclude_none=True)


def _text_of(content: list[dict]) -> str:
    return "\n".join(b["text"] for b in content if b.get("type") == "text" and b.get("text")).strip()


class LibraryAgent:
    def __init__(self, conn: sqlite3.Connection, client=None, model: str | None = None,
                 max_steps: int | None = None):
        self.conn = conn
        self._client = client
        self.model = model or settings.model
        self.max_steps = max_steps or settings.agent_max_steps

    @property
    def client(self):
        if self._client is None:
            import anthropic  # imported lazily so tests run without it

            try:  # use the Windows/macOS certificate store (school networks often inspect HTTPS)
                import truststore

                truststore.inject_into_ssl()
            except ImportError:
                pass
            self._client = anthropic.Anthropic()
        return self._client

    # ---------------------------------------------------------- sessions

    def new_session(self, user_name: str) -> str:
        sid = uuid.uuid4().hex
        self.conn.execute("INSERT INTO chat_sessions (session_id, user_name, updated_at) VALUES (?, ?, ?)",
                          (sid, user_name, now()))
        self.conn.commit()
        return sid

    def history(self, session_id: str) -> list[dict]:
        row = self.conn.execute("SELECT messages FROM chat_sessions WHERE session_id = ?",
                                (session_id,)).fetchone()
        return json.loads(row["messages"]) if row else []

    def _save(self, session_id: str, messages: list[dict]) -> None:
        self.conn.execute("UPDATE chat_sessions SET messages = ?, updated_at = ? WHERE session_id = ?",
                          (json.dumps(messages), now(), session_id))
        self.conn.commit()

    def transcript(self, session_id: str) -> list[tuple[str, str]]:
        """(speaker, text) pairs for display, without tool plumbing."""
        out = []
        for m in self.history(session_id):
            if isinstance(m["content"], str):
                out.append((m["role"], m["content"]))
                continue
            text = _text_of(m["content"])
            if text:
                out.append((m["role"], text))
        return out

    # ---------------------------------------------------------- the loop

    def chat(self, session_id: str, user_name: str, text: str, *, role: str = "committee",
             today: date | None = None) -> str:
        today = today or date.today()
        clean = redact(text)  # passwords never reach storage or the API (test 20)
        header = f"[Today is {today.isoformat()} ({today.strftime('%A')}). Signed in: {user_name}, {role}.]"
        return self._run(session_id, f"{header}\n{clean}",
                         ToolContext(self.conn, user_name, role, session_id, today))

    def resume(self, session_id: str, event: str, *, today: date | None = None) -> str:
        """Continue a paused task after a human approved or rejected an action."""
        row = self.conn.execute("SELECT user_name FROM chat_sessions WHERE session_id = ?",
                                (session_id,)).fetchone()
        if row is None:
            return ""
        today = today or date.today()
        msg = (f"[Approvals update, {today.isoformat()}] {event}\n"
               "Tell the user what happened in one or two sentences and continue the task if "
               "anything is left to do.")
        return self._run(session_id, msg, ToolContext(self.conn, row["user_name"], "committee", session_id, today))

    def _run(self, session_id: str, user_text: str, ctx: ToolContext) -> str:
        messages = self.history(session_id)
        messages.append({"role": "user", "content": user_text})
        tools = tool_definitions()

        for _ in range(self.max_steps):
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
                thinking={"type": "adaptive"},
                output_config={"effort": _EFFORT},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            content = [_block_to_dict(b) for b in response.content]
            messages.append({"role": "assistant", "content": content})

            if response.stop_reason == "refusal":
                self._save(session_id, messages)
                return "I can't help with that request. Please ask the librarian."
            if response.stop_reason == "pause_turn":
                continue
            if response.stop_reason != "tool_use":
                self._save(session_id, messages)
                return _text_of(content) or "I don't know."

            results = []
            for block in content:
                if block.get("type") != "tool_use":
                    continue
                result, is_error = run_tool(ctx, block["name"], block.get("input") or {})
                results.append({"type": "tool_result", "tool_use_id": block["id"],
                                "content": json.dumps(result, default=str), "is_error": is_error})
            messages.append({"role": "user", "content": results})

        self._save(session_id, messages)
        return (f"I stopped after {self.max_steps} steps without finishing. Please check the "
                "approvals page, or ask the librarian.")
