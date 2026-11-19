"""Sign-in with a member's own password (AUTH_MODE=password)."""
import pytest
from fastapi.testclient import TestClient

from library_agent import passwords
from library_agent.__main__ import main
from library_agent.config import settings
from library_agent.db import connect


@pytest.fixture
def setup(tmp_path, monkeypatch):
    members = tmp_path / "members.csv"
    members.write_text("name,email,role\n"
                       "Lib Rarian,lib@s.example,librarian\n"
                       "Com Mittee,lib@s.example,committee\n")
    db = str(tmp_path / "web.db")
    monkeypatch.setattr(settings, "members_file", str(members))
    monkeypatch.setattr(settings, "auth_mode", "password")
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(passwords, "ITERATIONS", 1000)  # keep the tests fast
    from library_agent.web.app import create_app
    return TestClient(create_app(db, ai_enabled=False)), db


def _create(web, member, pw):
    r = web.post("/login/password", data={"member": member, "password": ""})
    assert "Create your password" in r.text or "Create the library password" in r.text
    return web.post("/login/setup", data={"new_password": pw, "confirm_password": pw},
                    follow_redirects=False)


def test_first_sign_in_creates_password_then_it_is_required(setup):
    web, db = setup
    assert web.get("/desk", follow_redirects=False).status_code == 303
    assert "first time" in web.get("/login").text
    assert _create(web, "0", "river-mango-12").status_code == 303
    assert "Your password is saved" in web.get("/desk?msg=Your password is saved").text
    # Stored as a hash, not the password itself.
    row = connect(db).execute("SELECT password_hash FROM member_passwords").fetchone()
    assert "river-mango-12" not in row[0]

    web.get("/logout")
    assert "wrong" in web.post("/login/password", data={"member": "0", "password": "nope"}).text
    assert web.get("/desk", follow_redirects=False).status_code == 303
    r = web.post("/login/password", data={"member": "0", "password": "river-mango-12"},
                 follow_redirects=False)
    assert r.status_code == 303 and web.get("/desk").status_code == 200


def test_new_password_must_match_and_be_long_enough(setup):
    web, _ = setup
    web.post("/login/password", data={"member": "0"})
    assert "do not match" in web.post("/login/setup", data={"new_password": "abcdefgh",
                                                           "confirm_password": "abcdefgX"}).text
    assert "at least 8" in web.post("/login/setup", data={"new_password": "short",
                                                          "confirm_password": "short"}).text
    assert web.get("/desk", follow_redirects=False).status_code == 303


def test_setup_needs_the_first_step(setup):
    web, _ = setup
    r = web.post("/login/setup", data={"new_password": "abcdefgh", "confirm_password": "abcdefgh"},
                 follow_redirects=False)
    assert r.headers["location"] == "/login"


def test_shared_email_members_get_separate_passwords(setup):
    web, _ = setup
    _create(web, "0", "librarian-pass")
    web.get("/logout")
    assert "Create your password" in web.post("/login/password", data={"member": "1"}).text


def test_change_password_needs_current_password(setup):
    web, _ = setup
    _create(web, "0", "first-password")
    assert "Current password" in web.get("/account/password").text
    r = web.post("/account/password", data={"current_password": "wrong-one",
                                            "new_password": "second-password",
                                            "confirm_password": "second-password"})
    assert "current password is wrong" in r.text
    r = web.post("/account/password", data={"current_password": "first-password",
                                            "new_password": "second-password",
                                            "confirm_password": "second-passwordX"})
    assert "do not match" in r.text
    r = web.post("/account/password", data={"current_password": "first-password",
                                            "new_password": "second-password",
                                            "confirm_password": "second-password"},
                 follow_redirects=False)
    assert r.status_code == 303 and "changed" in r.headers["location"]
    web.get("/logout")
    assert "wrong" in web.post("/login/password", data={"member": "0", "password": "first-password"}).text
    r = web.post("/login/password", data={"member": "0", "password": "second-password"},
                 follow_redirects=False)
    assert r.status_code == 303


def test_too_many_wrong_passwords_must_wait(setup):
    web, _ = setup
    _create(web, "0", "right-password")
    web.get("/logout")
    for _ in range(passwords.Attempts.MAX_TRIES):
        web.post("/login/password", data={"member": "0", "password": "bad-guess"})
    r = web.post("/login/password", data={"member": "0", "password": "right-password"})
    assert "Please wait" in r.text


def test_reset_password_command(setup, capsys):
    web, _ = setup
    _create(web, "0", "forgotten-one")
    main(["reset-password", "lib rarian"])
    assert "Password cleared" in capsys.readouterr().out
    web.get("/logout")
    assert "Create your password" in web.post("/login/password", data={"member": "0"}).text


def test_other_modes_have_no_password_pages(setup, monkeypatch):
    web, _ = setup
    monkeypatch.setattr(settings, "auth_mode", "dev")
    assert web.post("/login/password", data={"member": "0"}).status_code == 404


def test_attempts_unlock_after_wait():
    t = [0.0]
    a = passwords.Attempts(clock=lambda: t[0])
    for _ in range(a.MAX_TRIES):
        a.failed("k")
    assert a.locked("k") > 0
    t[0] += a.WAIT + 1
    assert a.locked("k") == 0


# ------------------------------------------------------------ one shared password

@pytest.fixture
def shared(setup, monkeypatch):
    monkeypatch.setattr(settings, "shared_password", True)
    return setup


def test_shared_password_is_made_once_and_works_for_everyone(shared):
    web, _ = shared
    r = web.post("/login/password", data={"member": "0"})
    assert "Everyone on the committee will sign in with" in r.text
    web.post("/login/setup", data={"new_password": "one-for-all", "confirm_password": "one-for-all"})
    web.get("/logout")
    # The other member does not make a new one; the same password signs them in.
    r = web.post("/login/password", data={"member": "1", "password": "one-for-all"},
                 follow_redirects=False)
    assert r.status_code == 303 and web.get("/desk").status_code == 200


def test_changing_shared_password_changes_it_for_everyone(shared):
    web, _ = shared
    _create(web, "0", "old-shared-pw")
    assert "changing it changes it for" in web.get("/account/password").text
    r = web.post("/account/password", data={"current_password": "old-shared-pw",
                                            "new_password": "new-shared-pw",
                                            "confirm_password": "new-shared-pw"},
                 follow_redirects=False)
    assert "for%20everyone" in r.headers["location"]
    web.get("/logout")
    assert "wrong" in web.post("/login/password", data={"member": "1", "password": "old-shared-pw"}).text
    r = web.post("/login/password", data={"member": "1", "password": "new-shared-pw"},
                 follow_redirects=False)
    assert r.status_code == 303


def test_switching_to_shared_keeps_the_existing_password(setup, monkeypatch):
    web, _ = setup
    _create(web, "0", "gunas-password")   # made while each member had their own
    web.get("/logout")
    monkeypatch.setattr(settings, "shared_password", True)
    r = web.post("/login/password", data={"member": "1", "password": "gunas-password"},
                 follow_redirects=False)
    assert r.status_code == 303


def test_reset_with_no_name_clears_the_shared_password(shared, capsys):
    web, _ = shared
    _create(web, "0", "shared-to-forget")
    main(["reset-password"])
    assert "All passwords cleared" in capsys.readouterr().out
    web.get("/logout")
    assert "Create the library password" in web.post("/login/password", data={"member": "1"}).text
