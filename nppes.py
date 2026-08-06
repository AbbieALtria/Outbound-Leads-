"""
NPPES (NPI Registry) discovery source — CMS's free, public, authoritative
registry of US healthcare providers. No API key, no cost, no rate-limit tier.

Mirrors pipeline.query_outscraper()'s shape: query -> list of normalized place
dicts the pull loop can insert. The DATA SHAPE differs from Maps though — NPPES
has no website, reviews, or rating, so those stay null and any offer signal that
depends on them simply doesn't fire (same as any other missing signal).

⚠️ US ONLY. Verified against the live API: Ontario/Canada queries return 0
results. Canadian healthcare campaigns must keep using the Maps source.
"""

import re

import requests

API_URL = "https://npiregistry.cms.hhs.gov/api/"
API_VERSION = "2.1"
MAX_PER_CALL = 200          # NPPES hard limit per request

# Our industry slugs -> NPPES taxonomy_description search text. Only slugs that
# genuinely exist as NPI provider taxonomies are here; anything else (veterinarian,
# med spa, ...) is NOT an NPI provider type and must not be offered for this source.
TAXONOMY_BY_INDUSTRY = {
    "dentist": "Dentist",
    "chiropractor": "Chiropractor",
    "physiotherapy": "Physical Therapist",
    "optometrist": "Optometrist",
    "orthoptics": "Orthoptist",
    "pharmacy": "Pharmacy",
    "urgent_care": "Urgent Care",
    "medical_clinic": "Clinic/Center",
}


def supports(industry_slug):
    """True when this industry maps to a real NPI provider taxonomy."""
    return industry_slug in TAXONOMY_BY_INDUSTRY


def supported_industries():
    return sorted(TAXONOMY_BY_INDUSTRY)


def _digits(value):
    return re.sub(r"\D", "", value or "")


def query_nppes(industry_slug, city="", state="", limit=50, timeout=45):
    """Organizations of one provider type in a city/state.

    Returns place dicts shaped like the Maps source so the existing pull loop can
    consume them unchanged: name, phone, full_address, city, state, postal_code,
    plus npi + taxonomy. Website/reviews/rating are absent by design.

    state must be a 2-letter US code. Raises RuntimeError on API failure.
    """
    taxonomy = TAXONOMY_BY_INDUSTRY.get(industry_slug)
    if not taxonomy:
        raise RuntimeError(f"'{industry_slug}' has no NPI provider taxonomy — "
                           "NPPES only covers licensed healthcare provider types.")
    params = {
        "version": API_VERSION,
        "enumeration_type": "NPI-2",        # organizations, not individuals
        "taxonomy_description": taxonomy,
        "limit": min(int(limit), MAX_PER_CALL),
    }
    if city:
        params["city"] = city
    if state:
        params["state"] = state[:2].upper()
    try:
        resp = requests.get(API_URL, params=params, timeout=timeout)
    except requests.RequestException as e:
        raise RuntimeError(f"NPPES request failed: {e}")
    if resp.status_code != 200:
        raise RuntimeError(f"NPPES returned status {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if data.get("Errors"):
        raise RuntimeError(f"NPPES error: {data['Errors']}")

    out = []
    for rec in data.get("results", []):
        basic = rec.get("basic") or {}
        name = (basic.get("organization_name") or "").strip()
        if not name:
            continue
        # Prefer the LOCATION address (where they practise) over the mailing one.
        addrs = rec.get("addresses") or []
        loc = next((a for a in addrs if a.get("address_purpose") == "LOCATION"),
                   addrs[0] if addrs else {})
        phone = _digits(loc.get("telephone_number")
                        or basic.get("authorized_official_telephone_number"))
        tax = next((t for t in (rec.get("taxonomies") or []) if t.get("primary")),
                   (rec.get("taxonomies") or [{}])[0])
        street = (loc.get("address_1") or "").strip()
        city_v = (loc.get("city") or "").strip().title()
        state_v = (loc.get("state") or "").strip()
        postal = (loc.get("postal_code") or "")[:5]
        full = ", ".join(p for p in (street, city_v, f"{state_v} {postal}".strip()) if p)
        out.append({
            "name": name,
            "phone": phone,                      # '' when NPPES has none
            "full_address": full,
            "street": street,
            "city": city_v,
            "state": state_v,
            "postal_code": postal,
            "npi": rec.get("number"),
            "taxonomy": (tax.get("desc") or "").strip(),
            "source": "nppes",
        })
    return out
