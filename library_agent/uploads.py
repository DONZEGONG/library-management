"""Read an uploaded CSV or Excel file and guess which column is which
(MINI SPEC 3.1 and 3.2). Used by bulk add books and bulk add students."""
import csv
import io
import re
from datetime import date, datetime

MAX_ROWS = 1000


class UploadError(ValueError):
    pass


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # Excel keeps numbers such as ISBNs and years as floats
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:10]
    return re.sub(r"\s+", " ", str(value)).strip()


def _from_csv(data: bytes) -> list[list[str]]:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise UploadError("The file could not be read. Save it as CSV UTF-8 and try again.")
    return [[_cell(v) for v in row] for row in csv.reader(io.StringIO(text))]


def _from_excel(data: bytes) -> list[list[str]]:
    try:
        from openpyxl import load_workbook
    except ImportError as e:
        raise UploadError("Excel files need openpyxl. Run: .venv\\Scripts\\python.exe -m pip install -r "
                          "requirements.txt, or save the file as CSV.") from e
    try:
        book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:  # a damaged or password-protected file
        raise UploadError("The Excel file could not be opened. Save it as .xlsx or CSV and try again.") from e
    sheet = book.worksheets[0]
    rows = [[_cell(v) for v in row] for row in sheet.iter_rows(values_only=True)]
    book.close()
    return rows


def read_table(filename: str, data: bytes) -> tuple[list[str], list[list[str]]]:
    """The header row and the data rows, every value as text. Blank rows are dropped."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        rows = _from_excel(data)
    elif name.endswith(".xls"):
        raise UploadError("Old .xls files are not supported. In Excel choose File > Save As > .xlsx or CSV.")
    elif name.endswith((".csv", ".txt")) or not name:
        rows = _from_csv(data)
    else:
        raise UploadError("Upload a CSV or Excel (.xlsx) file.")
    rows = [r for r in rows if any(v for v in r)]
    if not rows:
        raise UploadError("The file is empty.")
    width = max(len(r) for r in rows)
    headers = [h or f"Column {i + 1}" for i, h in enumerate(rows[0] + [""] * (width - len(rows[0])))]
    body = [r + [""] * (width - len(r)) for r in rows[1:]]
    if len(body) > MAX_ROWS:
        raise UploadError(f"The file has {len(body)} rows. The most one upload can take is {MAX_ROWS:,}; "
                          "split it into smaller files.")
    return headers, body


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def guess_columns(headers: list[str], synonyms: dict[str, tuple[str, ...]]) -> dict[str, int | None]:
    """For each field, the index of the column that looks like it, or None."""
    keys = [_key(h) for h in headers]
    found: dict[str, int | None] = {}
    taken: set[int] = set()
    for exact in (True, False):
        for field, names in synonyms.items():
            if found.get(field) is not None:
                continue
            wanted = [_key(n) for n in names]
            for i, k in enumerate(keys):
                if i in taken or not k:
                    continue
                if (k in wanted) if exact else any(w in k for w in wanted if len(w) > 3):
                    found[field] = i
                    taken.add(i)
                    break
    return {field: found.get(field) for field in synonyms}
