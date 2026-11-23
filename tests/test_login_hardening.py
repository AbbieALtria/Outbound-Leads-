"""Login hardening: SECRET_KEY is required in production and random locally,
session cookies carry the right flags, and failed sign-ins are rate-limited
per username and per IP with a lock that expires on its own."""
from datetime import datetime, timedelta

import pytest
from werkzeug.security import generate_password_hash

import db
import security


# --- secret key ---------------------------------------------------------------------------

def test_secret_key_comes_from_the_env_when_set():
    assert security.secret_key({"SECRET_KEY": " abc123 "}) == "abc123"
    assert security.secret_key({"SECRET_KEY": "k", "RAILWAY_PROJECT_ID": "p"}) == "k"


@pytest.mark.parametrize("env", [
    {"RAILWAY_ENVIRONMENT_NAME": "production"},
    {"RAILWAY_PROJECT_ID": "abc"},
    {"REQUIRE_SECRET_KEY": "1"},
    {"REQUIRE_SECRET_KEY": "true", "APP_PASSWORD": "pw"},   # a password is not a key
])
def test_production_refuses_to_start_without_secret_key(env):
    assert security.is_production(env)
    with pytest.raises(SystemExit) as exc:
        security.secret_key(env)
    assert "SECRET_KEY" in str(exc.value) and "Railway" in str(exc.value)


def test_local_development_gets_a_random_key_not_a_fixed_string():
    env = {"APP_PASSWORD": "pw"}
    assert not security.is_production(env)
    a, b = security.secret_key(env), security.secret_key(env)
    assert len(a) == 64 and a != b
    assert a != "local-dev-secret-key" and a != "pw"


# --- cookies ------------------------------------------------------------------------------

def test_cookie_flags():
    prod = security.cookie_settings(production=True)
    assert prod == {"SESSION_COOKIE_HTTPONLY": True, "SESSION_COOKIE_SAMESITE": "Lax",
                    "SESSION_COOKIE_SECURE": True}
    assert security.cookie_settings(production=False)["SESSION_COOKIE_SECURE"] is False


def test_app_applies_cookie_flags():
    import app as A
    assert A.app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert A.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert A.app.secret_key and A.app.secret_key != "local-dev-secret-key"


# --- attempt limit: the rules ------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "leads.db"
    db.init_db(path)
    c = db.connect(path)
    yield c
    c.close()


T0 = datetime(2026, 11, 23, 9, 0, 0)


def _fail(conn, n, username="admin", ip="1.1.1.1", start=T0):
    for i in range(n):
        security.record_failure(conn, username, ip, now=start + timedelta(seconds=i))


def test_five_failures_lock_the_username_and_the_ip(conn):
    _fail(conn, 4)
    assert security.lockout_seconds(conn, "admin", "1.1.1.1", now=T0 + timedelta(seconds=10)) == 0
    _fail(conn, 1, start=T0 + timedelta(seconds=4))
    at = T0 + timedelta(seconds=10)
    assert security.lockout_seconds(conn, "Admin ", "9.9.9.9", now=at) > 0    # same username, any IP
    assert security.lockout_seconds(conn, "someone", "1.1.1.1", now=at) > 0   # same IP, any username
    assert security.lockout_seconds(conn, "someone", "9.9.9.9", now=at) == 0  # neither


def test_lock_expires_on_its_own_and_is_not_extended_by_rejected_tries(conn):
    _fail(conn, 5)
    last = T0 + timedelta(seconds=4)
    assert security.lockout_seconds(conn, "admin", "1.1.1.1", now=last + timedelta(minutes=14)) > 0
    assert security.lockout_seconds(conn, "admin", "1.1.1.1", now=last + timedelta(minutes=15, seconds=1)) == 0
    # The window has passed: the old rows are gone, a fresh count starts.
    assert conn.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0] == 0


def test_successful_sign_in_clears_failures(conn):
    _fail(conn, 3)
    security.clear_failures(conn, "admin", "1.1.1.1")
    assert conn.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0] == 0


# --- attempt limit: through the login page ------------------------------------------------

@pytest.fixture
def login_user(monkeypatch):
    """A real user in the app database, with auth switched on for the test."""
    import app as A
    import users
    monkeypatch.setattr(users, "auth_enabled", lambda conn=None: True)
    conn = db.connect()
    conn.execute("DELETE FROM login_attempts")
    conn.execute("DELETE FROM users WHERE username = 'locktest'")
    conn.execute(
        "INSERT INTO users (username, password_hash, role, enabled, created_at, created_by, "
        "must_change_password) VALUES ('locktest', ?, 'agent', 1, ?, 'test', 0)",
        (generate_password_hash("right-pass"), db.now_iso()))
    conn.commit()
    conn.close()
    yield A.app.test_client()
    conn = db.connect()
    conn.execute("DELETE FROM login_attempts")
    conn.execute("DELETE FROM users WHERE username = 'locktest'")
    conn.commit()
    conn.close()


def _post(client, password, username="locktest", ip="127.0.0.1"):
    return client.post("/login", data={"username": username, "password": password},
                       environ_base={"REMOTE_ADDR": ip})


def test_login_locks_after_five_failures_then_works_again(login_user, monkeypatch):
    client = login_user
    for _ in range(5):
        r = _post(client, "wrong")
        assert r.status_code == 200 and "Wrong username or password" in r.get_data(as_text=True)
    # Sixth try, even with the right password, is refused for a while.
    r = _post(client, "right-pass")
    assert r.status_code == 429
    assert "Too many failed sign-in attempts" in r.get_data(as_text=True)
    # Another username from another IP is unaffected.
    r = _post(client, "x", username="other", ip="10.0.0.9")
    assert r.status_code == 200
    # The window passes: the correct password signs in normally.
    later = datetime.now() + timedelta(minutes=15, seconds=5)
    monkeypatch.setattr(security, "_now", lambda: later)
    r = _post(client, "right-pass")
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    with client.session_transaction() as s:
        assert s.get("user_id")


def test_ip_lock_covers_any_username_from_that_address(login_user):
    client = login_user
    for i in range(5):
        _post(client, "wrong", username=f"guess{i}", ip="5.5.5.5")
    r = _post(client, "right-pass", username="locktest", ip="5.5.5.5")
    assert r.status_code == 429
    r = _post(client, "right-pass", username="locktest", ip="6.6.6.6")
    assert r.status_code == 302
