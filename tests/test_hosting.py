"""Pieces needed to run on Vercel: members from a setting, the cron job and a
sign-in lock kept in the database."""
from fastapi.testclient import TestClient

from library_agent import passwords
from library_agent.config import settings
from library_agent.db import connect, init_db
from library_agent.web.app import create_app, member_list


def test_members_can_come_from_a_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "members_csv", "name,email,role\nAsha,a@s.example,librarian")
    assert member_list(str(tmp_path / "missing.csv")) == [
        {"name": "Asha", "email": "a@s.example", "role": "librarian"}]


def test_cron_needs_the_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cron_secret", "s3cret")
    web = TestClient(create_app(str(tmp_path / "c.db"), ai_enabled=False))
    assert web.get("/cron/daily-job").status_code == 401
    assert web.get("/cron/daily-job", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = web.get("/cron/daily-job", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200 and isinstance(r.json(), dict)


def test_cron_is_off_without_a_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cron_secret", "")
    web = TestClient(create_app(str(tmp_path / "c.db"), ai_enabled=False))
    assert web.get("/cron/daily-job", headers={"Authorization": "Bearer "}).status_code == 401


def test_lock_is_shared_through_the_database(tmp_path):
    db = str(tmp_path / "a.db")
    c = connect(db); init_db(c); c.close()
    t = [1000.0]
    one = passwords.Attempts(clock=lambda: t[0], connect=lambda: connect(db))
    two = passwords.Attempts(clock=lambda: t[0], connect=lambda: connect(db))  # another server
    for _ in range(one.MAX_TRIES):
        one.failed("k")
    assert two.locked("k") > 0
    t[0] += one.WAIT + 1
    assert two.locked("k") == 0
    one.failed("k"); one.succeeded("k")
    assert two.locked("k") == 0
