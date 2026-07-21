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

import requests

SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/search"
MATCH_URL = "https://api.apollo.io/api/v1/people/match"

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


def enrich_leads(conn, lead_rows, reveal_email=False, reveal_phone=False, log=print):
    """Enrich each lead that has a website with a decision-maker name/title (+ email/
    direct dial when revealed). Writes contact / contact_title / email / direct_phone.
    Returns {checked, enriched, emails, phones, error}."""
    checked = enriched = emails = phones = 0
    error = ""
    for row in lead_rows:
        website = (row["website"] if "website" in row.keys() else "") or ""
        if not website:
            continue
        checked += 1
        try:
            info = find_contact(website, reveal_email=reveal_email,
                                reveal_phone=reveal_phone)
        except Exception as e:
            # Surface the first failure (e.g. bad key / plan / rate limit) instead
            # of silently finding nothing.
            if not error:
                error = str(e)
            log(f"  enrich failed for lead {row['id']}: {e}")
            continue
        if not info.get("name"):
            continue
        sets, vals = ["contact = ?", "contact_title = ?"], [info["name"], info.get("title", "")]
        if info.get("email"):
            sets.append("email = ?"); vals.append(info["email"]); emails += 1
        if info.get("direct_phone"):
            sets.append("direct_phone = ?"); vals.append(info["direct_phone"]); phones += 1
        vals.append(row["id"])
        conn.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id = ?", vals)
        enriched += 1
    conn.commit()
    return {"checked": checked, "enriched": enriched, "emails": emails,
            "phones": phones, "error": error}
