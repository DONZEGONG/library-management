"""PostgreSQL support. The first tests need no database server; the rest run
only when TEST_DATABASE_URL points at a throwaway PostgreSQL database."""
import pytest

from conftest import ROOT, TEST_DATABASE_URL
from library_agent import backup, importer, register
from library_agent.db import Row, _to_pg, connect, describe, init_db, is_empty, is_postgres

needs_pg = pytest.mark.skipif(not TEST_DATABASE_URL, reason="set TEST_DATABASE_URL to run")


def test_placeholders_are_translated():
    assert _to_pg("SELECT * FROM books WHERE book_id = ? AND name LIKE ?") == \
        "SELECT * FROM books WHERE book_id = %s AND name LIKE %s"


def test_row_reads_by_name_and_position():
    r = Row(["book_id", "name"], ("B-1", "Charlotte"))
    assert r["name"] == "Charlotte" and r[0] == "B-1" and dict(r) == {"book_id": "B-1", "name": "Charlotte"}


def test_password_is_hidden_when_printed():
    url = "postgresql://library:s3cret@db.example.com:5432/library?sslmode=require"
    assert is_postgres(url) and not is_postgres("library.db")
    assert "s3cret" not in describe(url) and "library:***@db.example.com" in describe(url)


def test_move_needs_a_postgres_url(tmp_path):
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        backup.move_to_postgres(str(tmp_path / "x.db"), str(tmp_path / "y.db"))


def _sqlite_library(path):
    c = connect(str(path))
    init_db(c)
    importer.import_books(c, str(ROOT / "sample_data" / "books.csv"))
    importer.import_students(c, str(ROOT / "sample_data" / "students.csv"))
    c.execute("INSERT INTO correction_log (original_decision, human_correction, reason, corrected_by, date)"
              " VALUES ('a', 'b', 'c', 'd', '2026-11-11')")
    c.commit()
    c.close()


@needs_pg
def test_move_sqlite_into_postgres_then_refuse_twice(tmp_path, db_target):
    _sqlite_library(tmp_path / "old.db")
    assert backup.move_to_postgres(str(tmp_path / "old.db"), db_target) > 0
    c = connect(db_target)
    assert register.totals(c)["total_books"] == 5
    # New IDs continue after the copied ones.
    c.execute("INSERT INTO correction_log (original_decision, human_correction, reason, corrected_by, date)"
              " VALUES ('e', 'f', 'g', 'h', '2026-11-11')")
    assert [r[0] for r in c.execute("SELECT correction_id FROM correction_log ORDER BY 1")] == [1, 2]
    c.commit()
    c.close()
    with pytest.raises(RuntimeError, match="already has library data"):
        backup.move_to_postgres(str(tmp_path / "old.db"), db_target)


@needs_pg
def test_restore_replaces_postgres_data(tmp_path, conn, db_target):
    saved = backup.backup(db_target, str(tmp_path / "bk"))
    conn.execute("DELETE FROM students WHERE student_id = 'S-001'")
    conn.commit()
    backup.restore(str(saved), db_target)
    c = connect(db_target)
    assert c.execute("SELECT name FROM students WHERE student_id = 'S-001'").fetchone() is not None
    assert not is_empty(c)
    c.close()
