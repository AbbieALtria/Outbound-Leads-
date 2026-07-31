"""
B2B intent-signal intake: company-level (not phone-level) signals matched onto
existing leads.

Two vendor-agnostic sources, same shape (company_name, domain?, signal_strength,
last_seen_date, topic?):
  - SITE VISITOR  (Leadfeeder / Dealfront / Albacross): "Company X visited N times,
    last on DATE" from a tracking script on the client's own site.
  - INTENT        (Bombora / G2 Buyer Intent): "Company X is researching TOPIC".

Modeled on leads_intake.py (field-aliasing dict, CSV import) but matched by
COMPANY, not phone: prefer the website domain, fall back to the normalized
business name (reusing pipeline._norm_name — the multi_location grouping key).
Unmatched signals are kept in `unmatched_signals` for review / later backfill —
we never auto-create a lead (these sources carry no phone number).
"""

import csv
import io
import re

import db
import scoring
from contacts import domain_of
from pipeline import _norm_name

KINDS = ("site_visitor", "intent")


def _norm(s):
    """Normalize a CSV header / alias (same as leads_intake)."""
    return re.sub(r"[^a-z0-9]+", "_", str(s or "").strip().lower()).strip("_")


# Vendor field names -> our common shape.
FIELD_ALIASES = {
    "company_name": ["company_name", "company", "account", "account_name",
                     "organization", "organisation", "org", "name", "business",
                     "business_name"],
    "domain": ["domain", "website", "url", "web", "company_domain", "site",
               "web_domain"],
    "signal_strength": ["signal_strength", "strength", "score", "visits",
                        "visit_count", "visits_count", "intent_score", "count",
                        "sessions", "pageviews"],
    "last_seen_date": ["last_seen_date", "last_seen", "last_visit", "last_visit_date",
                       "date", "last_active", "last_activity", "timestamp",
                       "last_seen_at"],
    "topic": ["topic", "intent_topic", "category", "keyword", "surge_topic",
              "subject", "theme"],
}


def _pick(row_norm, field):
    for alias in FIELD_ALIASES.get(field, []):
        key = _norm(alias)
        if key in row_norm and str(row_norm[key]).strip():
            return str(row_norm[key]).strip()
    return ""


def _as_int(v):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


# --- matching -------------------------------------------------------------

def build_index(conn):
    """One pass over leads -> ({domain: [ids]}, {norm_name: [ids]}) for fast match."""
    by_domain, by_name = {}, {}
    for r in conn.execute("SELECT id, business_name, website FROM leads"):
        nm = _norm_name(r["business_name"])
        if nm:
            by_name.setdefault(nm, []).append(r["id"])
        dom = domain_of(r["website"])
        if dom:
            by_domain.setdefault(dom, []).append(r["id"])
    return by_domain, by_name


def match(company_name, domain, index):
    """Lead ids for this company. Domain first (more reliable), then name."""
    by_domain, by_name = index
    dom = domain_of(domain)
    if dom and dom in by_domain:
        return by_domain[dom]
    nm = _norm_name(company_name)
    if nm and nm in by_name:
        return by_name[nm]
    return []


def apply_one(conn, kind, data, index, source, affected=None):
    """Match one signal onto lead(s) and stamp its columns, or record it as
    unmatched. Appends any matched lead ids to `affected` (for rescoring).
    Returns 'matched' | 'unmatched' | 'invalid'."""
    company = (data.get("company_name") or "").strip()
    domain = (data.get("domain") or "").strip()
    if not company and not domain:
        return "invalid"
    ids = match(company, domain, index)
    if ids:
        if affected is not None:
            affected.extend(ids)
        ph = ",".join("?" * len(ids))
        last_seen = data.get("last_seen_date", "")
        if kind == "intent":
            conn.execute(
                f"UPDATE leads SET intent_topic = ?, intent_last_seen_date = ? WHERE id IN ({ph})",
                [data.get("topic", ""), last_seen, *ids])
        else:  # site_visitor
            conn.execute(
                f"UPDATE leads SET site_visitor = 1, site_visit_count = ?, "
                f"site_last_visit_date = ? WHERE id IN ({ph})",
                [_as_int(data.get("signal_strength")), last_seen, *ids])
        return "matched"
    conn.execute(
        "INSERT INTO unmatched_signals (kind, company_name, domain, signal_strength, "
        "last_seen_date, topic, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, company, domain, str(data.get("signal_strength", "")),
         data.get("last_seen_date", ""), data.get("topic", ""), source, db.now_iso()))
    return "unmatched"


def import_csv(conn, text, kind, source):
    """Import a CSV of signals of one `kind`. Returns a summary dict."""
    if kind not in KINDS:
        raise ValueError(f"Unknown signal kind '{kind}'.")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("Empty CSV or no header row.")
    index = build_index(conn)
    summary = {"matched": 0, "unmatched": 0, "invalid": 0, "total": 0}
    affected = []
    for row in reader:
        summary["total"] += 1
        row_norm = {_norm(k): v for k, v in row.items()}
        data = {field: _pick(row_norm, field) for field in FIELD_ALIASES}
        summary[apply_one(conn, kind, data, index, source, affected)] += 1
    conn.commit()
    scoring.rescore_leads(conn, affected)   # matched leads reflect the new signal now
    return summary


# --- unmatched review / backfill ------------------------------------------

def unmatched_count(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM unmatched_signals").fetchone()["n"]


def unmatched_list(conn, limit=300):
    return conn.execute(
        "SELECT * FROM unmatched_signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def rematch_unmatched(conn):
    """Re-run matching for stored unmatched signals against the CURRENT leads (e.g.
    after a later Maps pull added a phone for that company). Newly-matched signals
    are applied and removed from the unmatched table. Returns how many matched."""
    index = build_index(conn)
    matched, affected = 0, []
    for r in conn.execute("SELECT * FROM unmatched_signals").fetchall():
        data = {"company_name": r["company_name"], "domain": r["domain"],
                "signal_strength": r["signal_strength"],
                "last_seen_date": r["last_seen_date"], "topic": r["topic"]}
        if match(r["company_name"], r["domain"], index):
            apply_one(conn, r["kind"], data, index, r["source"], affected)
            conn.execute("DELETE FROM unmatched_signals WHERE id = ?", (r["id"],))
            matched += 1
    conn.commit()
    scoring.rescore_leads(conn, affected)
    return matched
