"""Policy numbers and settings.

Phase 0 values are provisional defaults until the committee confirms them
(see "Open decisions" in SPEC.md). Every value can be overridden with an
environment variable so nothing needs a code change.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass
class Settings:
    db_path: str = os.environ.get("LIBRARY_DB", str(BASE_DIR / "library.db"))

    # Policy numbers (Phase 0, open decision 5)
    loan_period_days: int = _int("LOAN_PERIOD_DAYS", 7)
    loan_limit: int = _int("LOAN_LIMIT", 2)
    max_renewals: int = _int("MAX_RENEWALS", 1)
    reservation_hold_days: int = _int("RESERVATION_HOLD_DAYS", 3)
    school_escalation_days: int = _int("SCHOOL_ESCALATION_DAYS", 7)

    # Email
    school_email: str = os.environ.get("SCHOOL_EMAIL", "office@school.example")
    committee_email: str = os.environ.get("COMMITTEE_EMAIL", "library-committee@school.example")
    smtp_host: str = os.environ.get("SMTP_HOST", "")
    smtp_port: int = _int("SMTP_PORT", 587)
    smtp_user: str = os.environ.get("SMTP_USER", "")
    smtp_password: str = os.environ.get("SMTP_PASSWORD", "")  # read from env, never stored in the DB
    mail_from: str = os.environ.get("MAIL_FROM", "library@school.example")

    # Agent
    model: str = os.environ.get("LIBRARY_AGENT_MODEL", "claude-opus-5-5")
    agent_max_steps: int = _int("AGENT_MAX_STEPS", 8)

    # Web sign-in: "dev" (pick a name, local testing only) or "header"
    # (a school sign-in proxy puts the signed-in email in a header).
    auth_mode: str = os.environ.get("AUTH_MODE", "dev")
    auth_header: str = os.environ.get("AUTH_HEADER", "X-Forwarded-Email")
    session_secret: str = os.environ.get("SESSION_SECRET", "change-me")
    members_file: str = os.environ.get("MEMBERS_FILE", str(BASE_DIR / "members.csv"))

    backup_dir: str = os.environ.get("BACKUP_DIR", str(BASE_DIR / "backups"))
    knowledge_dir: Path = field(default=BASE_DIR / "library_agent" / "knowledge")


settings = Settings()
