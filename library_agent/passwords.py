"""Committee sign-in passwords (AUTH_MODE=password).

Each member creates their own password the first time they sign in, and can
change it later after typing the current one. With SHARED_PASSWORD=yes there is
one password for every member instead: the first person to sign in creates it,
and a change made by anyone changes it for everyone. Only a salted PBKDF2 hash is
stored, in the member_passwords table. The librarian can clear a forgotten
password with `python -m library_agent reset-password "Name"`.
"""
import hashlib
import hmac
import secrets
import time

from .config import settings
from .db import now

ITERATIONS = 600_000
MIN_LENGTH = 8

# Short, common words for suggested passwords (easy to read out and type).
WORDS = """apple river mango tiger lotus cloud paper pencil garden window yellow purple
orange silver golden bright quiet happy rocket planet forest island candle bridge
monsoon peacock banyan jasmine coconut parrot temple harbor meadow thunder summer
winter breeze marble violet copper pepper cotton saffron elephant rainbow""".split()


def member_key(member: dict) -> str:
    """Members who share one email each get their own password, so the key is both."""
    return f"{member['email'].strip().lower()}|{member['name'].strip().lower()}"


SHARED_KEY = "*shared*"


def key_for(member: dict) -> str:
    """The stored-password row this member signs in with."""
    return SHARED_KEY if settings.shared_password else member_key(member)


def _adopt_shared(conn) -> None:
    """Switching to one shared password: the most recently set member password
    becomes the shared one, so nobody has to make a new password."""
    if conn.execute("SELECT 1 FROM member_passwords WHERE member_key = ?", (SHARED_KEY,)).fetchone():
        return
    row = conn.execute("SELECT password_hash, set_at FROM member_passwords "
                       "ORDER BY set_at DESC LIMIT 1").fetchone()
    if row:
        conn.execute("INSERT INTO member_passwords (member_key, password_hash, set_at) VALUES (?, ?, ?)",
                     (SHARED_KEY, row["password_hash"], row["set_at"]))
        conn.commit()


def _row(conn, member: dict):
    if settings.shared_password:
        _adopt_shared(conn)
    return conn.execute("SELECT password_hash FROM member_passwords WHERE member_key = ?",
                        (key_for(member),)).fetchone()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def check_hash(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


def has_password(conn, member: dict) -> bool:
    return _row(conn, member) is not None


def verify(conn, member: dict, password: str) -> bool:
    row = _row(conn, member)
    return row is not None and check_hash(password, row["password_hash"])


def problem(new: str, confirm: str) -> str | None:
    """Why a new password can't be used, or None if it is fine."""
    if len(new) < MIN_LENGTH:
        return f"The password must be at least {MIN_LENGTH} characters."
    if new != confirm:
        return "The two passwords do not match. Type the same password in both boxes."
    if new.strip() != new:
        return "The password can't start or end with a space."
    return None


def set_password(conn, member: dict, password: str) -> None:
    conn.execute("INSERT INTO member_passwords (member_key, password_hash, set_at) VALUES (?, ?, ?) "
                 "ON CONFLICT(member_key) DO UPDATE SET password_hash = excluded.password_hash, "
                 "set_at = excluded.set_at",
                 (key_for(member), hash_password(password), now()))
    conn.commit()


def clear_password(conn, member: dict) -> bool:
    cur = conn.execute("DELETE FROM member_passwords WHERE member_key = ?", (member_key(member),))
    conn.commit()
    return cur.rowcount > 0


def clear_all(conn) -> int:
    """Remove every stored password, the shared one included."""
    cur = conn.execute("DELETE FROM member_passwords")
    conn.commit()
    return cur.rowcount


def suggestions(n: int = 3) -> list[str]:
    """Easy-to-type passwords a member can pick instead of making one up."""
    return [f"{'-'.join(secrets.choice(WORDS) for _ in range(3))}-{secrets.randbelow(90) + 10}"
            for _ in range(n)]


class Attempts:
    """Slows down guessing: after MAX_TRIES wrong passwords a member must wait.

    With `connect` (a function that opens the database) the counts are kept in
    the login_attempts table, so they hold when the app runs on several servers
    at once (as on Vercel). Without it they are kept in memory only."""
    MAX_TRIES = 5
    WAIT = 5 * 60  # seconds

    def __init__(self, clock=time.time, connect=None):
        self.clock = clock
        self.connect = connect
        self._fails: dict[str, dict] = {}

    # -- storage
    def _get(self, key):
        if self.connect is None:
            return self._fails.get(key)
        conn = self.connect()
        try:
            row = conn.execute("SELECT fails, since FROM login_attempts WHERE attempt_key = ?",
                               (key,)).fetchone()
        finally:
            conn.close()
        return {"count": row["fails"], "since": row["since"]} if row else None

    def _put(self, key, count, since):
        if self.connect is None:
            self._fails[key] = {"count": count, "since": since}
            return
        conn = self.connect()
        try:
            conn.execute("INSERT INTO login_attempts (attempt_key, fails, since) VALUES (?, ?, ?) "
                         "ON CONFLICT(attempt_key) DO UPDATE SET fails = excluded.fails, "
                         "since = excluded.since", (key, count, since))
            conn.commit()
        finally:
            conn.close()

    def _drop(self, key):
        if self.connect is None:
            self._fails.pop(key, None)
            return
        conn = self.connect()
        try:
            conn.execute("DELETE FROM login_attempts WHERE attempt_key = ?", (key,))
            conn.commit()
        finally:
            conn.close()

    # -- rules
    def locked(self, key: str) -> int:
        """Seconds left to wait, or 0."""
        entry = self._get(key)
        if not entry or entry["count"] < self.MAX_TRIES:
            return 0
        left = self.WAIT - (self.clock() - entry["since"])
        if left <= 0:
            self._drop(key)
            return 0
        return int(left) + 1

    def failed(self, key: str) -> None:
        entry = self._get(key)
        self._put(key, (entry["count"] if entry else 0) + 1, self.clock())

    def succeeded(self, key: str) -> None:
        if self._get(key):
            self._drop(key)
