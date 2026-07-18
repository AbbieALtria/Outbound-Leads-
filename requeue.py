"""
Requeue engine.

Takes a day's VICIdial dispositions and decides, per lead, what happens:
  - retry codes (YPVM/YPCBCK/YPNA/INCALL/DROP) with called_count < 3  -> active
    (stays in the ready-to-dial pool, so the existing VICIdial export re-serves it)
  - retry codes with called_count >= 3 -> exhausted (dropped from the pool)
  - YPNI (not interested) -> suppressed_leads for 90 days (a temporary DNC),
    excluded from dialing AND from being re-served by future exports

attempt_count mirrors VICIdial's called_count — we don't keep our own counter.
Decisions are written to requeue_leads / suppressed_leads so the export stays
fast and the dashboard has state to show.
"""

from datetime import datetime, timedelta, timezone

import db
import dnc
import ops_dispositions
from dialer_import import normalize_phone

CAP = 3                      # total dials (called_count) allowed before exhausted
CALLBACK_CAP = 5             # callbacks get more room — the customer asked us to call
COOLDOWN_DAYS = 90          # YPNI suppression window


def _cap_for(status):
    """Dial cap for a disposition — callbacks get the higher CALLBACK_CAP."""
    return CALLBACK_CAP if status in ops_dispositions.CALLBACK_CODES else CAP

try:
    from zoneinfo import ZoneInfo
    EST = ZoneInfo("America/New_York")     # proper DST (tzdata present on Railway)
except Exception:
    EST = timezone(timedelta(hours=-5))    # fallback if tzdata missing (local dev)


def today_est():
    return datetime.now(EST).strftime("%Y-%m-%d")


def process(conn, dispositions, day=None, dry_run=False):
    """Apply a list of {phone, status, called_count} dispositions for the EST
    day `day` (the redial-batch key). Returns a summary. Idempotent: re-running
    the same day yields the same state."""
    day = day or today_est()
    lead_by_phone = {}
    for row in conn.execute("SELECT id, phone FROM leads"):
        p = normalize_phone(row["phone"])
        if p:
            lead_by_phone[p] = row["id"]

    summary = {"processed": 0, "requeued": 0, "exhausted": 0,
               "suppressed": 0, "dnc": 0, "unmatched": 0}
    now = db.now_iso()
    cooldown = (datetime.now(EST) + timedelta(days=COOLDOWN_DAYS)).isoformat(timespec="seconds")

    for d in dispositions:
        summary["processed"] += 1
        phone = normalize_phone(d.get("phone"))
        status = (d.get("status") or "").strip().upper()
        try:
            count = int(d.get("called_count") or 0)
        except (TypeError, ValueError):
            count = 0
        if not phone:
            continue

        if status in ops_dispositions.DNC_CODES:           # YPDNC - hard do-not-call
            # A do-not-call is legal and permanent, so unlike YPNI it's global:
            # add the number to the DNC list regardless of whether it's our lead.
            # If it IS ours, flag the lead so the export drops it right away.
            summary["dnc"] += 1
            if not dry_run:
                dnc.add_numbers(conn, [phone], source="vicidial",
                                reason=f"{status} - do not call")
                lead_id = lead_by_phone.get(phone)
                if lead_id:
                    conn.execute(
                        "UPDATE leads SET status = 'dnc', status_updated_at = ? WHERE id = ?",
                        (now, lead_id),
                    )
            continue

        if status in ops_dispositions.SUPPRESS_CODES:      # YPNI
            # Only suppress numbers WE generated. Client-owned leads (not in our
            # app) are the client's campaign — we're just the dialer, so we don't
            # impose a suppression on their numbers. DNC stays global elsewhere.
            if phone not in lead_by_phone:
                summary["unmatched"] += 1
                continue
            summary["suppressed"] += 1
            if not dry_run:
                conn.execute(
                    "INSERT OR IGNORE INTO suppressed_leads (phone, reason, cooldown_until, added_at) "
                    "VALUES (?, ?, ?, ?)",
                    (phone, f"{status} - not interested", cooldown, now),
                )
            continue

        if status not in ops_dispositions.RETRY_CODES:
            continue
        lead_id = lead_by_phone.get(phone)
        if not lead_id:
            summary["unmatched"] += 1          # dialed number we didn't generate
            continue

        state = "exhausted" if count >= _cap_for(status) else "active"
        campaign = (d.get("campaign") or "").strip()
        summary["exhausted" if state == "exhausted" else "requeued"] += 1
        if not dry_run:
            # Keep a manual 'excluded' decision sticky; otherwise set active/exhausted.
            conn.execute(
                "INSERT INTO requeue_leads (lead_id, last_disposition, attempt_count, state, campaign, batch_date, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(lead_id) DO UPDATE SET last_disposition = excluded.last_disposition, "
                "attempt_count = excluded.attempt_count, campaign = excluded.campaign, "
                "batch_date = excluded.batch_date, updated_at = excluded.updated_at, "
                "state = CASE WHEN requeue_leads.state = 'excluded' THEN 'excluded' ELSE excluded.state END",
                (lead_id, status, count, state, campaign, day, now),
            )

    if not dry_run:
        conn.commit()
    return summary


def run(conn, day=None, campaign=None, dry_run=False):
    """Fetch a day's dispositions from VICIdial (optionally one campaign) and
    process them."""
    if not ops_dispositions.enabled():
        return {"error": "VICIdial connection not configured (set OPS_DB_* env vars)."}
    day = day or today_est()
    try:
        disps = ops_dispositions.fetch_dispositions(day, campaign=campaign or None)
    except Exception as e:
        return {"error": f"Could not read VICIdial dispositions: {e}"}
    summary = process(conn, disps, day=day, dry_run=dry_run)
    summary["day"] = day
    summary["campaign"] = campaign or "all"
    summary["dry_run"] = dry_run
    return summary


# --- exclusion sets used by the dial export ---

def blocked_lead_ids(conn):
    """Lead ids that must NOT be dialed: exhausted or manually excluded."""
    return {r["lead_id"] for r in conn.execute(
        "SELECT lead_id FROM requeue_leads WHERE state IN ('exhausted', 'excluded')")}


def suppressed_phones(conn):
    """Normalized phones still within their YPNI cooldown."""
    return {r["phone"] for r in conn.execute(
        "SELECT phone FROM suppressed_leads WHERE cooldown_until > ?", (db.now_iso(),))}


def dashboard_rows(conn, campaign=None):
    sql = (
        "SELECT r.id, r.lead_id, r.last_disposition, r.attempt_count, r.state, r.campaign, r.updated_at, "
        "l.business_name, l.phone, l.city, l.state AS lead_state "
        "FROM requeue_leads r JOIN leads l ON l.id = r.lead_id "
    )
    params = []
    if campaign:
        sql += "WHERE r.campaign = ? "
        params.append(campaign)
    sql += "ORDER BY CASE r.state WHEN 'active' THEN 0 WHEN 'exhausted' THEN 1 ELSE 2 END, r.updated_at DESC"
    rows = []
    for r in conn.execute(sql, params):
        d = dict(r)
        d["cap"] = _cap_for((d.get("last_disposition") or "").strip().upper())
        rows.append(d)
    return rows


def campaigns_in_requeue(conn):
    return [r["campaign"] for r in conn.execute(
        "SELECT DISTINCT campaign FROM requeue_leads WHERE campaign != '' ORDER BY campaign")]


def segments(conn):
    """The active redial pool grouped into upload batches by campaign source +
    industry + batch date (DATE of updated_at — the day the lead entered/refreshed
    the requeue). Each row is one VICIdial file the admin downloads and loads into
    the matching list. Returns [{campaign, industry, batch_date, n}, ...]."""
    sql = (
        "SELECT r.campaign AS campaign, "
        "       COALESCE(NULLIF(TRIM(l.industry), ''), NULLIF(TRIM(l.category), ''), '') AS industry, "
        "       r.batch_date AS batch_date, COUNT(*) AS n "
        "FROM requeue_leads r JOIN leads l ON l.id = r.lead_id "
        "WHERE r.state = 'active' "
        "GROUP BY r.campaign, industry, r.batch_date "
        "ORDER BY r.batch_date DESC, r.campaign, industry"
    )
    return [dict(x) for x in conn.execute(sql)]
