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
import sqlite3

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
    existing APP_PASSWORD) if auth is enabled and no admin exists yet."""
    password = _bootstrap_password()
    if not password:
        return
    if conn.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone():
        return
    username = (os.environ.get("ADMIN_USER", "admin").strip() or "admin")
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
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, generate_password_hash(password), role, db.now_iso(), created_by),
        )
        conn.commit()
        return True, None
    except sqlite3.IntegrityError:
        return False, f"User '{username}' already exists."


def set_password(conn, user_id, password):
    if not password or len(password) < 6:
        return False
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                 (generate_password_hash(password), user_id))
    conn.commit()
    return True


def toggle_enabled(conn, user_id):
    conn.execute("UPDATE users SET enabled = 1 - enabled WHERE id = ?", (user_id,))
    conn.commit()


def delete_user(conn, user_id):
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()


def list_users(conn):
    return conn.execute(
        "SELECT id, username, role, enabled, created_at, created_by FROM users "
        "ORDER BY role, username"
    ).fetchall()


def get(conn, user_id):
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def admin_count(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND enabled = 1").fetchone()["n"]
