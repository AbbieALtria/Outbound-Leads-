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


def name_from_email(email):
    """Derive a plausible person name from an email local-part, or '' if it looks
    like a role inbox (info@, sales@, ...) rather than a person."""
    local = email.split("@", 1)[0].lower()
    local = re.sub(r"\d+", "", local)  # drop trailing digits like john12
    if local in ROLE_ACCOUNTS or not local:
        return ""
    parts = [p for p in re.split(r"[._-]+", local) if p]
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
    inferred = name_from_email(extract_email(place))
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


def _stamp_location_counts(conn, run_id, rules, log=print):
    """After a multi-city sweep: group this run's leads by normalized business
    name; a name at 2+ DISTINCT addresses is a multi-site prospect. Stamp
    location_count on those leads, then rescore the run's leads so the
    multi_location signal is reflected in score + hook."""
    from collections import defaultdict
    rows = conn.execute(
        "SELECT id, business_name, address FROM leads WHERE run_id = ?", (run_id,)
    ).fetchall()
    addrs, ids = defaultdict(set), defaultdict(list)
    for r in rows:
        key = _norm_name(r["business_name"])
        if not key:
            continue
        addrs[key].add((r["address"] or "").strip().lower())
        ids[key].append(r["id"])
    stamped = 0
    for key, lead_ids in ids.items():
        count = len(addrs[key])
        if count >= 2:
            conn.execute(
                f"UPDATE leads SET location_count = ? WHERE id IN ({','.join('?' * len(lead_ids))})",
                [count, *lead_ids])
            stamped += len(lead_ids)
    if stamped:
        conn.commit()
        scoring._rescore_rows(conn, conn.execute(
            "SELECT * FROM leads WHERE run_id = ?", (run_id,)), rules)
        conn.commit()
        log(f"  multi-location: {stamped} lead(s) marked multi-site, rescored")
    return stamped


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
        query_errors = 0
        last_error = ""
        new_lead_ids = []
        cancelled = False

        names = "+".join(s for s in industry_slugs)
        log(f"Target: {target} fresh {names} leads across {len(targets)} location(s)")

        for target_loc in targets:
            if added_total >= target or _is_cancelled(conn, run_id):
                cancelled = _is_cancelled(conn, run_id)
                break

            city_label = target_loc["label"]

            for industry, chain_names in industries:
                if added_total >= target:
                    break
                if _is_cancelled(conn, run_id):
                    cancelled = True
                    break

                slug = industry["slug"]
                progress_label = (
                    f"{city_label} ({slug})" if len(industries) > 1 else city_label
                )
                _update_run(conn, run_id, current_city=progress_label, added=added_total)

                remaining = target - added_total
                pull_limit = max(5, int(remaining * buffer_multiplier))
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
                    log(f"  WARNING: {e}")
                    continue

                added_this_query = 0
                places_seen += len(places)
                for place in places:
                    if added_total >= target:
                        break

                    name = (place.get("name") or place.get("title") or "").strip()
                    phone = (place.get("phone") or place.get("phone_number") or "").strip()

                    # NPPES occasionally has no phone. Rather than drop the lead,
                    # keep it under a unique NPI-based key and mark the phone
                    # invalid — dialable_leads() already excludes phone_valid=0,
                    # so it can't reach VICIdial until a number is found.
                    needs_phone = False
                    if not phone and place.get("npi"):
                        phone = f"NPI-{place['npi']}"
                        needs_phone = True
                    if not name or not phone:
                        continue
                    if is_chain(name, chain_names):
                        continue
                    biz_status = (place.get("business_status") or "").upper()
                    if "PERMANENTLY" in biz_status:
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
                time.sleep(1)  # be polite to the API between calls

            if target_loc["db_id"] is not None:
                conn.execute(
                    "UPDATE cities SET last_pulled_at = ? WHERE id = ?",
                    (db.now_iso(), target_loc["db_id"]),
                )
            conn.commit()

        if added_total == 0 and query_errors > 0:
            raise RuntimeError(f"All {query_errors} queries failed. Last error: {last_error}")

        # Multi-location scoring pass: across a multi-city sweep, mark businesses
        # found at 2+ addresses as multi-site and rescore. Single-location pulls
        # (one target) keep location_count = 1, so nothing to do.
        if len(targets) >= 2 and new_lead_ids and run_id is not None:
            _stamp_location_counts(conn, run_id, rules, log=log)

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
