"""Policy numbers and settings.

Phase 0 values are provisional defaults until the committee confirms them
(see "Open decisions" in SPEC.md). Every value can be overridden with an
environment variable so nothing needs a code change.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    """Read NAME=value lines from settings.env (kept out of git) so secrets
    such as DATABASE_URL live in one file. Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


_load_env_file(Path(os.environ.get("LIBRARY_ENV_FILE", BASE_DIR / "settings.env")))


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass
class Settings:
    # DATABASE_URL (PostgreSQL) wins; otherwise the SQLite file LIBRARY_DB.
    db_path: str = os.environ.get("DATABASE_URL") or os.environ.get("LIBRARY_DB", str(BASE_DIR / "library.db"))

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

    # Agent. AI_PROVIDER "none" (default) runs the library from the Desk page
    # only, with no AI and no API costs. "anthropic" turns on the AI chat and
    # needs ANTHROPIC_API_KEY plus `pip install -r requirements-ai.txt`.
    ai_provider: str = os.environ.get("AI_PROVIDER", "none").strip().lower()
    model: str = os.environ.get("LIBRARY_AGENT_MODEL", "claude-opus-5-5")
    agent_max_steps: int = _int("AGENT_MAX_STEPS", 8)

    # Web sign-in: "dev" (pick a name, local testing only), "code" (the app
    # emails a one-time code to a school address on the committee list; needs
    # SMTP_HOST) or "header" (a school sign-in proxy puts the signed-in email
    # in a header).
    auth_mode: str = os.environ.get("AUTH_MODE", "dev")
    # With AUTH_MODE=password: "yes" gives every member one shared password,
    # "no" (default) gives each member their own.
    shared_password: bool = os.environ.get("SHARED_PASSWORD", "no").strip().lower() in ("1", "yes", "true")
    auth_header: str = os.environ.get("AUTH_HEADER", "X-Forwarded-Email")
    session_secret: str = os.environ.get("SESSION_SECRET", "change-me")
    members_file: str = os.environ.get("MEMBERS_FILE", str(BASE_DIR / "members.csv"))
    # On a host with no members.csv file (Vercel), the same CSV text can be
    # given in MEMBERS_CSV instead. It wins over MEMBERS_FILE when set.
    members_csv: str = os.environ.get("MEMBERS_CSV", "")
    # Vercel cron calls /cron/daily-job with "Authorization: Bearer <CRON_SECRET>".
    cron_secret: str = os.environ.get("CRON_SECRET", "")

    backup_dir: str = os.environ.get("BACKUP_DIR", str(BASE_DIR / "backups"))
    knowledge_dir: Path = field(default=BASE_DIR / "library_agent" / "knowledge")


settings = Settings()
