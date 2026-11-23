"""Command line: python -m library_agent <command>

  init-db                      create the database
  import-books FILE.csv        load or update the book catalogue
  import-students FILE.csv     load or update the student list
  daily-job                    reminders, overdue alerts, school email, reservation expiry
  backup                       copy the database to BACKUP_DIR
  restore FILE.db              restore the database from a backup (replaces everything)
  move-to-postgres [FILE.db]   copy library.db into the empty PostgreSQL database in DATABASE_URL
  serve [--port 8000]          start the web app
  totals                       print total and current books
  shelf-check FILE.csv         export every book's status to tick off against the shelf
  report [--from D] [--to D]   proposals, rejections, corrections and approval wait times
                               (default: the last 30 days)
  reset-password ["NAME"]      clear a member's forgotten password (AUTH_MODE=password);
                               they create a new one at their next sign-in. With no
                               name, clears every password, the shared one included
                               (use this with SHARED_PASSWORD=yes)
"""
import argparse
import json
from datetime import date, timedelta

from . import backup as backups
from . import importer, jobs, passwords, register, reports
from .config import settings
from .db import connect, describe, init_db
from .notifications import Mailer


def main(argv=None):
    p = argparse.ArgumentParser(prog="library_agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    sub.add_parser("import-books").add_argument("file")
    sub.add_parser("import-students").add_argument("file")
    sub.add_parser("daily-job")
    sub.add_parser("backup")
    sub.add_parser("restore").add_argument("file")
    sub.add_parser("move-to-postgres").add_argument("file", nargs="?", default="library.db")
    sub.add_parser("totals")
    sub.add_parser("shelf-check").add_argument("file")
    r = sub.add_parser("report")
    r.add_argument("--from", dest="start", type=date.fromisoformat)
    r.add_argument("--to", dest="end", type=date.fromisoformat)
    sub.add_parser("reset-password").add_argument("name", nargs="?")
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    args = p.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn
        from .web.app import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port)
        return
    if args.cmd == "restore":
        backups.restore(args.file, settings.db_path)
        print(f"Restored {describe(settings.db_path)} from {args.file}")
        return
    if args.cmd == "move-to-postgres":
        n = backups.move_to_postgres(args.file, settings.db_path)
        print(f"Copied {n} row(s) from {args.file} to {describe(settings.db_path)}")
        return

    conn = connect()
    init_db(conn)
    if args.cmd == "init-db":
        print(f"Database ready at {describe(settings.db_path)}")
    elif args.cmd == "import-books":
        print(f"Imported {importer.import_books(conn, args.file)} book(s)")
    elif args.cmd == "import-students":
        print(f"Imported {importer.import_students(conn, args.file)} student(s)")
    elif args.cmd == "daily-job":
        print(json.dumps(jobs.run_daily(conn, Mailer())))
    elif args.cmd == "backup":
        print(f"Backed up to {backups.backup(settings.db_path, settings.backup_dir)}")
    elif args.cmd == "totals":
        print(json.dumps(register.totals(conn), indent=2))
    elif args.cmd == "shelf-check":
        print(f"Wrote {reports.write_shelf_check(conn, args.file)} book(s) to {args.file}")
    elif args.cmd == "report":
        end = args.end or date.today()
        start = args.start or end - timedelta(days=30)
        print(reports.format_report(reports.period_report(conn, start, end)))
    elif args.cmd == "reset-password" and not args.name:
        passwords.clear_all(conn)
        print("All passwords cleared. The next person to sign in creates a new one.")
    elif args.cmd == "reset-password":
        from .web.app import member_list

        found = [m for m in member_list(settings.members_file)
                 if m["name"].lower() == args.name.strip().lower()]
        if not found:
            print(f'No member called "{args.name}" in {settings.members_file}')
        for m in found:
            if passwords.clear_password(conn, m):
                print(f"Password cleared for {m['name']}. They will create a new one at their next sign-in.")
            else:
                print(f"{m['name']} has no password set yet.")
    conn.close()


if __name__ == "__main__":
    main()
