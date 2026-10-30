"""Daily backup and restore (PLAN 7.1). Point BACKUP_DIR at a folder that
syncs to cloud storage (for example the school's OneDrive or Google Drive)."""
import sqlite3
from datetime import date
from pathlib import Path


def backup(db_path: str, backup_dir: str, keep: int = 30) -> Path:
    out_dir = Path(backup_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"library-{date.today().isoformat()}.db"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)  # safe while the app is running
    src.close()
    dst.close()
    for old in sorted(out_dir.glob("library-*.db"))[:-keep]:
        old.unlink()
    return target


def restore(backup_file: str, db_path: str) -> None:
    src = sqlite3.connect(backup_file)
    if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError(f"{backup_file} failed the integrity check; not restored.")
    dst = sqlite3.connect(db_path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
