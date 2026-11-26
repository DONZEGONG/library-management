"""Activity log (MINI SPEC 4): every bulk add, copy-count change and student
add, remove or restore is written here, with who did it and how."""
from .db import now

ACTIONS = ("BULK_ADD", "UPDATE_COPIES", "ADD_STUDENT", "EDIT_STUDENT", "REMOVE_STUDENT", "RESTORE_STUDENT")
SOURCES = ("file", "scan", "voice", "manual")


def log(conn, user: str, action: str, details: str, source: str = "manual") -> None:
    if action not in ACTIONS:
        raise ValueError(f"Unknown activity {action}.")
    if source not in SOURCES:
        source = "manual"
    conn.execute(
        "INSERT INTO activity_log (user_name, action, details, source, created_at) VALUES (?, ?, ?, ?, ?)",
        (user, action, details, source, now()),
    )


def recent(conn, limit: int = 200, action: str = "") -> list:
    sql, params = "SELECT * FROM activity_log", []
    if action:
        sql += " WHERE action = ?"
        params.append(action)
    return conn.execute(sql + " ORDER BY activity_id DESC LIMIT ?", (*params, limit)).fetchall()
