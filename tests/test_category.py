"""Book categories: schema upgrade, import and search."""
import sqlite3

from conftest import ROOT
from library_agent import importer, register
from library_agent.db import connect, init_db


def test_old_database_gets_category_column(tmp_path):
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE books (book_id TEXT PRIMARY KEY, name TEXT NOT NULL,"
                " author TEXT NOT NULL DEFAULT '', copy_number INTEGER NOT NULL DEFAULT 1,"
                " status TEXT NOT NULL DEFAULT 'Available', condition_notes TEXT NOT NULL DEFAULT '',"
                " deleted_at TEXT, deleted_by TEXT, delete_reason TEXT)")
    old.execute("INSERT INTO books (book_id, name) VALUES ('B-1', 'Old book')")
    old.commit()
    old.close()
    c = connect(path)
    init_db(c)
    init_db(c)  # running twice is harmless
    assert c.execute("SELECT category FROM books WHERE book_id = 'B-1'").fetchone()[0] == ""


def test_find_books_by_category(conn):
    assert register.categories(conn) == ["Biography", "Stories"]
    assert [b["book_id"] for b in register.find_books(conn, "biography")] == ["B-201"]
    stories = register.find_books(conn, "", category="Stories")
    assert len(stories) == 4 and all(b["category"] == "Stories" for b in stories)
    assert register.find_books(conn, "Jungle", category="Biography") == []


def test_import_without_category_column_keeps_categories(conn, tmp_path):
    f = tmp_path / "books.csv"
    f.write_text("book_id,name,author,copy_number,condition_notes\nB-201,Wings of Fire (new),,1,\n",
                 encoding="utf-8")
    importer.import_books(conn, str(f))
    row = register.get_book(conn, "B-201")
    assert (row["name"], row["category"]) == ("Wings of Fire (new)", "Biography")
