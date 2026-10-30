# AI Library Management Agent

A web app over a library database for a school library committee. A Claude-powered agent
answers questions and proposes borrowings, returns, damage reports and renewals. Every
higher-risk change waits for a committee member to approve it. See [SPEC.md](SPEC.md) and
[PLAN.md](PLAN.md).

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# On networks that inspect HTTPS (pip fails with CERTIFICATE_VERIFY_FAILED), add:
#   --use-feature=truststore
```

Set your Claude API key (from https://console.anthropic.com):

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

## First run

```powershell
$py = ".\.venv\Scripts\python.exe"
& $py -m library_agent init-db
& $py -m library_agent import-books sample_data\books.csv        # or your own catalogue
& $py -m library_agent import-students sample_data\students.csv
Copy-Item members.example.csv members.csv                        # then edit the names
& $py -m library_agent serve                                     # http://127.0.0.1:8000
```

CSV columns:

- `books.csv`: `book_id,name,author,copy_number,condition_notes`
- `students.csv`: `student_id,name,grade,contact_email`
- `members.csv`: `name,email,role` (role is `committee` or `librarian`)

## Daily job and backup

Run once a day with Windows Task Scheduler (or cron). It is safe to run twice:

```powershell
.\.venv\Scripts\python.exe -m library_agent daily-job   # reminders, overdue alerts, school email, reservation expiry
.\.venv\Scripts\python.exe -m library_agent backup      # copies the DB to BACKUP_DIR
```

Point `BACKUP_DIR` at a folder that syncs to cloud storage. Restore with
`python -m library_agent restore backups\library-YYYY-MM-DD.db`.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `LIBRARY_DB` | `library.db` | Database file |
| `LOAN_LIMIT` | 2 | Books per student *(provisional)* |
| `MAX_RENEWALS` | 1 | Renewals per loan *(provisional)* |
| `RESERVATION_HOLD_DAYS` | 3 | How long a reservation is held *(provisional)* |
| `SCHOOL_ESCALATION_DAYS` | 7 | Overdue days before the school is emailed *(provisional)* |
| `SCHOOL_EMAIL` | `office@school.example` | School mail ID |
| `COMMITTEE_EMAIL` | `library-committee@school.example` | Where red alerts go |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `MAIL_FROM` | | School email server. With no `SMTP_HOST`, messages are only recorded in the database. |
| `AUTH_MODE` | `dev` | `dev` = pick your name (local testing only). `header` = a school sign-in proxy passes the signed-in email in `AUTH_HEADER`. |
| `SESSION_SECRET` | `change-me` | Set a long random value in real use |
| `LIBRARY_AGENT_MODEL` | `claude-opus-5-5` | Claude model |

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The tests use a fake AI client, so they need no API key. `tests/test_cases.py` holds the 20 test
cases from PLAN Phase 6.

## Layout

| Path | What it does |
|---|---|
| `library_agent/db.py` | Tables (PLAN 1.1). The correction log cannot be edited or deleted. |
| `library_agent/states.py` | The 7 book states and allowed transitions (Layer 3) |
| `library_agent/tools.py` | The agent's tools. Write tools create pending actions. |
| `library_agent/approvals.py` | Approve or reject pending actions |
| `library_agent/librarian.py` | Human-only actions: fines, due dates, lost, delete, corrections |
| `library_agent/jobs.py` | The daily job |
| `library_agent/agent.py` | The Claude agent loop and system prompt |
| `library_agent/knowledge/` | Rules and policies the agent can read (fine policy is librarian only) |
| `library_agent/web/` | The web pages |
| `docs/committee-guide.md` | One-page guide for committee members |
