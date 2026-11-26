"""Bulk add books (MINI SPEC 3.1, 3.4 and 3.5).

The register keeps one row per physical copy (B-062, B-062-2, ...). A "title"
here is every copy that shares a name and author, and its copy count is the
number of those rows. Raising the count adds copy rows; lowering it removes
(soft-deletes) copies that are on the shelf, never ones that are out.

Every way of adding books (file, scan, voice, typed) builds the same list of
rows, which goes through check() for the preview and save() to store it.
"""
import json
import re
import unicodedata
from datetime import date
from difflib import SequenceMatcher

from . import activity
from .db import now
from .states import AVAILABLE, BORROWED, OVERDUE
from .uploads import MAX_ROWS

FIELDS = ("isbn", "title", "author", "publisher", "year", "category", "copies")
SYNONYMS = {
    "isbn": ("isbn", "isbn13", "isbn 13", "isbn10", "isbn no", "isbn number"),
    "title": ("title", "book title", "book name", "name", "name of the book", "book"),
    "author": ("author", "authors", "author name", "writer", "written by"),
    "publisher": ("publisher", "publishers", "publication", "published by"),
    "year": ("year", "publication year", "year of publication", "pub year", "published", "edition year"),
    "category": ("category", "genre", "subject", "type", "language"),
    "copies": ("copies", "copy", "no of copies", "number of copies", "qty", "quantity", "count", "nos"),
}
SIMILAR = 0.9          # "more than 90% similar" (MINI SPEC 3.5)
DECISIONS = ("set", "add", "skip", "new")


class CatalogueError(ValueError):
    pass


# ---------------------------------------------------------------- cleaning

def _text(value, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text)).strip()


def isbn_digits(raw: str) -> str:
    return re.sub(r"[^0-9Xx]", "", raw or "").upper()


def isbn_valid(isbn: str) -> bool:
    if len(isbn) == 10 and re.fullmatch(r"\d{9}[\dX]", isbn):
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(isbn))
        return total % 11 == 0
    if len(isbn) == 13 and isbn.isdigit():
        total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(isbn))
        return total % 10 == 0
    return False


def isbn13(isbn: str) -> str:
    """One form for comparing: ISBN-10s are turned into ISBN-13s."""
    if len(isbn) != 10:
        return isbn
    core = "978" + isbn[:9]
    check = (10 - sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core)) % 10) % 10
    return core + str(check)


def _whole_number(value) -> int | None:
    text = _text(value)
    if re.fullmatch(r"-?\d+(\.0+)?", text):
        return int(float(text))
    return None


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    m = SequenceMatcher(None, a, b, autojunk=False)
    if m.real_quick_ratio() <= SIMILAR or m.quick_ratio() <= SIMILAR:
        return 0.0  # cannot reach 90%, so skip the slow comparison
    return m.ratio()


# ---------------------------------------------------------------- the catalogue as titles

def _pending_book_ids(conn) -> set[str]:
    ids = set()
    for row in conn.execute("SELECT details FROM pending_actions WHERE status = 'pending'"):
        book_id = json.loads(row["details"]).get("book_id")
        if book_id:
            ids.add(book_id)
    return ids


class Catalogue:
    """Every title in the library, built once per check or save."""

    def __init__(self, conn):
        self.conn = conn
        self.all_ids = {r[0] for r in conn.execute("SELECT book_id FROM books")}
        reserved = {r[0] for r in conn.execute("SELECT book_id FROM reservations WHERE active = 1")}
        pending = _pending_book_ids(conn)
        groups: dict[tuple, dict] = {}
        for b in conn.execute("SELECT * FROM books WHERE deleted_at IS NULL ORDER BY copy_number, book_id"):
            key = (norm(b["name"]), norm(b["author"]))
            g = groups.get(key)
            if g is None:
                g = groups[key] = {"key": key, "title": b["name"], "author": b["author"],
                                   "category": b["category"], "first_id": b["book_id"], "copies": [],
                                   "isbns": set(), "isbn": "", "publisher": "", "year": ""}
            g["copies"].append(b)
            for field in ("isbn", "publisher", "year", "category"):
                if b[field] and not g[field]:
                    g[field] = b[field]
            if b["isbn"]:
                g["isbns"].add(isbn13(isbn_digits(b["isbn"])))
        for g in groups.values():
            copies = g["copies"]
            g["total"] = len(copies)
            g["available"] = sum(1 for b in copies if b["status"] == AVAILABLE)
            g["issued"] = sum(1 for b in copies if b["status"] in (BORROWED, OVERDUE))
            g["removable"] = [b for b in copies if b["status"] == AVAILABLE
                              and b["book_id"] not in reserved and b["book_id"] not in pending]
            g["min_total"] = g["total"] - len(g["removable"])
        self.groups = list(groups.values())
        self.by_isbn = {i: g for g in self.groups for i in g["isbns"]}

    def match(self, isbn: str, title: str, author: str):
        """The title already in the library that this book is, or None.
        Same ISBN, or title and author more than 90% similar. Most of the
        existing catalogue has no author, so then the title alone is compared."""
        if isbn and isbn13(isbn) in self.by_isbn:
            return self.by_isbn[isbn13(isbn)]
        t, a = norm(title), norm(author)
        if not t:
            return None
        best, best_score = None, SIMILAR
        for g in self.groups:
            gt, ga = g["key"]
            score = similarity(f"{t} {a}", f"{gt} {ga}") if (a and ga) else similarity(t, gt)
            if score > best_score:
                best, best_score = g, score
        return best

    @staticmethod
    def summary(g) -> dict:
        return {"book_id": g["first_id"], "title": g["title"], "author": g["author"],
                "total": g["total"], "available": g["available"], "issued": g["issued"],
                "min_total": g["min_total"]}


def min_total_message(match: dict) -> str:
    n, issued = match["min_total"], match["issued"]
    if n == issued:
        return f"{issued} copies are currently issued. Total can't be less than {issued}."
    return f"{n} copies are issued, reserved, lost or damaged. Total can't be less than {n}."


# ---------------------------------------------------------------- check (the preview)

def _clean(row: dict) -> dict:
    out = {k: _text(row.get(k)) for k in ("title", "author", "publisher", "category")}
    out["isbn"] = isbn_digits(_text(row.get("isbn")))
    out["isbn_raw"] = _text(row.get("isbn"))
    year = _text(row.get("year"))
    out["year"] = str(int(float(year))) if re.fullmatch(r"\d{4}(\.0+)?", year) else year
    copies = _text(row.get("copies"))
    out["copies"] = copies if copies else "1"   # empty Copies means 1 (MINI SPEC 3.1)
    out["id"] = _text(row.get("id"), 40)
    out["source"] = row.get("source") if row.get("source") in activity.SOURCES else "manual"
    out["skip"] = bool(row.get("skip"))
    decision = row.get("decision") or {}
    out["decision"] = {"mode": decision.get("mode") if decision.get("mode") in DECISIONS else "",
                       "value": _text(decision.get("value"), 10)}
    return out


def _same_book(a: dict, b: dict) -> bool:
    if a["isbn"] and b["isbn"] and isbn_valid(a["isbn"]) and isbn_valid(b["isbn"]):
        return isbn13(a["isbn"]) == isbn13(b["isbn"])
    ta, tb = norm(a["title"]), norm(b["title"])
    if not ta or not tb:
        return False
    return similarity(f"{ta} {norm(a['author'])}", f"{tb} {norm(b['author'])}") > SIMILAR


def _merge(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """The same book twice in one list: one row, copies added together (MINI SPEC 6)."""
    merged: list[dict] = []
    notices = []
    for row in rows:
        twin = None if row["skip"] else next(
            (m for m in merged if not m["skip"] and _same_book(m, row)), None)
        if twin is None:
            merged.append(row)
            continue
        a, b = _whole_number(twin["copies"]), _whole_number(row["copies"])
        if a is not None and b is not None and a > 0 and b > 0:
            twin["copies"] = str(a + b)
        for field in ("isbn", "isbn_raw", "title", "author", "publisher", "year", "category"):
            if not twin[field] and row[field]:
                twin[field] = row[field]
        name = twin["title"] or twin["isbn_raw"]
        notices.append(f"“{name}” was in the list more than once, so the rows were merged "
                       f"into one with {twin['copies']} copies.")
    return merged, notices


def _errors(row: dict) -> list[str]:
    errors = []
    if not row["title"]:
        errors.append("Title is missing.")
    if not row["author"]:
        errors.append("Author is missing.")
    if row["isbn_raw"] and not isbn_valid(row["isbn"]):
        errors.append(f"ISBN {row['isbn_raw']} is not valid.")
    copies = _whole_number(row["copies"])
    if copies is None:
        errors.append("Copies must be a whole number.")
    elif copies < 1:
        errors.append("Copies must be 1 or more; 0 and negative numbers are not allowed.")
    if row["year"] and not (re.fullmatch(r"\d{4}", row["year"])
                            and 1000 <= int(row["year"]) <= date.today().year + 1):
        errors.append(f"Year {row['year']} is not a valid year.")
    return errors


def _decision_problem(row: dict, match: dict) -> str:
    mode, value = row["decision"]["mode"], _whole_number(row["decision"]["value"])
    if not mode:
        return (f"“{match['title']}” is already in the library with {match['total']} "
                f"{'copy' if match['total'] == 1 else 'copies'}. How many copies are there in the library now?")
    if mode in ("set", "add"):
        if value is None:
            return "Enter a whole number of copies."
        if value < 1:
            return "Copies must be 1 or more; 0 and negative numbers are not allowed."
        if mode == "set" and value < match["min_total"]:
            return min_total_message(match)
    return ""


def check(conn, rows: list[dict], catalogue: Catalogue | None = None) -> dict:
    """Give every row a status: new, exists (asks for the copy count) or error."""
    if len(rows) > MAX_ROWS:
        raise CatalogueError(f"The list has {len(rows)} rows. The most one save can take is {MAX_ROWS:,}.")
    cat = catalogue or Catalogue(conn)
    cleaned, notices = _merge([_clean(r) for r in rows])
    out = []
    for row in cleaned:
        g = cat.match(row["isbn"] if isbn_valid(row["isbn"]) else "", row["title"], row["author"])
        if g is not None:
            # A scanned ISBN that is already in the library fills in its details.
            for field, value in (("title", g["title"]), ("author", g["author"]), ("category", g["category"]),
                                 ("publisher", g["publisher"]), ("year", g["year"])):
                if not row[field] and value:
                    row[field] = value
        row["errors"] = _errors(row)
        row["match"] = Catalogue.summary(g) if g is not None and row["title"] else None
        if row["match"] and row["decision"]["mode"] != "new" and not g["author"]:
            # Most old catalogue entries have no author; only the copy count changes here.
            row["errors"] = [e for e in row["errors"] if e != "Author is missing."]
        if row["skip"]:
            row["status"], row["message"], row["ready"] = "skip", "Skipped: this row will not be saved.", True
        elif row["errors"]:
            row["status"], row["message"], row["ready"] = "error", " ".join(row["errors"]), False
        elif row["match"] and row["decision"]["mode"] != "new":
            problem = _decision_problem(row, row["match"])
            row["status"], row["message"], row["ready"] = "exists", problem, not problem
        else:
            row["status"], row["ready"] = "new", True
            row["message"] = "Will be saved as a separate new book." if row["match"] else "Will be added."
        row["_group"] = g
        out.append(row)
    counts = {s: sum(1 for r in out if r["status"] == s) for s in ("new", "exists", "error", "skip")}
    return {"rows": out, "notices": notices, "counts": counts,
            "ready": all(r["ready"] for r in out) and any(r["status"] != "skip" for r in out)}


def public(result: dict) -> dict:
    """The check result without the internal fields, for the browser."""
    return {**result, "rows": [{k: v for k, v in r.items() if not k.startswith("_")} for r in result["rows"]]}


# ---------------------------------------------------------------- save

def _next_number(all_ids: set[str]) -> int:
    numbers = [int(m.group(1)) for i in all_ids if (m := re.fullmatch(r"B-(\d+)(?:-\d+)?", i))]
    return max(numbers, default=0) + 1


def _free_id(base: str, start: int, taken: set[str]) -> tuple[str, int]:
    n = start
    while True:
        book_id = base if n == 1 else f"{base}-{n}"
        if book_id not in taken:
            return book_id, n
        n += 1


INSERT_BOOK = ("INSERT INTO books (book_id, name, author, category, copy_number, status, isbn, publisher, year,"
               " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")


def _insert_copies(inserts: list, taken: set[str], base: str, first_copy: int, count: int, row: dict,
                   stamp: str) -> list[str]:
    """Queue the new copy rows; they are written together in one batch."""
    ids = []
    copy_number = first_copy
    for _ in range(count):
        book_id, copy_number = _free_id(base, copy_number, taken)
        taken.add(book_id)
        inserts.append((book_id, row["title"], row["author"], row["category"], copy_number, AVAILABLE,
                        row["isbn"], row["publisher"], row["year"], stamp, stamp))
        ids.append(book_id)
        copy_number += 1
    return ids


def _id_range(ids: list[str]) -> str:
    return ids[0] if len(ids) == 1 else f"{ids[0]} to {ids[-1]}"


def save(conn, rows: list[dict], user: str) -> dict:
    """Check the list again and save it all in one step, or nothing."""
    cat = Catalogue(conn)
    result = check(conn, rows, cat)
    waiting = [r for r in result["rows"] if not r["ready"]]
    if waiting:
        raise CatalogueError(f"{len(waiting)} row(s) still need attention (fix, answer or skip them) "
                             "before anything can be saved.")
    stamp = now()
    taken = set(cat.all_ids)
    next_number = _next_number(taken)
    added: dict[str, list[str]] = {}
    added_titles = added_copies = updated = skipped = 0
    inserts: list[tuple] = []
    try:
        for row in result["rows"]:
            if row["status"] == "skip":
                skipped += 1
                continue
            if row["status"] == "new":
                base = f"B-{next_number:03d}"
                next_number += 1
                ids = _insert_copies(inserts, taken, base, 1, int(row["copies"]), row, stamp)
                added.setdefault(row["source"], []).append(f"{row['title']} ({_id_range(ids)})")
                added_titles += 1
                added_copies += len(ids)
                continue
            # Already in the library: the librarian chose the copy count.
            g, mode = row["_group"], row["decision"]["mode"]
            if mode == "skip":
                skipped += 1
                continue
            value = int(row["decision"]["value"])
            target = value if mode == "set" else g["total"] + value
            for field in ("isbn", "publisher", "year"):   # fill details the old rows lack
                if row[field]:
                    conn.execute(f"UPDATE books SET {field} = ?, updated_at = ? WHERE deleted_at IS NULL"
                                 f" AND {field} = '' AND book_id IN ({','.join('?' * len(g['copies']))})",
                                 (row[field], stamp, *[b["book_id"] for b in g["copies"]]))
            if target == g["total"]:
                skipped += 1
                continue
            if target > g["total"]:
                base = re.sub(r"^(B-\d+)-\d+$", r"\1", g["first_id"])
                start = max(b["copy_number"] for b in g["copies"]) + 1
                source_row = {**row, "title": g["title"], "author": g["author"],
                              "category": row["category"] or g["category"],
                              "isbn": row["isbn"] or g["isbn"], "publisher": row["publisher"] or g["publisher"],
                              "year": row["year"] or g["year"]}
                ids = _insert_copies(inserts, taken, base, start, target - g["total"], source_row, stamp)
                change = f"added {_id_range(ids)}"
            else:
                remove = sorted(g["removable"], key=lambda b: (b["copy_number"], b["book_id"]),
                                reverse=True)[:g["total"] - target]
                for b in remove:
                    conn.execute("UPDATE books SET deleted_at = ?, deleted_by = ?, delete_reason = ?, updated_at = ?"
                                 " WHERE book_id = ?",
                                 (stamp, user, f"Copy count set to {target}", stamp, b["book_id"]))
                change = "removed " + ", ".join(b["book_id"] for b in remove)
            activity.log(conn, user, "UPDATE_COPIES",
                         f"{g['title']} ({g['first_id']}): copies {g['total']} -> {target} ({change})",
                         row["source"])
            g["total"] = target  # a second row for the same title builds on this
            updated += 1
        if inserts:
            conn.executemany(INSERT_BOOK, inserts)
        for source, titles in added.items():
            count = len(titles)
            details = f"Added {count} title{'s' if count != 1 else ''}: " + "; ".join(titles)
            activity.log(conn, user, "BULK_ADD", details[:4000], source)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    message = f"{added_titles} added, {updated} updated, {skipped} skipped."
    if added_copies != added_titles:
        message = (f"{added_titles} added ({added_copies} copies), {updated} updated, {skipped} skipped.")
    return {"added": added_titles, "added_copies": added_copies, "updated": updated,
            "skipped": skipped, "message": message, "notices": result["notices"]}
