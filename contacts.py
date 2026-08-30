"""
B2B contact enrichment — decision-maker name, title, email, direct dial.

Provider: Apollo.io. Dormant unless APOLLO_API_KEY is set (env var), so it costs
nothing until switched on.

Cost model (Apollo, 2026): People SEARCH returns name + title and is FREE (no
credits). Email/phone are unlocked via People ENRICH only when explicitly asked —
email = 1 credit, phone = 8 credits each — so reveal is opt-in (reveal_phone is
off by default). Names/titles alone already help agents get past the gatekeeper.

Only the free search path runs unless a reveal flag is set. The paid reveal path
should be verified against a live Apollo account before running at scale.
"""

import os
import re
from datetime import datetime

import requests

SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/search"
MATCH_URL = "https://api.apollo.io/api/v1/people/match"
# Organization Enrichment. NOTE: per Apollo's docs this costs 1 CREDIT PER
# ORGANIZATION (it is not free), so callers dedupe by domain within a run and the
# number of org calls actually made is reported back for cost visibility.
ORG_URL = "https://api.apollo.io/api/v1/organizations/enrich"

# Decision-makers worth reaching, best-first — covers TA's personas (Owner/GM/IT/
# Practice Manager) and small-business owners.
TARGET_TITLES = [
    "owner", "founder", "president", "ceo", "principal", "managing partner",
    "general manager", "coo", "vp operations", "director of operations",
    "operations manager", "office manager", "practice manager", "clinic manager",
    "it manager", "it director", "director of it",
]


def api_key():
    return os.environ.get("APOLLO_API_KEY", "").strip()


def enabled():
    """True when Apollo enrichment is configured."""
    return bool(api_key())


def domain_of(website):
    """Bare registrable domain from a website URL — Apollo matches on domain."""
    w = (website or "").strip().lower()
    if not w:
        return ""
    w = re.sub(r"^https?://", "", w)
    w = w.split("/")[0].split("?")[0]
    w = re.sub(r"^www\.", "", w)
    return w.strip()


def _headers():
    return {"X-Api-Key": api_key(), "Content-Type": "application/json",
            "Cache-Control": "no-cache"}


def find_contact(website, titles=None, reveal_email=False, reveal_phone=False,
                 timeout=30):
    """Best decision-maker for a company, matched by its website domain.
    Returns {name, title, email, direct_phone}; email/direct_phone are filled only
    when revealed AND Apollo has them. Search is free; reveal spends credits.
    Returns {} when disabled, no domain, or no match."""
    domain = domain_of(website)
    if not enabled() or not domain:
        return {}
    body = {
        "q_organization_domains": domain,
        "person_titles": titles or TARGET_TITLES,
        "page": 1, "per_page": 1,
    }
    resp = requests.post(SEARCH_URL, json=body, headers=_headers(), timeout=timeout)
    resp.raise_for_status()
    people = (resp.json() or {}).get("people") or []
    if not people:
        return {}
    p = people[0]
    out = {"name": (p.get("name") or "").strip(),
           "title": (p.get("title") or "").strip(),
           "email": "", "direct_phone": ""}
    if reveal_email or reveal_phone:
        out.update(_reveal(p, domain, reveal_email, reveal_phone, timeout))
    return out


def enrich_organization(website, timeout=30):
    """Company firmographics (employee count, revenue, industry) for a company,
    matched by website domain, via Apollo's Organization Enrichment endpoint.
    Returns {employee_count, revenue, industry} or {} when disabled / no domain /
    no match.

    COST: Apollo's docs put this at 1 credit per organization — it is NOT free, so
    callers should dedupe by domain and watch the reported call count against the
    account's actual usage rather than trusting an assumed price."""
    domain = domain_of(website)
    if not enabled() or not domain:
        return {}
    resp = requests.get(ORG_URL, params={"domain": domain}, headers=_headers(),
                        timeout=timeout)
    if resp.status_code != 200:
        return {}
    org = (resp.json() or {}).get("organization") or {}
    if not org:
        return {}
    # annual_revenue is numeric; annual_revenue_printed is the display string.
    revenue = org.get("annual_revenue_printed") or org.get("annual_revenue")
    return {
        "employee_count": org.get("estimated_num_employees"),
        "revenue": str(revenue).strip() if revenue not in (None, "") else "",
        "industry": (org.get("industry") or "").strip(),
    }


def _reveal(person, domain, reveal_email, reveal_phone, timeout):
    """People Enrich to unlock email (1 credit) / phone (8 credits). Best-effort;
    returns only the fields that came back."""
    body = {
        "first_name": person.get("first_name") or "",
        "last_name": person.get("last_name") or "",
        "domain": domain,
        "reveal_personal_emails": bool(reveal_email),
        "reveal_phone_number": bool(reveal_phone),
    }
    try:
        r = requests.post(MATCH_URL, json=body, headers=_headers(), timeout=timeout)
        if r.status_code != 200:
            return {}
        pr = (r.json() or {}).get("person") or {}
        out = {}
        if reveal_email and pr.get("email"):
            out["email"] = pr["email"].strip()
        if reveal_phone:
            phones = pr.get("phone_numbers") or []
            if phones:
                out["direct_phone"] = (phones[0].get("raw_number") or "").strip()
        return out
    except Exception:
        return {}


def _val(row, key):
    """Read an optional column off a sqlite3.Row (older callers may not select it)."""
    return (row[key] if key in row.keys() else None)


def enrich_leads(conn, lead_rows, reveal_email=False, reveal_phone=False, log=print,
                 force=False):
    """Enrich each website-having lead with a decision-maker via Apollo — as a
    WATERFALL over Outscraper's own enrichment:
      - if BOTH contact and email are already present (Outscraper's
        extract_contact/extract_email succeeded during the pull) -> skip Apollo
        entirely (counted as skipped_already_enriched).
      - if Apollo has ALREADY been asked about this lead on an earlier run
        (enrich_attempted_at is set) -> skip too, even when the record it produced
        is partial (contact but no email, or nothing at all). Apollo will return
        the same gap on a re-run, so paying for it again buys nothing; counted as
        skipped_already_attempted.
      - otherwise -> call Apollo but write ONLY the missing field(s), never
        overwriting what Outscraper already found (a paid reveal for an
        email/contact we already have is wasted).
    Org enrichment (1 credit/org) follows the same rule: skipped once the lead has
    firmographics OR an org lookup was already attempted for it.

    An attempt is only stamped when Apollo actually answered — a transport/API
    failure (bad key, rate limit) leaves the lead eligible for a later retry.
    Pass force=True to re-enrich regardless, for a deliberate refresh.

    Returns {checked, enriched, emails, phones, skipped_already_enriched,
    skipped_already_attempted, org_calls, error} where `checked` is Apollo contact
    calls MADE."""
    checked = enriched = emails = phones = skipped = org_calls = 0
    skipped_attempted = 0
    error = ""
    now = datetime.now().isoformat(timespec="seconds")
    org_cache = {}          # domain -> org info, so a shared domain costs 1 credit
    for row in lead_rows:
        website = (row["website"] if "website" in row.keys() else "") or ""
        if not website:
            continue

        # Company firmographics: once per unique domain in this run (1 credit each),
        # never re-fetched for a lead that already has them, and never re-paid for
        # a lead whose earlier lookup came back empty (org_enrich_attempted_at).
        have_org = ("employee_count" in row.keys()) and row["employee_count"] is not None
        org_tried = bool(_val(row, "org_enrich_attempted_at"))
        dom = "" if (have_org or (org_tried and not force)) else domain_of(website)
        if dom and dom not in org_cache:
            try:
                org_cache[dom] = enrich_organization(website)
                org_calls += 1
            except Exception as e:
                org_cache[dom] = {}
                if not error:
                    error = str(e)
                dom = ""            # API failed — stay eligible for a later retry
        if dom:
            org = org_cache.get(dom) or {}
            if org:
                conn.execute(
                    "UPDATE leads SET employee_count = ?, company_revenue = ?, "
                    "company_industry = ?, org_enrich_attempted_at = ? WHERE id = ?",
                    (org.get("employee_count"), org.get("revenue", ""),
                     org.get("industry", ""), now, row["id"]))
            else:
                # Apollo answered with no match: record the attempt so this lead
                # isn't charged for the same empty answer on every future run.
                conn.execute(
                    "UPDATE leads SET org_enrich_attempted_at = ? WHERE id = ?",
                    (now, row["id"]))
        have_contact = bool(((row["contact"] if "contact" in row.keys() else "") or "").strip())
        have_email = bool(((row["email"] if "email" in row.keys() else "") or "").strip())
        # Waterfall: Outscraper already got both -> don't spend an Apollo call.
        if have_contact and have_email:
            skipped += 1
            continue
        # Already asked Apollo about this lead on an earlier run — a partial or
        # empty result is Apollo's answer, not a reason to pay for it again.
        if _val(row, "enrich_attempted_at") and not force:
            skipped_attempted += 1
            continue
        checked += 1
        try:
            # Only pay for the email reveal when the email is actually missing.
            info = find_contact(website, reveal_email=reveal_email and not have_email,
                                reveal_phone=reveal_phone)
        except Exception as e:
            # Surface the first failure (e.g. bad key / plan / rate limit) instead
            # of silently finding nothing.
            if not error:
                error = str(e)
            log(f"  enrich failed for lead {row['id']}: {e}")
            continue
        # Apollo answered (even if with nothing): mark the lead as processed.
        sets, vals = ["enrich_attempted_at = ?"], [now]
        # Fill ONLY the gaps — never overwrite Outscraper's contact/email.
        if not have_contact and info.get("name"):
            sets += ["contact = ?", "contact_title = ?"]
            vals += [info["name"], info.get("title", "")]
        if not have_email and info.get("email"):
            sets.append("email = ?"); vals.append(info["email"]); emails += 1
        if info.get("direct_phone"):
            sets.append("direct_phone = ?"); vals.append(info["direct_phone"]); phones += 1
        vals.append(row["id"])
        conn.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id = ?", vals)
        if len(sets) > 1:
            enriched += 1
    conn.commit()
    return {"checked": checked, "enriched": enriched, "emails": emails,
            "phones": phones, "skipped_already_enriched": skipped,
            "skipped_already_attempted": skipped_attempted,
            "org_calls": org_calls, "error": error}
