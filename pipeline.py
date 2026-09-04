"""
Lead pull pipeline: queries Outscraper, filters chains, dedupes against the
leads table, scores, and inserts new leads into leads.db.

Callable from the web app (in a background thread) or from the command line:

    python pipeline.py --industry hvac --leads 100
    python pipeline.py --industry hvac,plumbing,roofing --leads 100
"""

import argparse
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import requests

import db
import geo_data
import nppes
import dnc
import scoring

OUTSCRAPER_URL = "https://api.outscraper.com/maps/search-v3"

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env"


def load_env_file():
    """Load KEY=VALUE lines from .env into os.environ (existing vars win)."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_api_key():
    load_env_file()
    return os.environ.get("OUTSCRAPER_API_KEY", "").strip()


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def query_outscraper(api_key, query_text, limit, enrich_contacts=False):
    params = {"query": query_text, "limit": limit, "async": "false"}
    if enrich_contacts:
        # Emails & Contacts enrichment: adds email_1..3 and contact names/titles
        # to each place (extra Outscraper credits per lead).
        params["enrichment"] = "domains_service"
    headers = {"X-API-KEY": api_key}
    resp = requests.get(OUTSCRAPER_URL, params=params, headers=headers, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Outscraper returned status {resp.status_code} for '{query_text}': {resp.text[:300]}"
        )
    data = resp.json()
    results = data.get("data", data)
    flat = []
    for item in results:
        if isinstance(item, list):
            flat.extend(item)
        else:
            flat.append(item)
    return flat


def score_place(place):
    """Higher score = better SEO prospect. Website absence is the strongest
    available signal from raw listing data."""
    score = 0
    hooks = []

    website = (place.get("site") or place.get("website") or "").strip()
    if not website:
        score += 50
        hooks.append("No website listed -- invisible in search results")
    else:
        score += 10

    rating_count = place.get("reviews") or place.get("reviews_count") or 0
    try:
        rating_count = int(rating_count)
    except (TypeError, ValueError):
        rating_count = 0
    if rating_count < 10:
        score += 20
        hooks.append(f"Only {rating_count} reviews -- weak online reputation")
    elif rating_count < 25:
        score += 10

    city = place.get("city") or ""
    state = place.get("state") or place.get("region") or ""
    if city and state:
        hooks.append(f"Local angle: rank for '[service] in {city}, {state}'")

    return score, " | ".join(hooks)


ROLE_ACCOUNTS = {
    "info", "sales", "contact", "admin", "office", "service", "support",
    "hello", "help", "team", "mail", "email", "billing", "accounts", "accounting",
    "customerservice", "customercare", "dispatch", "scheduling", "appointments",
    "estimates", "quotes", "no-reply", "noreply", "webmaster", "marketing",
    "hr", "jobs", "careers", "manager", "owner", "general",
}


# Role words that show up as ONE PART of a compound local-part —
# privacy.officer@, front.desk@, new.patients@. ROLE_ACCOUNTS only matches a
# whole local-part, so "privacy.officer" passed it and reached an agent as a
# person called "Privacy Officer".
ROLE_WORDS = ROLE_ACCOUNTS | {
    "officer", "privacy", "reception", "receptionist", "frontdesk", "front",
    "desk", "enquiries", "inquiries", "booking", "bookings", "reservations",
    "patient", "patients", "practice", "clinic", "dental", "care", "emergency",
    "emergencies", "hello", "enquiry", "inquiry",
}


def name_from_email(email):
    """Derive a plausible person name from an email local-part, or '' if it looks
    like a role inbox (info@, privacy.officer@, ...) rather than a person."""
    local = email.split("@", 1)[0].lower()
    local = re.sub(r"\d+", "", local)  # drop trailing digits like john12
    if local in ROLE_ACCOUNTS or not local:
        return ""
    parts = [p for p in re.split(r"[._-]+", local) if p]
    # A role word anywhere in the local-part means the inbox is a function, not
    # a person -- worth losing a genuine "john.officer" to avoid handing an
    # agent a name that makes the call sound automated.
    if any(p in ROLE_WORDS for p in parts):
        return ""
    # first.last -> "First Last"; single short token isn't reliably a name
    if len(parts) >= 2 and all(p.isalpha() and len(p) > 1 for p in parts[:2]):
        return " ".join(p.capitalize() for p in parts[:2])
    return ""


# Words that are trades/entity types, never surnames. Outscraper sometimes returns
# these as a "last name" (e.g. "Stacey Dentist" for stacey@barriedentist.ca).
NOT_SURNAMES = {
    "dentist", "dental", "dentistry", "clinic", "clinics", "medical", "health",
    "healthcare", "care", "vet", "veterinary", "optical", "optometry", "pharmacy",
    "physio", "physiotherapy", "chiropractic", "law", "legal", "realty", "insurance",
    "group", "centre", "center", "services", "service", "solutions", "systems",
    "company", "associates", "partners", "practice", "office", "hotel", "motel",
    "spa", "salon", "auto", "hvac", "plumbing", "roofing", "electric", "electrical",
    "inc", "llc", "ltd", "corp", "co", "team", "staff", "admin", "reception",
}


def _plausible_person(name, place):
    """Reject a 'name' whose surname is a trade or entity word rather than a real
    surname. A wrong name on an agent's screen is worse than no name — they'd ask
    for "Stacey Dentist" and lose credibility instantly."""
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    if len(parts) < 2:
        return False
    surname = parts[-1].lower().strip(".,")
    if surname in NOT_SURNAMES or len(surname) < 3:
        return False
    # A surname that IS the business name is usually genuine (John Smith at
    # smithdental.ca), so only the trade-word check above rejects.
    return True


def _other_city_in_email(email, place_city):
    """City named in an email address that isn't this listing's city.

    A chain publishes one mailbox per site — fom_grandeprairie@sandman.ca — and
    Maps happily attaches it to a different site's listing. The contact is real,
    but they work somewhere else, and an agent asking Abbotsford for the Grande
    Prairie manager has lost the call in its first sentence.
    """
    local = (email or "").split("@", 1)[0].lower()
    if not local:
        return ""
    squashed = re.sub(r"[^a-z]", "", local)
    here = re.sub(r"[^a-z]", "", (place_city or "").lower())
    for cities in geo_data.CITIES_BY_STATE.values():
        for city in cities:
            key = re.sub(r"[^a-z]", "", city.lower())
            # Short names like "Ajax" or "York" appear inside ordinary words, so
            # only trust a match long enough to be deliberate.
            if len(key) >= 6 and key in squashed and key != here:
                return city
    return ""


def extract_contact(place):
    """Best decision-maker guess as 'Name (Title)'. Outscraper's Maps data rarely
    carries a real person here, so this is best-effort: prefer a named contact
    with an owner/GM/CEO-ish title, else any named contact, else a name inferred
    from a personal email address. Returns '' when nothing credible is found."""
    candidates = []
    for i in (1, 2, 3):
        name = (place.get(f"email_{i}_full_name") or "").strip()
        title = (place.get(f"email_{i}_title") or "").strip()
        if name:
            candidates.append((name, title))
    email = extract_email(place)
    # A mailbox belonging to another branch means the person on the other end of
    # it is not at this address, whatever their title says.
    elsewhere = _other_city_in_email(email, place.get("city") or "")
    if elsewhere:
        return ""

    decision_words = ("owner", "founder", "ceo", "president", "general manager",
                      "gm", "principal", "partner", "director", "manager")
    for name, title in candidates:
        if any(w in title.lower() for w in decision_words):
            return f"{name} ({title})"
    if candidates:
        name, title = candidates[0]
        if _plausible_person(name, place):
            return f"{name} ({title})" if title else name
    # Fall back to a name inferred from the primary email address.
    inferred = name_from_email(email)
    return f"{inferred} (from email)" if inferred else ""


def extract_email(place):
    for key in ("email_1", "email", "email_2", "email_3"):
        value = (place.get(key) or "").strip()
        if value and "@" in value:
            return value
    return ""


def extract_postcode(place, address):
    value = str(place.get("postal_code") or "").strip()
    if value:
        return value
    m = re.search(r"\b(\d{5})(?:-\d{4})?\s*$", address or "")
    return m.group(1) if m else ""


def seo_site_check(url):
    """Small SEO probe of a lead's website, to arm the agent with specifics.
    Returns (extra_score, [findings])."""
    if not url.lower().startswith(("http://", "https://")):
        url = "http://" + url
    findings, extra = [], 0
    try:
        resp = requests.get(url, timeout=8, allow_redirects=True,
                            headers={"User-Agent": "Mozilla/5.0 (compatible; site-check)"})
        if resp.status_code >= 400:
            return 20, [f"Website returns error {resp.status_code} -- effectively invisible"]
        html = resp.text[:80000].lower()
        if resp.url.startswith("http://"):
            extra += 5
            findings.append("Site has no HTTPS -- browsers mark it 'not secure'")
        title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S)
        if not title or not title.group(1).strip():
            extra += 10
            findings.append("Missing page title -- Google can't rank it properly")
        if 'name="description"' not in html and "name='description'" not in html:
            extra += 5
            findings.append("No meta description -- weak search result snippet")
        if "viewport" not in html:
            extra += 10
            findings.append("Not mobile-friendly -- most local searches are on phones")
    except requests.RequestException:
        return 20, ["Website unreachable -- dead link on their listing"]
    return extra, findings


def check_new_websites(conn, lead_ids, run_id=None, log=print):
    """Run seo_site_check over freshly pulled leads that have a website and
    fold the findings into their score and call hook."""
    rows = [
        r for r in conn.execute(
            f"SELECT id, website FROM leads WHERE id IN ({','.join('?' * len(lead_ids))})",
            lead_ids,
        ) if r["website"]
    ] if lead_ids else []
    if not rows:
        return
    if run_id:
        conn.execute("UPDATE pull_runs SET current_city = ? WHERE id = ?",
                     (f"SEO-checking {len(rows)} websites", run_id))
        conn.commit()
    log(f"SEO-checking {len(rows)} lead websites...")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda r: seo_site_check(r["website"]), rows))

    for row, (extra, findings) in zip(rows, results):
        if not findings:
            continue
        conn.execute(
            "UPDATE leads SET score = score + ?, "
            "call_hook = CASE WHEN call_hook = '' THEN ? "
            "ELSE call_hook || ' | ' || ? END WHERE id = ?",
            (extra, " | ".join(findings), " | ".join(findings), row["id"]),
        )
    conn.commit()


PHONES_ENRICHER_URL = "https://api.outscraper.com/phones-enricher"
# Reviews are a SEPARATE Outscraper service from Places search and are billed per
# REVIEW (~$3 / 1,000 at the time of writing, first 500 free) — so this only runs
# when the `review_signals` setting is on, and pulls a small number per lead.
REVIEWS_URL = "https://api.outscraper.com/maps/reviews-v3"
REVIEWS_PER_LEAD = 10


def fetch_reviews(api_key, query, limit=REVIEWS_PER_LEAD, timeout=120):
    """Newest reviews for one place (query = place id / name+address / maps url).
    Returns (first_review_date, sample_text): the earliest review date seen and the
    concatenated text of the most recent reviews. Best-effort — returns ('', '')
    when the service errors or has nothing, so a pull never fails on reviews."""
    try:
        resp = requests.get(
            REVIEWS_URL,
            params={"query": query, "reviewsLimit": limit, "sort": "newest",
                    "async": "false"},
            headers={"X-API-KEY": api_key}, timeout=timeout,
        )
        if resp.status_code != 200:
            return "", ""
        rows = resp.json().get("data", [])
    except Exception:
        return "", ""
    # Response shape: [ {..place.., reviews_data: [ {review_text, ...}, ... ] } ]
    reviews = []
    for item in rows if isinstance(rows, list) else [rows]:
        if isinstance(item, dict):
            reviews.extend(item.get("reviews_data") or [])
        elif isinstance(item, list):
            for sub in item:
                reviews.extend((sub or {}).get("reviews_data") or [])
    if not reviews:
        return "", ""
    texts, dates = [], []
    for r in reviews:
        t = (r.get("review_text") or "").strip()
        if t:
            texts.append(t)
        # Field name varies by service version — accept the known variants.
        for key in ("review_datetime_utc", "review_timestamp", "date", "review_date"):
            v = r.get(key)
            if v:
                dates.append(str(v))
                break
    first = min(dates)[:10] if dates else ""
    return first, " | ".join(texts[:limit])[:4000]


def collect_review_signals(conn, lead_ids, api_key, log=print):
    """Fetch review data for freshly pulled leads and store first_review_date +
    review_text_sample (powers new_in_market / review_pain_match). Opt-in: only
    called when the review_signals setting is on. Costs Outscraper review credits."""
    rows = conn.execute(
        f"SELECT id, business_name, address, maps_url FROM leads "
        f"WHERE id IN ({','.join('?' * len(lead_ids))})", lead_ids).fetchall() if lead_ids else []
    done = 0
    for row in rows:
        query = row["maps_url"] or ", ".join(
            p for p in (row["business_name"], row["address"]) if p)
        if not query:
            continue
        first, sample = fetch_reviews(api_key, query)
        if first or sample:
            conn.execute(
                "UPDATE leads SET first_review_date = ?, review_text_sample = ? WHERE id = ?",
                (first, sample, row["id"]))
            done += 1
    conn.commit()
    log(f"  review signals captured for {done} lead(s)")
    return done


def validate_phones(api_key, phones):
    """Validate a batch of phone numbers via Outscraper's phones-enricher.
    Returns {original_phone: {"type": <line type>, "valid": bool, "carrier": str}}.
    A number Outscraper can't identify a carrier for is treated as invalid.
    Raises RuntimeError on API failure so callers can surface it."""
    phones = [p for p in phones if p]
    if not phones:
        return {}
    result = {}
    # phones-enricher accepts a repeated `query` param; chunk to keep URLs sane.
    for start in range(0, len(phones), 25):
        chunk = phones[start:start + 25]
        resp = requests.get(
            PHONES_ENRICHER_URL,
            params={"query": chunk, "async": "false"},
            headers={"X-API-KEY": api_key},
            timeout=120,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Phone validation failed (status {resp.status_code}): {resp.text[:200]}"
            )
        data = resp.json()
        rows = data.get("data", data)
        flat = []
        for item in rows:
            flat.extend(item if isinstance(item, list) else [item])
        for original, row in zip(chunk, flat):
            carrier_type = (row.get("carrier_type") or "").strip().lower()
            carrier_name = (row.get("carrier_name") or "").strip()
            valid = bool(carrier_type and carrier_type not in ("invalid", "unknown"))
            result[original] = {
                "type": carrier_type or "invalid",
                "valid": valid,
                "carrier": carrier_name,
            }
    return result


def verify_lead_phones(conn, api_key, lead_rows, run_id=None, log=print):
    """Validate phones for the given lead rows (id, phone) and write phone_type /
    phone_valid / validated_at back. Invalid numbers are marked (status stays,
    but they're excluded from dial exports). Returns a summary dict."""
    lead_rows = [r for r in lead_rows if r["phone"]]
    if not lead_rows:
        return {"checked": 0, "invalid": 0}
    if run_id:
        conn.execute("UPDATE pull_runs SET current_city = ? WHERE id = ?",
                     (f"validating {len(lead_rows)} phone numbers", run_id))
        conn.commit()
    log(f"Validating {len(lead_rows)} phone numbers...")

    validation = validate_phones(api_key, [r["phone"] for r in lead_rows])
    now = db.now_iso()
    invalid = 0
    for row in lead_rows:
        info = validation.get(row["phone"])
        if not info:
            continue
        conn.execute(
            "UPDATE leads SET phone_type = ?, phone_valid = ?, validated_at = ? WHERE id = ?",
            (info["type"], 1 if info["valid"] else 0, now, row["id"]),
        )
        if not info["valid"]:
            invalid += 1
    conn.commit()
    return {"checked": len(lead_rows), "invalid": invalid}


# Words that mark a result as actually being in the industry asked for. Maps
# search is a ranking, not a filter: query "dentist in Arviat" against a hamlet
# with no dentist and it returns the nearest businesses it has — which is how a
# Tim Hortons and a public library reached a dental list. Checked against the
# business name AND the category Maps returns, so either one is enough to keep.
INDUSTRY_KEYWORDS = {
    "dentist": ("dent", "orthodont", "endodont", "periodont", "prosthodont",
                "oral surg", "denturist", "hygien"),
    "chiropractor": ("chiroprac",), "optometrist": ("optom", "eye", "optic", "vision"),
    "orthoptics": ("orthopt", "eye", "vision"), "veterinarian": ("vet", "animal", "pet"),
    "physiotherapy": ("physio", "physical therap", "rehab"),
    "pharmacy": ("pharmac", "drug", "chemist"),
    "medical_clinic": ("medic", "clinic", "health", "doctor", "physician", "family practice"),
    "urgent_care": ("urgent", "walk-in", "walk in", "emergency", "medic", "clinic"),
    "med_spa": ("med spa", "medspa", "aesthet", "esthet", "skin", "laser", "cosmetic"),
    "law_firm": ("law", "attorney", "legal", "solicitor", "barrister", "notary", "lawyer"),
    "accountant": ("account", "cpa", "tax", "bookkeep", "audit"),
    "insurance_agency": ("insur", "assurance", "broker"),
    "real_estate": ("real estate", "realt", "broker", "property"),
    "consulting_firm": ("consult", "advisor", "advisory"),
    "hotel": ("hotel", "motel", "inn", "resort", "lodg", "hostel", "suites"),
    "car_dealership": ("dealer", "auto", "car", "motor", "vehicle", "truck"),
    "auto_repair": ("auto", "car", "mechanic", "repair", "garage", "service station"),
    "auto_body": ("auto", "body", "collision", "paint", "car"),
    "auto_detailing": ("detail", "auto", "car wash", "car"),
    "tire_shop": ("tire", "tyre", "wheel", "auto"),
    "towing": ("tow", "recovery", "roadside"),
    "hvac": ("hvac", "heating", "cooling", "air condition", "furnace", "ventilat"),
    "plumbing": ("plumb", "drain", "rooter", "septic", "water"),
    "electrician": ("electric",), "roofing": ("roof",), "flooring": ("floor", "carpet", "tile"),
    "painting": ("paint", "decorat"), "fencing": ("fence", "fencing", "gate"),
    "concrete": ("concrete", "cement", "paving", "masonry"),
    "landscaping": ("landscap", "garden", "lawn", "yard", "nursery"),
    "lawn_care": ("lawn", "landscap", "garden", "turf"),
    "tree_service": ("tree", "arborist", "stump"),
    "pest_control": ("pest", "exterminat", "termite", "wildlife"),
    "cleaning": ("clean", "janitor", "maid", "housekeep"),
    "carpet_cleaning": ("carpet", "clean", "upholster", "rug"),
    "pressure_washing": ("pressure", "power wash", "wash", "clean", "exterior"),
    "junk_removal": ("junk", "removal", "haul", "rubbish", "waste", "disposal"),
    "moving": ("mov", "relocat", "storage", "van line"),
    "locksmith": ("lock", "key", "security"),
    "garage_door": ("garage", "door", "overhead"),
    "gutter": ("gutter", "eaves", "downspout", "roof"),
    "chimney": ("chimney", "fireplace", "sweep", "masonry"),
    "septic": ("septic", "sewer", "waste", "plumb"),
    "solar": ("solar", "photovolta", "energy", "renewab"),
    "pool_service": ("pool", "spa", "hot tub"),
    "appliance_repair": ("applian", "repair"), "handyman": ("handyman", "repair", "home"),
    "remodeling": ("remodel", "renovat", "contractor", "construct", "kitchen", "bath"),
    "restoration": ("restor", "water damage", "fire damage", "mold", "mould"),
    "furniture_store": ("furnitur", "mattress", "home", "interior"),
}


def _industry_terms(slug, label=""):
    """Keywords that mark a result as belonging to `slug`.

    Falls back to the slug's own words for industries added by the user, so a
    new industry is filtered on its own name rather than not at all.
    """
    terms = INDUSTRY_KEYWORDS.get(slug)
    if terms:
        return terms
    words = [w for w in re.split(r"[^a-z]+", f"{slug} {label}".lower()) if len(w) > 3]
    return tuple(dict.fromkeys(words)) or (slug.lower(),)


def on_industry(slug, label, business_name, category):
    """True when a returned place plausibly belongs to the industry searched for.

    Permissive on purpose: the name OR the Maps category matching is enough, so
    "Dr. Kerby Bruce and Associates" survives on its category and "Downtown
    Dental" on its name. Only a result matching on neither is dropped.
    """
    terms = _industry_terms(slug, label)
    haystack = f"{business_name} {category}".lower()
    return any(t in haystack for t in terms)


def is_chain(business_name, chain_names):
    if not business_name:
        return False
    name_lower = business_name.lower()
    return any(chain.lower() in name_lower for chain in chain_names)


def _update_run(conn, run_id, **fields):
    if run_id is None:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE pull_runs SET {sets} WHERE id = ?", (*fields.values(), run_id))
    conn.commit()


def _is_cancelled(conn, run_id):
    """True if a Stop request was written to this run's row (from the web app)."""
    if run_id is None:
        return False
    row = conn.execute("SELECT cancel FROM pull_runs WHERE id = ?", (run_id,)).fetchone()
    return bool(row and row["cancel"])


_STATE_CODES = {
    "alabama":"AL","alaska":"AK","arizona":"AZ","arkansas":"AR","california":"CA",
    "colorado":"CO","connecticut":"CT","delaware":"DE","florida":"FL","georgia":"GA",
    "hawaii":"HI","idaho":"ID","illinois":"IL","indiana":"IN","iowa":"IA","kansas":"KS",
    "kentucky":"KY","louisiana":"LA","maine":"ME","maryland":"MD","massachusetts":"MA",
    "michigan":"MI","minnesota":"MN","mississippi":"MS","missouri":"MO","montana":"MT",
    "nebraska":"NE","nevada":"NV","new hampshire":"NH","new jersey":"NJ","new mexico":"NM",
    "new york":"NY","north carolina":"NC","north dakota":"ND","ohio":"OH","oklahoma":"OK",
    "oregon":"OR","pennsylvania":"PA","rhode island":"RI","south carolina":"SC",
    "south dakota":"SD","tennessee":"TN","texas":"TX","utah":"UT","vermont":"VT",
    "virginia":"VA","washington":"WA","west virginia":"WV","wisconsin":"WI",
    "wyoming":"WY","district of columbia":"DC",
}


def abbrev_state_code(state):
    """2-letter US state code (NPPES requires it); '' for anything non-US."""
    s = (state or "").strip()
    if len(s) == 2:
        return s.upper()
    return _STATE_CODES.get(s.lower(), "")


def _norm_name(name):
    """Loose key for matching the same business across locations: lowercased,
    punctuation + common legal suffixes stripped."""
    n = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    n = re.sub(r"\b(inc|llc|ltd|co|corp|corporation|company|the)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


# Mail hosts and site builders anyone can sign up for. Two businesses sharing
# one of these share a vendor, not an owner, so they must never group.
GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.ca", "yahoo.co.uk",
    "hotmail.com", "hotmail.ca", "outlook.com", "live.com", "live.ca", "msn.com",
    "aol.com", "icloud.com", "me.com", "mac.com", "protonmail.com", "proton.me",
    "gmx.com", "zoho.com", "mail.com", "email.com",
    # Canadian ISP mailboxes — very common for owner-operators
    "shaw.ca", "telus.net", "rogers.com", "bell.net", "sympatico.ca",
    "videotron.ca", "cogeco.ca", "eastlink.ca", "xplornet.com",
    # hosted site builders / directories
    "wixsite.com", "wix.com", "squarespace.com", "wordpress.com", "weebly.com",
    "business.site", "godaddysites.com", "myshopify.com", "blogspot.com",
    "facebook.com", "sites.google.com", "webs.com", "jimdosite.com",
    "yelp.com", "linktr.ee",
}


def _org_domain(value):
    """Owning domain from an email address or URL; '' when absent or generic.

    Returning '' for the generic hosts above is the whole point of this helper:
    a blank key never groups, so two unrelated clinics on gmail stay separate.
    """
    v = (value or "").strip().lower()
    if not v:
        return ""
    host = v.rsplit("@", 1)[1] if "@" in v else re.sub(r"^[a-z]+://", "", v)
    host = re.split(r"[/?#:]", host, 1)[0]
    if host.startswith("www."):
        host = host[4:]
    if "." not in host or host in GENERIC_DOMAINS:
        return ""
    # a subdomain of a builder (mysite.wixsite.com) is just as generic
    if any(host.endswith("." + g) for g in GENERIC_DOMAINS):
        return ""
    return host


def _stamp_location_counts(conn, log=print):
    """Mark multi-site prospects and rescore them.

    Two leads are treated as one organisation when they share a normalized
    business name OR a non-generic email/website domain. The domain key is what
    finds corporate groups that brand every site differently — a chain whose
    Barrie and Airdrie clinics have unrelated names but mail from one domain is
    invisible to name matching, yet it is exactly the multi-site prospect an
    ICT offer wants most.

    Grouping spans the whole table rather than one run, so a single-city pull
    still links up with sites found in earlier pulls. A lead's count is the
    largest group it belongs to, counted in DISTINCT addresses so duplicate
    rows can't inflate it.
    """
    from collections import defaultdict
    rows = conn.execute(
        "SELECT id, business_name, address, email, website, location_count FROM leads"
    ).fetchall()

    addrs, members = defaultdict(set), defaultdict(list)
    for r in rows:
        addr = (r["address"] or "").strip().lower()
        keys = {("name", _norm_name(r["business_name"])),
                ("dom", _org_domain(r["email"])),
                ("dom", _org_domain(r["website"]))}
        for kind, key in keys:
            if not key:
                continue
            addrs[(kind, key)].add(addr)
            members[(kind, key)].append(r["id"])

    # Largest group each lead sits in wins: a name group of 2 inside a domain
    # group of 40 is a 40-site organisation.
    best = defaultdict(lambda: 1)
    for key, lead_ids in members.items():
        count = len(addrs[key])
        for lid in lead_ids:
            if count > best[lid]:
                best[lid] = count

    changed = [r["id"] for r in rows
               if best[r["id"]] != (r["location_count"] or 1)]
    if not changed:
        return 0
    for lid in changed:
        conn.execute("UPDATE leads SET location_count = ? WHERE id = ?", (best[lid], lid))
    conn.commit()
    # Each lead rescores under its OWN campaign's offer — the group can reach
    # across campaigns, so this run's rules don't apply to all of them.
    scoring.rescore_leads(conn, changed)
    multi = sum(1 for lid in changed if best[lid] >= 2)
    log(f"  multi-location: {multi} lead(s) marked multi-site "
        f"({len(changed)} rescored)")
    return multi


# Sweep rationing. A pass hands each participating location a share of what is
# still missing instead of letting the first one drain the whole target. The
# floor exists because a tiny query is poor value — Outscraper's fixed overhead
# per request doesn't shrink with the limit — so rather than asking ten cities
# for two leads each, a pass asks fewer cities for a worthwhile chunk and the
# next pass moves on to the others.
MIN_PER_LOCATION = 5
MAX_SWEEP_PASSES = 4
# Off-industry results before a location is written off for an industry. Arviat
# answers "dentist" with a coffee shop because it has no dentist; two such
# answers with nothing to show for them is enough to stop asking.
DEAD_MARKET_REJECTS = 2


def _dead_markets(conn, industry_slugs):
    """(city, industry) pairs that have produced rejects and never a lead.

    Retiring a market only for the current run means every later pull pays to
    rediscover it — and coverage ordering puts the emptiest regions first, so a
    market with no businesses of this kind gets asked FIRST, every time.
    Remembering it is what makes "tick everything" affordable: each dead market
    costs one query once, rather than one query per pull forever.
    """
    if not industry_slugs:
        return set()
    ph = ",".join("?" * len(industry_slugs))
    rows = conn.execute(
        f"SELECT r.city, r.industry, COUNT(*) AS n FROM pull_rejects r "
        f"WHERE r.industry IN ({ph}) AND r.reason = 'off_industry' "
        f"GROUP BY r.city, r.industry HAVING COUNT(*) >= ?",
        [*industry_slugs, DEAD_MARKET_REJECTS]).fetchall()
    dead = set()
    for r in rows:
        city = (r["city"] or "").split(",")[0].strip().lower()
        if not city:
            continue
        # A market that has ever yielded a real lead is not dead — it just has
        # noise mixed in, which the per-record filter already handles.
        got = conn.execute(
            f"SELECT 1 FROM leads WHERE LOWER(city) = ? AND industry IN ({ph}) LIMIT 1",
            [city, *industry_slugs]).fetchone()
        if not got:
            dead.add(city)
    return dead


def _plan_locations(conn, targets, industry_slugs):
    """Decide what order to visit the selected locations in.

    Two rules, both driven by the leads already held for this industry:

    Emptiest first — locations ticked on the dashboard arrive in the tree's
    alphabetical order and carry no db_id to timestamp, which is how three
    straight pulls can all land in whichever city sorts first. Ranking by
    existing coverage spreads leads ACROSS pulls, not just within one.

    Then one region at a time — a pass only visits a handful of locations, so
    without this "select all of Canada" would spend the whole target inside a
    single province. Provinces are dealt in turn, emptiest province first, so a
    pass samples the country and later pulls move on to the untouched regions.
    """
    if len(targets) < 2 or not industry_slugs:
        return targets
    # Drop markets already shown to have none of this industry. Done before
    # ordering, because ordering puts the emptiest first and a market with
    # nothing to find is the emptiest of all.
    dead = _dead_markets(conn, industry_slugs)
    if dead:
        live = [t for t in targets if (t["city"] or "").lower() not in dead]
        if live:                      # never filter the list down to nothing
            targets = live
    ph = ",".join("?" * len(industry_slugs))
    counts, region_counts = {}, {}
    for r in conn.execute(
            f"SELECT city, state, COUNT(*) AS n FROM leads "
            f"WHERE industry IN ({ph}) GROUP BY city, state", list(industry_slugs)):
        state = str(r["state"]).lower()
        counts[(str(r["city"]).lower(), state)] = r["n"]
        region_counts[state] = region_counts.get(state, 0) + r["n"]

    ordered = sorted(
        enumerate(targets),
        key=lambda it: (counts.get(((it[1]["city"] or "").lower(),
                                    (it[1]["state"] or "").lower()), 0), it[0]),
    )
    buckets = {}
    for i, t in ordered:
        buckets.setdefault((t["state"] or "").lower(), []).append((i, t))
    queues = [q for _, q in sorted(
        buckets.items(), key=lambda kv: (region_counts.get(kv[0], 0), kv[1][0][0]))]

    out = []
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0)[1])
    return out


def run_pull(industry_slugs, target, api_key, location=None, locations=None,
             run_id=None, campaign_id=None, source="maps", db_path=db.DB_FILE, log=print):
    """Execute one pull. Returns the number of leads added.

    industry_slugs: one slug, a comma-separated string, or a list of slugs.
    location: an optional dict {city, state, country} entered on the dashboard.
    locations: an optional LIST of such dicts — the pull sweeps every one of them
    (multi-city/state/country under one campaign). If neither is given it falls
    back to rotating the saved cities in Settings. Stops when `target` leads are in.

    Opens its own DB connection so it is safe to call from a background thread.
    If run_id is given, progress is written to that pull_runs row as it goes.
    """
    if isinstance(industry_slugs, str):
        industry_slugs = [s.strip() for s in industry_slugs.split(",") if s.strip()]

    conn = db.connect(db_path)
    try:
        industries = []
        for slug in industry_slugs:
            row = conn.execute(
                "SELECT * FROM industries WHERE slug = ?", (slug,)
            ).fetchone()
            if not row:
                raise RuntimeError(f"Unknown industry '{slug}'")
            chains = [
                r["name"] for r in conn.execute(
                    "SELECT name FROM chains WHERE industry_id = ?", (row["id"],)
                )
            ]
            industries.append((row, chains))
        if not industries:
            raise RuntimeError("No industry selected")

        # Where to pull: an explicit location entered on the dashboard, else the
        # saved-cities rotation. A "target" carries the query label + the geo to
        # stamp on leads; db_id is set only for saved cities (to touch last_pulled).
        loc_list = [l for l in (locations or ([location] if location else [])) if l]
        explicit = []
        for l in loc_list:
            c = (l.get("city") or "").strip()
            s = (l.get("state") or "").strip()
            co = (l.get("country") or "").strip()
            if c or s:
                label = ", ".join(p for p in (c, s, co) if p)
                explicit.append({"label": label, "city": c, "state": s,
                                 "country": co, "db_id": None})
        if explicit:
            targets = explicit
        else:
            rows = conn.execute(
                "SELECT * FROM cities WHERE enabled = 1 "
                "ORDER BY last_pulled_at IS NOT NULL, last_pulled_at, id"
            ).fetchall()
            if not rows:
                raise RuntimeError(
                    "Enter a City/State to pull, or add cities in Settings.")
            targets = [{"label": f"{r['name']}, {r['state']}", "city": r["name"],
                        "state": r["state"], "country": "", "db_id": r["id"]}
                       for r in rows]

        buffer_multiplier = float(db.get_setting(conn, "buffer_multiplier", "1.4"))
        enrich = db.get_setting(conn, "contact_enrichment", "1") == "1"
        validate = db.get_setting(conn, "phone_validation", "0") == "1"
        review_signals = db.get_setting(conn, "review_signals", "0") == "1"
        # This pull runs for one campaign (client engagement); score its leads
        # under that campaign's offer. campaign_id=None -> unassigned, default offer.
        camp_row = None
        if campaign_id:
            camp_row = conn.execute(
                "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        offer = db.offer_for_campaign(conn, camp_row)
        rules = scoring.load_rules(offer)
        today = str(date.today())
        added_total = 0
        places_seen = 0      # records Outscraper billed us for
        off_industry = 0     # returned, but not in the industry searched for
        query_errors = 0
        last_error = ""
        new_lead_ids = []
        cancelled = False

        targets = _plan_locations(conn, targets, industry_slugs)

        names = "+".join(s for s in industry_slugs)
        log(f"Target: {target} fresh {names} leads across {len(targets)} location(s)")

        # Interleaved sweep. Each pass spreads what's still missing over as many
        # locations as MIN_PER_LOCATION allows, so a 20-lead target across eight
        # cities comes back as a spread instead of 20 from whichever city sorts
        # first. Locations that return nothing new drop out; passes repeat until
        # the target is met, everyone is tapped out, or the cap is reached.
        exhausted, tried = set(), set()
        for sweep_pass in range(MAX_SWEEP_PASSES):
            if added_total >= target or _is_cancelled(conn, run_id):
                cancelled = _is_cancelled(conn, run_id)
                break
            live = [t for t in targets if t["label"] not in exhausted]
            if not live:
                log("  no location has fresh leads left for this industry")
                break

            remaining_pass = target - added_total
            share = max(MIN_PER_LOCATION, -(-remaining_pass // len(live)))
            # A location never asked yet is likelier to hold fresh businesses
            # than one we've already drawn from, so it goes first. Without this
            # the cap below keeps re-picking the head of the list and later
            # locations never get a turn.
            live.sort(key=lambda t: t["label"] in tried)
            # Asking more locations than the remainder supports just buys
            # records we'll throw away, so cap participation instead.
            live = live[:max(1, -(-remaining_pass // share))]
            if sweep_pass:
                log(f"  pass {sweep_pass + 1}: {remaining_pass} still needed, "
                    f"{len(live)} location(s) this pass")

            for target_loc in live:
                if added_total >= target or _is_cancelled(conn, run_id):
                    cancelled = _is_cancelled(conn, run_id)
                    break

                city_label = target_loc["label"]
                added_before_loc = added_total
                tried.add(city_label)
                # Set false by any query that came back full; a short answer
                # means the source had nothing more to give.
                loc_drained, loc_errored = True, False

                for industry, chain_names in industries:
                    if added_total >= target:
                        break
                    if added_total - added_before_loc >= share:
                        break  # this location has given its share for this pass
                    if _is_cancelled(conn, run_id):
                        cancelled = True
                        break

                    slug = industry["slug"]
                    progress_label = (
                        f"{city_label} ({slug})" if len(industries) > 1 else city_label
                    )
                    _update_run(conn, run_id, current_city=progress_label, added=added_total)

                    remaining = min(share - (added_total - added_before_loc),
                                    target - added_total)
                    pull_limit = max(MIN_PER_LOCATION, int(remaining * buffer_multiplier))
                    query_text = industry["query_template"].format(
                        industry=slug.replace("_", " "), city=city_label
                    )
                    log(f"Querying: '{query_text}' (up to {pull_limit})")

                    try:
                        places = []
                        if source in ("maps", "both"):
                            places += query_outscraper(api_key, query_text, pull_limit,
                                                       enrich_contacts=enrich)
                        if source in ("nppes", "both") and nppes.supports(slug):
                            # Free US provider registry. No website/reviews — those
                            # signals just don't fire for these leads.
                            places += nppes.query_nppes(
                                slug, city=target_loc["city"],
                                state=abbrev_state_code(target_loc["state"]),
                                limit=pull_limit)
                    except RuntimeError as e:
                        query_errors += 1
                        last_error = str(e)
                        loc_errored = True
                        log(f"  WARNING: {e}")
                        continue

                    if len(places) >= pull_limit:
                        loc_drained = False

                    added_this_query = 0
                    places_seen += len(places)
                    rejects_here = 0

                    def _reject(place_name, reason, category=""):
                        """Record a paid-for record we chose not to keep."""
                        conn.execute(
                            "INSERT INTO pull_rejects (run_id, business_name, category, "
                            "city, industry, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (run_id, place_name[:200], (category or "")[:120],
                             city_label, slug, reason, db.now_iso()))

                    for place in places:
                        if added_total >= target:
                            break

                        name = (place.get("name") or place.get("title") or "").strip()
                        phone = (place.get("phone") or place.get("phone_number") or "").strip()
                        category = place.get("type") or place.get("category") or ""

                        # NPPES occasionally has no phone. Rather than drop the lead,
                        # keep it under a unique NPI-based key and mark the phone
                        # invalid — dialable_leads() already excludes phone_valid=0,
                        # so it can't reach VICIdial until a number is found.
                        needs_phone = False
                        if not phone and place.get("npi"):
                            phone = f"NPI-{place['npi']}"
                            needs_phone = True
                        if not name:
                            continue
                        if not phone:
                            _reject(name, "no_phone", category)
                            continue
                        if is_chain(name, chain_names):
                            _reject(name, "chain", category)
                            continue
                        # Maps ranks rather than filters, so a thin market returns
                        # whatever is nearby. Calling a coffee shop about network
                        # infrastructure costs more than the lead is worth.
                        if not on_industry(slug, industry["label"], name, category):
                            off_industry += 1
                            rejects_here += 1
                            _reject(name, "off_industry", category)
                            log(f"  skipped off-industry: {name} [{category}]")
                            continue
                        biz_status = (place.get("business_status") or "").upper()
                        if "PERMANENTLY" in biz_status:
                            _reject(name, "closed", category)
                            continue  # dead business -> guaranteed non-connect

                        address = place.get("full_address") or place.get("address") or ""
                        website = place.get("site") or place.get("website") or ""
                        reviews = _as_int(place.get("reviews") or place.get("reviews_count"))
                        rating = _as_float(place.get("rating"))
                        # Outscraper: verified=False => "Claim this business" (unclaimed).
                        verified = place.get("verified")
                        unclaimed = 1 if verified is False else (0 if verified is True else None)
                        email = extract_email(place)

                        # Score via the active campaign, off the neutral signals.
                        # location_count is 1 here (single-location assumption); a
                        # post-sweep grouping pass fixes multi-site businesses + rescores.
                        signal_lead = {
                            "website": website, "email": email, "reviews": reviews,
                            "rating": rating, "unclaimed": unclaimed, "location_count": 1,
                            "city": place.get("city") or target_loc["city"],
                            "state": place.get("state") or place.get("region") or target_loc["state"],
                        }
                        score, hook = scoring.evaluate(signal_lead, rules)

                        cur = conn.execute(
                            "INSERT OR IGNORE INTO leads (phone, business_name, address, city, state, "
                            "website, category, industry, score, call_hook, pulled_date, "
                            "email, contact, postcode, search_query, run_id, campaign_id, phone_valid, "
                            "reviews, rating, unclaimed, street_address, maps_url, "
                            "country, facebook, business_status) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                phone, name, address,
                                place.get("city") or target_loc["city"],
                                place.get("state") or place.get("region") or target_loc["state"],
                                website,
                                place.get("type") or place.get("category") or slug,
                                slug, score, hook, today,
                                email,
                                extract_contact(place),
                                extract_postcode(place, address),
                                query_text, run_id, campaign_id,
                                0 if needs_phone else 1,
                                reviews, rating, unclaimed,
                                place.get("street") or "",
                                place.get("location_link") or place.get("url") or "",
                                place.get("country") or target_loc["country"],
                                place.get("facebook") or "",
                                biz_status,
                            ),
                        )
                        if cur.rowcount:  # 0 when the phone was already seen (dedupe)
                            added_total += 1
                            added_this_query += 1
                            new_lead_ids.append(cur.lastrowid)

                    conn.commit()
                    _update_run(conn, run_id, added=added_total)
                    log(f"  -> {added_this_query} new qualified leads ({slug}, {city_label})")
                    # A market that answers mostly with the wrong industry has no
                    # more of the right one — Maps was already reaching for
                    # whatever was nearby. Asking again just buys more of that,
                    # and we are billed per record returned, so retire it now.
                    if places and rejects_here > len(places) / 2:
                        exhausted.add(city_label)
                        log(f"  {city_label}: {rejects_here}/{len(places)} off-industry "
                            f"— no more {slug} here, dropping it")
                    time.sleep(1)  # be polite to the API between calls

                # Stop paying to re-ask a location with nothing left: either it
                # answered short (the source is out of matches) or a full answer
                # was all duplicates we already hold. A failed query proves
                # neither, so an errored location stays in the rotation.
                if not loc_errored and (loc_drained or added_total == added_before_loc):
                    exhausted.add(city_label)

                if target_loc["db_id"] is not None:
                    conn.execute(
                        "UPDATE cities SET last_pulled_at = ? WHERE id = ?",
                        (db.now_iso(), target_loc["db_id"]),
                    )
                conn.commit()

        if added_total == 0 and query_errors > 0:
            raise RuntimeError(f"All {query_errors} queries failed. Last error: {last_error}")

        # Multi-location scoring pass. Runs on every pull, not just multi-city
        # sweeps: domain grouping links these leads to sites found in earlier
        # runs, so even a one-city pull can expose a chain.
        if new_lead_ids:
            _stamp_location_counts(conn, log=log)

        # Suppress any freshly-pulled number already on the DNC list.
        blocked = dnc.scrub_leads(conn, new_lead_ids)
        if blocked:
            log(f"  DNC-suppressed {blocked} of the new leads")

        # Live site-quality probe only for offers that explicitly want it
        # (SEO, web design) — set by the offer's site_check flag.
        if offer and offer["site_check"]:
            check_new_websites(conn, new_lead_ids, run_id=run_id, log=log)

        # Review mining (opt-in, billed per review) -> new_in_market /
        # review_pain_match. Runs before the rescore below so scores include it.
        if review_signals and new_lead_ids and api_key:
            collect_review_signals(conn, new_lead_ids, api_key, log=log)
            scoring._rescore_rows(conn, conn.execute(
                f"SELECT * FROM leads WHERE id IN ({','.join('?' * len(new_lead_ids))})",
                new_lead_ids), rules)
            conn.commit()

        validation_note = ""
        if validate and new_lead_ids and api_key:
            rows = conn.execute(
                f"SELECT id, phone FROM leads WHERE id IN ({','.join('?' * len(new_lead_ids))})",
                new_lead_ids,
            ).fetchall()
            try:
                vsum = verify_lead_phones(conn, api_key, rows, run_id=run_id, log=log)
                validation_note = (f"; verified {vsum['checked']} phones, "
                                   f"{vsum['invalid']} bad")
            except RuntimeError as e:
                validation_note = f"; phone validation failed ({e})"
                log(f"  WARNING: {e}")

        if cancelled:
            message = f"Stopped by user after adding {added_total} leads{validation_note}"
            status = "cancelled"
        else:
            message = f"Added {added_total} fresh leads{validation_note}"
            if added_total < target:
                message += f" (target was {target} -- add more cities to hit it)"
        if places_seen:
            keep = round(100.0 * added_total / places_seen)
            message += (f" | {added_total} kept of {places_seen} records fetched "
                        f"({keep}% keep-rate)")
            if off_industry:
                message += f" | {off_industry} off-industry dropped"
            status = "done"
        _update_run(conn, run_id, status=status, finished_at=db.now_iso(),
                    added=added_total, current_city="", message=message)
        log(f"DONE. {message}")
        return added_total

    except Exception as e:
        _update_run(conn, run_id, status="error", finished_at=db.now_iso(),
                    message=str(e))
        raise
    finally:
        conn.close()


def main():
    db.init_db()
    conn = db.connect()
    default_industry = db.get_setting(conn, "default_industry", "hvac")
    default_target = int(db.get_setting(conn, "target_leads_per_day", "100"))
    conn.close()

    parser = argparse.ArgumentParser(description="Pull, filter, and rank fresh SEO leads.")
    parser.add_argument("--leads", type=int, default=default_target)
    parser.add_argument("--industry", default=default_industry,
                        help="one slug or comma-separated, e.g. hvac,plumbing")
    args = parser.parse_args()

    api_key = get_api_key()
    if not api_key:
        sys.exit("Set OUTSCRAPER_API_KEY (env var or .env file) first.")

    run_pull(args.industry, args.leads, api_key)


if __name__ == "__main__":
    main()
