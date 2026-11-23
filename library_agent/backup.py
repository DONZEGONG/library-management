"""Daily backup and restore (PLAN 7.1). Point BACKUP_DIR at a folder that
syncs to cloud storage (for example the school's OneDrive or Google Drive).

Backups are always SQLite files (library-YYYY-MM-DD.db), whichever database
the app uses, so one backup can be opened or restored anywhere. With
PostgreSQL the backup is a copy of every table; the provider's own backups
are a second line of defence.
"""
import sqlite3
from datetime import date
from pathlib import Path

from .db import connect, copy_tables, describe, init_db, is_empty, is_postgres, TABLES


def backup(db: str, backup_dir: str, keep: int = 30) -> Path:
    out_dir = Path(backup_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"library-{date.today().isoformat()}.db"
    if is_postgres(db):
        partial = target.with_suffix(".partial")
        partial.unlink(missing_ok=True)
        src, dst = connect(db), connect(str(partial))
        try:
            init_db(dst)
            copy_tables(src, dst)
            dst.commit()
        finally:
            src.close()
            dst.close()
        partial.replace(target)  # a failed run never leaves a half-written backup
    else:
        src = sqlite3.connect(db)
        dst = sqlite3.connect(target)
        with dst:
            src.backup(dst)  # safe while the app is running
        src.close()
        dst.close()
    for old in sorted(out_dir.glob("library-*.db"))[:-keep]:
        old.unlink()
    return target


def _check(backup_file: str) -> None:
    if not Path(backup_file).exists():
        raise RuntimeError(f"{backup_file} does not exist.")
    src = sqlite3.connect(backup_file)
    try:
        if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"{backup_file} failed the integrity check; not restored.")
    finally:
        src.close()


def restore(backup_file: str, db: str) -> None:
    """Replace everything in the database with the backup."""
    _check(backup_file)
    if is_postgres(db):
        src, dst = connect(backup_file), connect(db)
        try:
            init_db(dst)
            # TRUNCATE (not DELETE) so the correction log's no-delete lock
            # does not block a deliberate full restore.
            dst.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
            copy_tables(src, dst)
            dst.commit()
        except Exception:
            dst.rollback()
            raise
        finally:
            src.close()
            dst.close()
        return
    src = sqlite3.connect(backup_file)
    dst = sqlite3.connect(db)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()


def move_to_postgres(sqlite_file: str, url: str) -> int:
    """One-time move of an existing library.db into an empty PostgreSQL
    database. Refuses if the PostgreSQL database already has data."""
    if not is_postgres(url):
        raise RuntimeError("Set DATABASE_URL to your PostgreSQL connection string first.")
    _check(sqlite_file)
    src, dst = connect(sqlite_file), connect(url)
    try:
        init_db(dst)
        if not is_empty(dst):
            raise RuntimeError(f"{describe(url)} already has library data; nothing was copied. "
                               "To replace it on purpose, use: restore " + sqlite_file)
        n = copy_tables(src, dst)
        dst.commit()
        return n
    except Exception:
        dst.rollback()
        raise
    finally:
        src.close()
        dst.close()
