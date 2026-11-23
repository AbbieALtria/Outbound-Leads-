"""Login hardening: the session secret, cookie flags, and a sign-in attempt limit.

Secret key: in production (Railway's own env vars, or REQUIRE_SECRET_KEY=1)
the app refuses to start without SECRET_KEY, with a message that says what to
set. Locally a random key is generated per process — never a fixed string in
the source — so sessions simply end when the dev server restarts.

Attempt limit: failed sign-ins are stored in the database (login_attempts), so
every gunicorn worker sees the same count. After LOCKOUT_ATTEMPTS failures for
one username OR one IP inside LOCKOUT_MINUTES, further attempts for that
username/IP are rejected until LOCKOUT_MINUTES after the last failure. A
rejected attempt is not recorded, so the lock always expires on its own and
nobody — the founding admin included — can be kept out for good.
"""
import os
import secrets
from datetime import datetime, timedelta

LOCKOUT_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

# Any of these set means we are running on Railway.
_PRODUCTION_MARKERS = ("RAILWAY_ENVIRONMENT_NAME", "RAILWAY_ENVIRONMENT",
                       "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID")


def is_production(env=None):
    env = os.environ if env is None else env
    if (env.get("REQUIRE_SECRET_KEY") or "").strip().lower() in ("1", "true", "yes"):
        return True
    return any((env.get(k) or "").strip() for k in _PRODUCTION_MARKERS)


def secret_key(env=None):
    """The Flask secret key. Raises SystemExit (a clear start-up error) in
    production when SECRET_KEY is missing; a fresh random key locally."""
    env = os.environ if env is None else env
    key = (env.get("SECRET_KEY") or "").strip()
    if key:
        return key
    if is_production(env):
        raise SystemExit(
            "SECRET_KEY is not set. The app refuses to start in production without it, "
            "because the session cookie would be signed with a guessable value. Set "
            "SECRET_KEY to a long random string in Railway -> Variables (for example the "
            "output of: python -c \"import secrets; print(secrets.token_hex(32))\") and "
            "redeploy.")
    return secrets.token_hex(32)


def cookie_settings(production=None):
    """Flask session-cookie config: HttpOnly and SameSite=Lax always; Secure
    (HTTPS only) in production, where Railway terminates TLS in front of us."""
    production = is_production() if production is None else production
    return {"SESSION_COOKIE_HTTPONLY": True,
            "SESSION_COOKIE_SAMESITE": "Lax",
            "SESSION_COOKIE_SECURE": bool(production)}


def client_ip(request):
    return (request.remote_addr or "").strip()


def _now():
    return datetime.now()


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _key(username):
    return (username or "").strip().lower()


def lockout_seconds(conn, username, ip, now=None):
    """Seconds until this username or IP may try to sign in again; 0 = now."""
    now = now or _now()
    window_start = _iso(now - timedelta(minutes=LOCKOUT_MINUTES))
    # Old rows are useless and only grow; drop them as we go.
    conn.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (window_start,))
    wait = 0
    for column, value in (("username", _key(username)), ("ip", (ip or "").strip())):
        if not value:
            continue
        row = conn.execute(
            f"SELECT COUNT(*) AS n, MAX(attempted_at) AS last FROM login_attempts "
            f"WHERE {column} = ? AND attempted_at >= ?", (value, window_start)).fetchone()
        if row["n"] >= LOCKOUT_ATTEMPTS and row["last"]:
            until = datetime.fromisoformat(row["last"]) + timedelta(minutes=LOCKOUT_MINUTES)
            wait = max(wait, int((until - now).total_seconds()))
    conn.commit()
    return max(0, wait)


def record_failure(conn, username, ip, now=None):
    conn.execute(
        "INSERT INTO login_attempts (username, ip, attempted_at) VALUES (?, ?, ?)",
        (_key(username), (ip or "").strip(), _iso(now or _now())))
    conn.commit()


def clear_failures(conn, username, ip=""):
    """A successful sign-in wipes the slate for that username and that IP."""
    conn.execute("DELETE FROM login_attempts WHERE username = ?", (_key(username),))
    if (ip or "").strip():
        conn.execute("DELETE FROM login_attempts WHERE ip = ?", (ip.strip(),))
    conn.commit()
