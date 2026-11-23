import os
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from library_agent import importer  # noqa: E402
from library_agent.db import connect, init_db  # noqa: E402
from library_agent.notifications import Mailer  # noqa: E402
from library_agent.tools import ToolContext  # noqa: E402

FRIDAY = date(2026, 10, 30)
MONDAY = date(2026, 11, 2)


# Tests use a fresh SQLite file each time. Set TEST_DATABASE_URL to a
# throwaway PostgreSQL database to run them all against PostgreSQL instead
# (every table in it is dropped before each test).
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.fixture
def db_target(tmp_path):
    if not TEST_DATABASE_URL:
        return str(tmp_path / "test.db")
    c = connect(TEST_DATABASE_URL)
    c.execute("DROP SCHEMA public CASCADE")
    c.execute("CREATE SCHEMA public")
    c.commit()
    c.close()
    return TEST_DATABASE_URL


@pytest.fixture
def conn(db_target):
    c = connect(db_target)
    init_db(c)
    importer.import_books(c, str(ROOT / "sample_data" / "books.csv"))
    importer.import_students(c, str(ROOT / "sample_data" / "students.csv"))
    yield c
    c.close()


@pytest.fixture
def ctx(conn):
    return ToolContext(conn, actor="Member One", session_id=None, today=FRIDAY)


@pytest.fixture
def mailer():
    return Mailer()  # SMTP_HOST is unset, so messages are only recorded


# ---------------------------------------------------------------- fake Claude client

def text_block(text):
    return {"type": "text", "text": text}


def tool_block(name, args, id_="t1"):
    return {"type": "tool_use", "id": id_, "name": name, "input": args}


class FakeClient:
    """Plays back scripted responses and records every request, so agent
    tests run without an API key."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        import copy
        self.requests.append(copy.deepcopy(kwargs))
        content = self.script.pop(0)
        stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
        return SimpleNamespace(content=content, stop_reason=stop)
