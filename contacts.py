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

import site_contacts

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


def enrich_leads(conn, lead_rows, reveal_email=False, reveal_phone=False, log=print):
    """Enrich each website-having lead with a decision-maker via Apollo — as a
    WATERFALL over Outscraper's own enrichment:
      - if BOTH contact and email are already present (Outscraper's
        extract_contact/extract_email succeeded during the pull) -> skip Apollo
        entirely (counted as skipped_already_enriched).
      - if only one is missing -> call Apollo but write ONLY the missing field(s),
        never overwriting what Outscraper already found (a paid reveal for an
        email/contact we already have is wasted).
    Returns {checked, enriched, emails, phones, skipped_already_enriched,
    org_calls, site_hits, error}
    where `checked` is Apollo calls MADE and `skipped_already_enriched` is calls
    avoided."""
    checked = enriched = emails = phones = skipped = org_calls = site_hits = 0
    error = ""
    org_cache = {}          # domain -> org info, so a shared domain costs 1 credit
    for row in lead_rows:
        website = (row["website"] if "website" in row.keys() else "") or ""
        if not website:
            continue

        # Company firmographics: once per unique domain in this run (1 credit each),
        # and never re-fetched for a lead that already has them.
        have_org = ("employee_count" in row.keys()) and row["employee_count"] is not None
        dom = "" if have_org else domain_of(website)
        if dom and dom not in org_cache:
            try:
                org_cache[dom] = enrich_organization(website)
                org_calls += 1
            except Exception as e:
                org_cache[dom] = {}
                if not error:
                    error = str(e)
        org = org_cache.get(dom) or {}
        if org:
            conn.execute(
                "UPDATE leads SET employee_count = ?, company_revenue = ?, "
                "company_industry = ? WHERE id = ?",
                (org.get("employee_count"), org.get("revenue", ""),
                 org.get("industry", ""), row["id"]))
        have_contact = bool(((row["contact"] if "contact" in row.keys() else "") or "").strip())
        have_email = bool(((row["email"] if "email" in row.keys() else "") or "").strip())

        # TIER 0 (free): the business's own website. Local operators aren't in any
        # LinkedIn-derived database, but their About/Team page names them. Costs
        # nothing, so it runs before any paid provider and often removes the need
        # for one entirely.
        if not have_contact:
            try:
                found = site_contacts.find_team_contact(website)
            except Exception:
                found = {}
            if found.get("name"):
                conn.execute(
                    "UPDATE leads SET contact = ?, contact_title = ? WHERE id = ?",
                    (found["name"], found.get("title", ""), row["id"]))
                have_contact = True
                site_hits += 1
        # Waterfall gate: only call Apollo if it can still ADD something.
        #   - no contact yet                  -> Apollo may find one (search is free)
        #   - email missing AND revealing     -> Apollo may unlock it (1 credit)
        #   - revealing a direct dial         -> Apollo may add one (8 credits)
        # Otherwise (e.g. the website already gave us the name and reveals are off)
        # the call could return nothing useful, so skip it entirely.
        needs_apollo = ((not have_contact)
                        or (reveal_email and not have_email)
                        or reveal_phone)
        if not needs_apollo:
            skipped += 1
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
        # Fill ONLY the gaps — never overwrite Outscraper's contact/email.
        sets, vals = [], []
        if not have_contact and info.get("name"):
            sets += ["contact = ?", "contact_title = ?"]
            vals += [info["name"], info.get("title", "")]
        if not have_email and info.get("email"):
            sets.append("email = ?"); vals.append(info["email"]); emails += 1
        if info.get("direct_phone"):
            sets.append("direct_phone = ?"); vals.append(info["direct_phone"]); phones += 1
        if sets:
            vals.append(row["id"])
            conn.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id = ?", vals)
            enriched += 1
    conn.commit()
    return {"checked": checked, "enriched": enriched, "emails": emails,
            "phones": phones, "skipped_already_enriched": skipped,
            "org_calls": org_calls, "site_hits": site_hits, "error": error}
