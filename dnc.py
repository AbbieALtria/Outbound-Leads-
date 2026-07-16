"""
DNC / TCPA suppression.

A dedicated suppression list (dnc_numbers) that any lead can be scrubbed against.
Works today with lists YOU control — upload the federal DNC area-code files, your
internal DNC, or a TCPA-litigator list, and every matching lead is marked 'dnc'
(excluded from all dial exports and never re-served).

A real-time provider (DNCScrub, Blacklist Alliance, The DNC Project, TCPA
Litigator List) plugs into `external_scrub()` when you have an account — see the
note there. Until then only the internal list is used.

Compliance note: this is tooling, not legal advice. Keep documented consent for
B2C leads and run a full federal/state/litigator scrub before dialing.
"""

import csv
import io
import os
import re

import db
from dialer_import import normalize_phone


def add_numbers(conn, phones, source="manual", reason=""):
    """Add normalized 10-digit numbers to the suppression list. Returns count added."""
    added = 0
    now = db.now_iso()
    for raw in phones:
        p = normalize_phone(raw)
        if not p:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO dnc_numbers (phone, source, reason, added_at) "
            "VALUES (?, ?, ?, ?)",
            (p, source, reason, now),
        )
        added += cur.rowcount
    return added


def import_csv(conn, text, source="upload"):
    """Import a suppression CSV. Uses a column whose header contains 'phone'/'number',
    else the first column. Returns {added, rows}."""
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return {"added": 0, "rows": 0}
    header = [h.strip().lower() for h in rows[0]]
    col = 0
    data_rows = rows[1:]
    for i, h in enumerate(header):
        if "phone" in h or "number" in h:
            col = i
            break
    else:
        # No recognizable header -> treat every row (incl. first) as a raw number.
        if not any(re.search(r"[a-z]", c) for c in rows[0]):
            data_rows = rows
    phones = [r[col] for r in data_rows if len(r) > col]
    added = add_numbers(conn, phones, source=source, reason="suppression import")
    conn.commit()
    return {"added": added, "rows": len(phones)}


def is_suppressed(conn, phone):
    p = normalize_phone(phone)
    if not p:
        return False
    return conn.execute("SELECT 1 FROM dnc_numbers WHERE phone = ?", (p,)).fetchone() is not None


def suppressed_set(conn):
    return {r["phone"] for r in conn.execute("SELECT phone FROM dnc_numbers")}


def count(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM dnc_numbers").fetchone()["n"]


def scrub_leads(conn, lead_ids=None):
    """Mark any lead whose phone is on the suppression list as status='dnc'.
    If lead_ids is given, only those leads are checked (used right after a pull /
    intake); otherwise every non-dnc lead is scrubbed. Returns count newly blocked."""
    suppressed = suppressed_set(conn)
    if not suppressed:
        return 0
    if lead_ids:
        placeholders = ",".join("?" * len(lead_ids))
        rows = conn.execute(
            f"SELECT id, phone FROM leads WHERE id IN ({placeholders}) AND status != 'dnc'",
            lead_ids,
        ).fetchall()
    else:
        rows = conn.execute("SELECT id, phone FROM leads WHERE status != 'dnc'").fetchall()

    now = db.now_iso()
    blocked = 0
    for row in rows:
        p = normalize_phone(row["phone"])
        if p and p in suppressed:
            conn.execute(
                "UPDATE leads SET status = 'dnc', status_updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            blocked += 1
    conn.commit()
    return blocked


def external_scrub(phones):
    """Real-time provider scrub (federal + state DNC + TCPA litigator).

    Wire in your chosen provider here once you have an account:
      provider = os.environ.get("DNC_PROVIDER")   # e.g. "dncscrub" | "blacklist_alliance"
      key      = os.environ.get("DNC_API_KEY")
    Each provider exposes a batch endpoint that returns which numbers are on a
    DNC / litigator list; return that subset as a set of 10-digit strings.

    Until a provider is configured this returns an empty set (internal list only),
    so nothing here fabricates compliance.
    """
    if not os.environ.get("DNC_API_KEY"):
        return set()
    # Placeholder for the provider-specific HTTP call — intentionally not
    # implemented against a guessed API. Add it when the provider is chosen.
    return set()
