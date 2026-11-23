"""Daily spend caps, per paid provider.

Every paid call the app makes is counted against the provider's row for the
day in provider_usage (one row per provider per day, so several workers share
one count). Each provider has an admin-editable daily cap in Settings; the
defaults are deliberately small, and 0 means "no limit" — a value an admin has
to choose on purpose, never the default.

When a cap is reached the caller stops that provider cleanly: the record in
hand is finished, the rest are skipped, and the run summary says so with
cap_message(). Nothing here raises.
"""
from datetime import date

import db

# key, label, unit, default daily cap
PROVIDERS = [
    ("outscraper", "Outscraper", "records fetched", 500),
    ("apollo", "Apollo", "lookups", 100),
    ("email", "Email verifier", "checks", 200),
    ("claude", "Claude", "calls", 60),
    ("phone", "Phone validator", "numbers", 300),
]
_INFO = {k: (label, unit, default) for k, label, unit, default in PROVIDERS}


def today():
    return str(date.today())


def cap(conn, provider):
    """The daily cap (0 = unlimited). An unset or unreadable value is the default."""
    label, unit, default = _INFO[provider]
    raw = db.get_setting(conn, f"cap_{provider}")
    if raw is None:
        return default
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def set_cap(conn, provider, value):
    db.set_setting(conn, f"cap_{provider}", str(max(0, int(value))))


def used(conn, provider, day=None):
    row = conn.execute(
        "SELECT used FROM provider_usage WHERE provider = ? AND day = ?",
        (provider, day or today())).fetchone()
    return int(row["used"]) if row else 0


def record(conn, provider, n, day=None):
    """Add n units to today's count. Committed at once so a run that dies
    later still leaves its spend on the books."""
    if n <= 0:
        return
    conn.execute(
        "INSERT INTO provider_usage (provider, day, used) VALUES (?, ?, ?) "
        "ON CONFLICT(provider, day) DO UPDATE SET used = provider_usage.used + excluded.used",
        (provider, day or today(), int(n)))
    conn.commit()


def remaining(conn, provider):
    """Units still allowed today, or None when the provider is uncapped."""
    c = cap(conn, provider)
    if c == 0:
        return None
    return max(0, c - used(conn, provider))


def allowance(conn, provider, wanted):
    """How many of `wanted` units may be spent now: `wanted` when uncapped,
    else whatever is left today (possibly 0)."""
    left = remaining(conn, provider)
    return wanted if left is None else min(wanted, left)


def cap_message(conn, provider):
    label, unit, _ = _INFO[provider]
    return (f"{label} daily cap reached: {used(conn, provider)} of "
            f"{cap(conn, provider)} {unit} used")


def usage_report(conn):
    """Today's usage next to each cap, for the Settings page."""
    out = []
    for key, label, unit, default in PROVIDERS:
        c, u = cap(conn, key), used(conn, key)
        out.append({"key": key, "label": label, "unit": unit, "default": default,
                    "cap": c, "used": u,
                    "remaining": None if c == 0 else max(0, c - u),
                    "reached": c != 0 and u >= c})
    return out
