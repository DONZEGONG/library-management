"""Sign-in with a one-time code emailed to a committee address (AUTH_MODE=code)."""
import re

import pytest
from fastapi.testclient import TestClient

from library_agent.config import settings
from library_agent.notifications import Mailer
from library_agent.web.app import LoginCodes


class SentMailer(Mailer):
    configured = True

    def send(self, to, subject, body):
        self.outbox.append({"to": to, "subject": subject, "body": body})


@pytest.fixture
def setup(tmp_path, monkeypatch):
    members = tmp_path / "members.csv"
    members.write_text("name,email,role\nLib Rarian,lib@s.example,librarian\n")
    monkeypatch.setattr(settings, "members_file", str(members))
    monkeypatch.setattr(settings, "auth_mode", "code")
    from library_agent.web.app import create_app
    mailer = SentMailer()
    app = create_app(str(tmp_path / "web.db"), mailer=mailer, ai_enabled=False)
    return TestClient(app), mailer


def _code(mailer):
    return re.search(r"\b(\d{6})\b", mailer.outbox[-1]["body"])[1]


def test_code_sign_in(setup):
    web, mailer = setup
    assert web.get("/desk", follow_redirects=False).status_code == 303
    r = web.post("/login/code", data={"email": " LIB@s.example "})
    assert "We emailed a 6-digit code" in r.text and mailer.outbox[-1]["to"] == "lib@s.example"
    assert "is wrong" in web.post("/login/verify", data={"email": "lib@s.example", "code": "x"}).text
    r = web.post("/login/verify", data={"email": "lib@s.example", "code": _code(mailer)},
                 follow_redirects=False)
    assert r.status_code == 303
    assert web.get("/desk").status_code == 200


def test_code_works_once(setup):
    web, mailer = setup
    web.post("/login/code", data={"email": "lib@s.example"})
    code = _code(mailer)
    web.post("/login/verify", data={"email": "lib@s.example", "code": code})
    web.get("/logout")
    assert "is wrong" in web.post("/login/verify", data={"email": "lib@s.example", "code": code}).text


def test_unknown_email_gets_no_code_and_name_picking_is_off(setup):
    web, mailer = setup
    r = web.post("/login/code", data={"email": "someone@s.example"})
    assert "not on the library committee list" in r.text and mailer.outbox == []
    assert web.post("/login", data={"email": "lib@s.example"}).status_code == 404
    assert web.get("/desk", follow_redirects=False).status_code == 303


def test_no_email_setup_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "code")
    monkeypatch.setattr(settings, "smtp_host", "")
    from library_agent.web.app import create_app
    web = TestClient(create_app(str(tmp_path / "w.db"), ai_enabled=False))
    assert "not set up on this computer" in web.get("/login").text


def test_codes_expire_resend_limit_and_tries():
    t = [0.0]
    codes = LoginCodes(clock=lambda: t[0])
    code = codes.issue("a@s")
    assert codes.issue("a@s") is None          # too soon to resend
    t[0] += LoginCodes.LIFETIME + 1
    assert not codes.check("a@s", code)        # expired
    code = codes.issue("a@s")
    for _ in range(LoginCodes.MAX_TRIES):
        codes.check("a@s", "wrong")
    assert not codes.check("a@s", code)        # cancelled after too many tries


def test_shared_email_asks_who_is_signing_in(setup, tmp_path):
    web, mailer = setup
    (tmp_path / "members.csv").write_text(
        "name,email,role\nLib Rarian,lib@s.example,librarian\nCom Mittee,lib@s.example,committee\n")
    web.post("/login/code", data={"email": "lib@s.example"})
    assert "Lib Rarian / Com Mittee" in mailer.outbox[-1]["body"]
    r = web.post("/login/verify", data={"email": "lib@s.example", "code": _code(mailer)})
    assert "choose who is signing in" in r.text and "Com Mittee (committee)" in r.text
    assert web.get("/desk", follow_redirects=False).status_code == 303   # not signed in yet
    web.post("/login/choose", data={"name": "Com Mittee"})
    assert web.get("/librarian").status_code == 403                      # committee, not librarian
    web.get("/logout")
    # choosing a name without a correct code first does nothing
    web.post("/login/choose", data={"name": "Lib Rarian"})
    assert web.get("/desk", follow_redirects=False).status_code == 303
