"""Command line: python -m library_agent <command>

  init-db                      create the database
  import-books FILE.csv        load or update the book catalogue
  import-students FILE.csv     load or update the student list
  daily-job                    reminders, overdue alerts, school email, reservation expiry
  backup                       copy the database to BACKUP_DIR
  restore FILE.db              restore the database from a backup
  serve [--port 8000]          start the web app
  totals                       print total and current books
"""
import argparse
import json

from . import backup as backups
from . import importer, jobs, register
from .config import settings
from .db import connect, init_db
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
    sub.add_parser("totals")
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
        print(f"Restored {settings.db_path} from {args.file}")
        return

    conn = connect()
    init_db(conn)
    if args.cmd == "init-db":
        print(f"Database ready at {settings.db_path}")
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
    conn.close()


if __name__ == "__main__":
    main()
