"""
User accounts + authentication.

Session-based login backed by the users table (passwords hashed with werkzeug).
One admin is seeded from the environment on first run; the admin creates other
users in the UI. Roles: 'admin' (can manage users) and 'agent' (can use the app).

Auth is ON when a bootstrap password is configured (ADMIN_PASSWORD, or the
existing APP_PASSWORD) OR any user exists — i.e. on the hosted deploy. Locally,
with none of those set, the app stays open (no login) for convenience.
"""

import os
from datetime import date, timedelta

from werkzeug.security import check_password_hash, generate_password_hash

import db

ROLES = ("admin", "agent")


def _bootstrap_password():
    return os.environ.get("ADMIN_PASSWORD") or os.environ.get("APP_PASSWORD") or ""


def auth_enabled(conn=None):
    """Login is required exactly when a bootstrap password is configured in the
    environment (ADMIN_PASSWORD or APP_PASSWORD) — i.e. on the hosted deploy.
    Locally, with neither set, the app stays open. Driven only by the env var so
    behaviour is predictable and a stray seeded user can't lock local dev."""
    return bool(_bootstrap_password())


def ensure_admin(conn):
    """Seed the admin account from ADMIN_USER/ADMIN_PASSWORD (falls back to the
    existing APP_PASSWORD) if auth is enabled and no admin exists yet.

    Recovery: set env ADMIN_RESET=1 (with ADMIN_USER + ADMIN_PASSWORD) to FORCE
    that account to the given password on the next deploy — the way back in when
    the admin password is lost. Remove ADMIN_RESET afterwards."""
    password = _bootstrap_password()
    if not password:
        return
    username = (os.environ.get("ADMIN_USER", "admin").strip() or "admin")
    reset = os.environ.get("ADMIN_RESET", "").strip().lower() in ("1", "true", "yes")
    if conn.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone() and not reset:
        return
    if reset:
        row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET password_hash = ?, role = 'admin', enabled = 1, "
                "must_change_password = 0 WHERE id = ?",
                (generate_password_hash(password), row["id"]))
        else:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, enabled, created_at, "
                "created_by, must_change_password) VALUES (?, ?, 'admin', 1, ?, 'reset', 0)",
                (username, generate_password_hash(password), db.now_iso()))
        conn.commit()
        return
    conn.execute(
        "INSERT OR IGNORE INTO users (username, password_hash, role, created_at, created_by) "
        "VALUES (?, ?, 'admin', ?, 'system')",
        (username, generate_password_hash(password), db.now_iso()),
    )
    conn.commit()


def authenticate(conn, username, password):
    row = conn.execute(
        "SELECT * FROM users WHERE username = ? AND enabled = 1", ((username or "").strip(),)
    ).fetchone()
    if row and check_password_hash(row["password_hash"], password or ""):
        return row
    return None


def create_user(conn, username, password, role, created_by):
    username = (username or "").strip()
    if not username or not password:
        return False, "Username and password are both required."
    if len(password) < 6:
        return False, "Password must be at least 6 characters."
    role = role if role in ROLES else "agent"
    try:
        # New users must set their own password at first login.
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at, created_by, "
            "must_change_password) VALUES (?, ?, ?, ?, ?, 1)",
            (username, generate_password_hash(password), role, db.now_iso(), created_by),
        )
        conn.commit()
        return True, conn.execute(
            "SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]
    except db.IntegrityError:
        return False, f"User '{username}' already exists."


def set_password(conn, user_id, password, require_change=True):
    """Set a user's password. require_change=True flags them to change it at next
    login (used when an ADMIN resets it to a temporary password)."""
    if not password or len(password) < 6:
        return False
    conn.execute(
        "UPDATE users SET password_hash = ?, must_change_password = ? WHERE id = ?",
        (generate_password_hash(password), 1 if require_change else 0, user_id))
    conn.commit()
    return True


def change_own_password(conn, user_id, password):
    """A user setting their OWN password — clears the must-change flag."""
    return set_password(conn, user_id, password, require_change=False)


def toggle_enabled(conn, user_id):
    conn.execute("UPDATE users SET enabled = 1 - enabled WHERE id = ?", (user_id,))
    conn.commit()


def delete_user(conn, user_id):
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()


def list_users(conn):
    return conn.execute(
        "SELECT id, username, role, enabled, created_at, created_by, "
        "lead_limit_total, lead_limit_daily, allowed_campaigns FROM users "
        "ORDER BY role, username"
    ).fetchall()


def set_limits(conn, user_id, total, daily, allowed_campaigns,
               period=0, period_days=15, credit_limit=0):
    """Admin-set per-user quotas. 0 = unlimited for every cap. allowed_campaigns:
    list/iterable of campaign ids ('' or empty = all campaigns).

    `period`/`period_days` are the rolling lead allowance (e.g. 400 in 15 days)
    and `credit_limit` the Apollo spend allowed in that same window."""
    def _int(v, low=0):
        try:
            return max(low, int(v))
        except (TypeError, ValueError):
            return low
    ids = ",".join(str(int(c)) for c in allowed_campaigns if str(c).strip().isdigit())
    conn.execute(
        "UPDATE users SET lead_limit_total = ?, lead_limit_daily = ?, "
        "lead_limit_period = ?, lead_limit_period_days = ?, credit_limit_period = ?, "
        "allowed_campaigns = ? WHERE id = ?",
        (_int(total), _int(daily), _int(period), _int(period_days, 1) or 15,
         _int(credit_limit), ids, user_id),
    )
    conn.commit()


def allowed_campaign_ids(user_row):
    """Set of campaign ids this user may pull for. Empty set = ALL (no restriction)."""
    if not user_row:
        return set()
    raw = (user_row["allowed_campaigns"] or "").strip() if "allowed_campaigns" in user_row.keys() else ""
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


def usage(conn, user_id):
    """Leads this user has generated: (total, today). Counts leads from the user's
    own pulls (pull_runs.user_id), today by pulled_date."""
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM leads l JOIN pull_runs r ON l.run_id = r.id "
        "WHERE r.user_id = ?", (user_id,)).fetchone()["n"]
    today = conn.execute(
        "SELECT COUNT(*) AS n FROM leads l JOIN pull_runs r ON l.run_id = r.id "
        "WHERE r.user_id = ? AND l.pulled_date = ?",
        (user_id, str(date.today()))).fetchone()["n"]
    return total, today


def owner_id(conn):
    """The founding admin account, which other admins may not delete or reset.

    An admin can create more admins, and without this any of them could lock the
    owner out of their own system — or quietly take it over by resetting the
    owner's password. Identified as the earliest admin (the seeded one), so it
    needs no extra column and cannot drift.
    """
    row = conn.execute(
        "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1").fetchone()
    return row["id"] if row else None


def is_owner(conn, user_id):
    return user_id is not None and user_id == owner_id(conn)


def period_usage(conn, user_id, days):
    """Leads this user generated in the last `days` days (a rolling window).

    Rolling rather than calendar: a fortnightly allowance that resets on a fixed
    date lets someone spend the whole budget on the last day and the whole next
    one on the first, which is exactly the burst a budget is meant to prevent.
    """
    if not days or days < 1:
        return 0
    since = str(date.today() - timedelta(days=int(days) - 1))
    return conn.execute(
        "SELECT COUNT(*) AS n FROM leads l JOIN pull_runs r ON l.run_id = r.id "
        "WHERE r.user_id = ? AND l.pulled_date >= ?", (user_id, since)).fetchone()["n"]


def credits_used(conn, user_id, days):
    """Apollo credits this user spent in the last `days` days."""
    if not days or days < 1:
        return 0
    since = str(date.today() - timedelta(days=int(days) - 1))
    row = conn.execute(
        "SELECT COALESCE(SUM(credits), 0) AS n FROM credit_usage "
        "WHERE user_id = ? AND created_at >= ?", (user_id, since)).fetchone()
    return int(row["n"] or 0)


def record_credits(conn, user_id, credits, note=""):
    """Log an Apollo spend against a user so the budget can be enforced."""
    if not credits:
        return
    conn.execute(
        "INSERT INTO credit_usage (user_id, credits, note, created_at) VALUES (?, ?, ?, ?)",
        (user_id, int(credits), note[:200], db.now_iso()))
    conn.commit()


def credit_budget_left(conn, user):
    """Credits this user may still spend, or None when unlimited."""
    if not user or user["role"] == "admin":
        return None
    limit = _col(user, "credit_limit_period", 0)
    if not limit:
        return None
    days = _col(user, "lead_limit_period_days", 15) or 15
    return max(0, limit - credits_used(conn, user["id"], days))


def _col(row, name, default=None):
    """Read a column that may not exist on an older row."""
    try:
        return row[name] if name in row.keys() else default
    except (IndexError, KeyError, TypeError):
        return default


def reset_usage(conn, user_id):
    """Zero a user's counted usage by detaching their pulls' user stamp (their
    leads stay; they just no longer count against the quota). Admin action."""
    conn.execute("UPDATE pull_runs SET user_id = NULL WHERE user_id = ?", (user_id,))
    conn.commit()


def get(conn, user_id):
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def admin_count(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND enabled = 1").fetchone()["n"]
