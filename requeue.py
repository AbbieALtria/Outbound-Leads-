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

import re
from datetime import datetime, timedelta, timezone

import db
import dnc
import ops_dispositions
from dialer_import import normalize_phone, format_phone

CAP = 3                      # total dials (called_count) allowed before exhausted
CALLBACK_CAP = 5             # callbacks get more room — the customer asked us to call
COOLDOWN_DAYS = 90          # YPNI suppression window


def _cap_for(status):
    """Dial cap for a disposition — callbacks get the higher CALLBACK_CAP."""
    return CALLBACK_CAP if status in ops_dispositions.CALLBACK_CODES else CAP


def _ensure_lead(conn, d, phone10, batch_date, campaign_id=None):
    """Return the leads.id for this number, creating it from VICIdial's own record
    if we didn't generate it. Source-agnostic requeue: a not-reached number that
    isn't ours still needs a lead row so the cleaned/segmented dial export can
    re-serve it. Tagged lead_source='vicidial' so it stays separate from
    app-generated leads (and out of the lead-gen dashboard, which is run-scoped).
    campaign_id ties the lead to the client engagement (via VICIdial campaign_id)
    so redial cycles keep the same client/industry/offer and full history."""
    stored = format_phone(phone10)
    # This client loads the business name into vicidial_list.comments and the full
    # address into address1 (the standard name fields are empty) — confirmed via
    # the VICIdial inspector. Fall back to a contact name / phone for other layouts.
    fn = (d.get("first_name") or "").strip()
    ln = (d.get("last_name") or "").strip()
    comments = (d.get("comments") or "").strip()
    name = comments or (fn + " " + ln).strip() or f"VICIdial {phone10}"
    address = (d.get("address1") or "").strip()
    city = (d.get("city") or "").strip()
    region = (d.get("region") or "").strip()
    postcode = (d.get("postal_code") or "").strip()
    if not postcode and address:                      # zip is inside the address line
        m = re.search(r"\b(\d{5})(?:-\d{4})?\b", address)
        if m:
            postcode = m.group(1)
    email = (d.get("email") or "").strip()

    row = conn.execute(
        "SELECT id, business_name, lead_source, campaign_id FROM leads WHERE phone = ?",
        (stored,)).fetchone()
    if row:
        # Backfill a VICIdial-registered lead that was saved thin before this
        # mapping existed (never touch app-generated leads).
        if row["lead_source"] == "vicidial" and (
                not row["business_name"] or row["business_name"].startswith("VICIdial ")):
            conn.execute(
                "UPDATE leads SET business_name=?, address=?, city=?, state=?, postcode=?, email=? "
                "WHERE id=?",
                (name, address, city, region, postcode, email, row["id"]))
        # Attach to its campaign the first time we can map the VICIdial campaign_id.
        if campaign_id and row["campaign_id"] is None:
            conn.execute("UPDATE leads SET campaign_id=? WHERE id=?",
                         (campaign_id, row["id"]))
        return row["id"]
    status = "dnc" if dnc.is_suppressed(conn, phone10) else "new"
    conn.execute(
        "INSERT INTO leads (phone, business_name, address, city, state, postcode, email, "
        "pulled_date, campaign_id, lead_source, market_type, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'vicidial', 'b2b', ?)",
        (stored, name, address, city, region, postcode, email, batch_date, campaign_id, status),
    )
    return conn.execute("SELECT id FROM leads WHERE phone = ?", (stored,)).fetchone()["id"]

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
               "suppressed": 0, "dnc": 0, "unmatched": 0, "ignored": 0,
               "by_status": {}}
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
        # Per-disposition tally so the result page can mirror the ops scoreboard
        # and prove every not-reached lead is accounted for (none silently lost).
        if status:
            summary["by_status"][status] = summary["by_status"].get(status, 0) + 1

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
            # Source-agnostic: a 'not interested' number gets the 90-day cooldown
            # regardless of where the lead came from. suppressed_leads is keyed by
            # phone, so the dial export drops it whether or not it's our lead.
            summary["suppressed"] += 1
            if not dry_run:
                conn.execute(
                    "INSERT OR IGNORE INTO suppressed_leads (phone, reason, cooldown_until, added_at) "
                    "VALUES (?, ?, ?, ?)",
                    (phone, f"{status} - not interested", cooldown, now),
                )
            continue

        if status not in ops_dispositions.RETRY_CODES:
            # Reached-and-done outcomes (SALE, and any code we don't act on). Not a
            # lost lead — a completed one; counted here only so the totals add up.
            summary["ignored"] += 1
            continue

        state = "exhausted" if count >= _cap_for(status) else "active"
        campaign = (d.get("campaign") or "").strip()   # VICIdial campaign_id string
        summary["exhausted" if state == "exhausted" else "requeued"] += 1
        if not dry_run:
            # Map the VICIdial campaign_id to our campaign (client engagement), so
            # the redial lead stays tied to the same client/industry/offer.
            camp = db.campaign_by_vici(conn, campaign)
            camp_id = camp["id"] if camp else None
            # Source-agnostic: if we didn't generate this number, register it from
            # VICIdial's own record so the segmented dial export can re-serve it.
            lead_id = (lead_by_phone.get(phone)
                       or _ensure_lead(conn, d, phone, day, campaign_id=camp_id))
            lead_by_phone[phone] = lead_id
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
