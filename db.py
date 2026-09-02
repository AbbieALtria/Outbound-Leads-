"""
Database layer for the SEO leads app.

One SQLite file (leads.db) replaces the old scattered state:
    seen_leads.sqlite3  -> leads table (phone is the unique dedupe key)
    cities.txt          -> cities table
    rotation_state.json -> cities.last_pulled_at (least-recently-pulled goes first)
    CHAIN_LISTS dict    -> industries + chains tables

First run migrates all of the old files automatically; the old files are
left in place untouched.
"""

import csv
import json
import os
import re
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# Postgres when DATABASE_URL is set, SQLite otherwise. Railway injects
# DATABASE_URL when a Postgres service is linked, so this switches backend with
# no code change — and unsetting it is a complete rollback.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))
# Set when Postgres was asked for but could not be used, so the reason is
# reported in the app instead of only existing in a crashed deploy's log.
POSTGRES_ERROR = ""
if POSTGRES:
    import pgbackend
    IntegrityError, DbError = pgbackend.IntegrityError, pgbackend.Error
else:
    IntegrityError, DbError = sqlite3.IntegrityError, sqlite3.Error


def fall_back_to_sqlite(reason):
    """Abandon Postgres for this process and carry on with the local file.

    Turning DATABASE_URL on should be a safe experiment, not a gamble with the
    live site: an unusable Postgres would otherwise crash the worker on import
    and take the app down, which is how the last attempt ended. Storage becomes
    temporary again — the banner already says so — but the site stays up and
    the reason is preserved for diagnosis.
    """
    global POSTGRES, POSTGRES_ERROR, IntegrityError, DbError
    POSTGRES, POSTGRES_ERROR = False, reason
    IntegrityError, DbError = sqlite3.IntegrityError, sqlite3.Error
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[storage] Postgres unusable, falling back to SQLite: {reason}", flush=True)
# DB lives in DATA_DIR when set (e.g. a Railway persistent volume like /data),
# otherwise next to the code for local use. Keeping it configurable is what makes
# hosted deploys survive redeploys.
DATA_DIR = Path(os.environ.get("DATA_DIR") or SCRIPT_DIR)


def _await_mount(target, timeout=None):
    """Wait for `target` to become a real mount point before using it.

    Railway logs "Mounting volume on: ..." in the same second gunicorn starts,
    and the process can win that race: it resolves the path, creates the
    directory itself, and ends up writing to container-local storage that the
    next deploy discards -- while a shell opened later sees a perfectly healthy
    volume. A mount is a different device from the filesystem holding the code,
    so poll for that rather than guessing at a fixed sleep.

    Bounded, and skipped entirely when DATA_DIR is unset (a local run, where the
    database is meant to live beside the code). Giving up is not fatal: the app
    still starts, and the Settings banner reports that storage is temporary.
    """
    if target == SCRIPT_DIR:
        return False
    timeout = float(os.environ.get("VOLUME_WAIT_SECONDS", timeout if timeout is not None else 25))
    deadline, base = time.monotonic() + timeout, SCRIPT_DIR.stat().st_dev
    while time.monotonic() < deadline:
        try:
            if target.stat().st_dev != base:
                return True
        except OSError:
            pass          # not there yet — the mount can create it
        time.sleep(0.5)
    return False


if POSTGRES:
    # Nothing is stored on disk, so there is no volume to wait for.
    _MOUNT_READY = True
    print("[storage] Postgres — no volume required", flush=True)
elif os.environ.get("DATA_DIR"):
    _MOUNT_READY = _await_mount(DATA_DIR)
    print(f"[storage] {DATA_DIR}: "
          + ("volume mounted" if _MOUNT_READY else
             "NO VOLUME after waiting — data here is temporary"), flush=True)
else:
    _MOUNT_READY = False

DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DATA_DIR / "leads.db"
# Beside the database, and only useful if it outlives a deploy — see record_boot.
BOOT_FILE = DATA_DIR / ".boots.json"


def record_boot():
    """Count boots in a file next to the database, so persistence can be PROVEN.

    That DATA_DIR is set proves nothing. The mkdir above happily creates the
    directory inside the container when no volume is mounted there, and the
    result is indistinguishable from real storage — writes succeed, the path
    looks right — until the next deploy throws the whole thing away. A counter
    that survives a restart is the only honest evidence, so a boot count still
    stuck at 1 after a redeploy means the storage is ephemeral.
    """
    info = {"first_boot": now_iso(), "boots": 0}
    try:
        if BOOT_FILE.exists():
            stored = json.loads(BOOT_FILE.read_text())
            if isinstance(stored, dict):
                info.update(stored)
        info["boots"] = int(info.get("boots", 0)) + 1
        info["last_boot"] = now_iso()
        BOOT_FILE.write_text(json.dumps(info))
    except (OSError, ValueError, TypeError):
        info["error"] = "could not write to the data directory"
    return info

# Old-format files, imported once if present
OLD_SEEN_DB = SCRIPT_DIR / "seen_leads.sqlite3"
OLD_CITIES_FILE = SCRIPT_DIR / "cities.txt"

# Structured sub-reasons for a 'not_interested' outcome, so the biggest bucket
# isn't a black box. Optional per lead — some calls have no clean answer.
NOT_INTERESTED_REASONS = [
    "already_has_provider",   # "we already use someone for this"
    "no_pain_identified",     # "we don't see a need"
    "brush_off",              # polite decline, no real engagement
    "bad_timing",             # "call back later / not right now"
    "wrong_contact",          # reached someone who can't decide
    "price_objection",        # cost came up as the blocker
    "other",
]

LEAD_STATUSES = ["new", "called", "interested", "appointment", "not_interested",
                 "callback", "dnc"]

# Offer presets. An OFFER is the scoring/pitch profile (what makes a lead "hot" +
# the call-hook wording); a CAMPAIGN (see the campaigns table) is a client
# engagement that USES one offer. Each offer scores leads off NEUTRAL signals (see
# scoring.py) — no SEO logic is hard-coded anywhere else. audience: b2b|b2c,
# goal: close|appointment. rules map a signal -> {points, hook}; {reviews}/{rating}
# interpolate in hooks.
OFFER_PRESETS = {
    "seo": {
        "name": "SEO / Rank Higher", "audience": "b2b", "goal": "close",
        "site_check": 1,
        "rules": {
            "no_website": {"points": 50, "hook": "No website -- invisible in search results"},
            "low_reviews": {"points": 20, "hook": "Only {reviews} reviews -- weak online reputation"},
            "few_reviews": {"points": 10, "hook": ""},
            "has_website": {"points": 10, "hook": ""},
        },
    },
    "listing_verification": {
        "name": "Listing Verification / GBP Claiming", "audience": "b2b", "goal": "close",
        "rules": {
            "unclaimed": {"points": 60, "hook": "Google listing is UNVERIFIED -- you don't control it and may be missing calls"},
            "no_website": {"points": 15, "hook": "No website -- your listing is your only web presence"},
            "low_reviews": {"points": 10, "hook": "Only {reviews} reviews"},
        },
    },
    "reputation": {
        "name": "Reputation Management", "audience": "b2b", "goal": "close",
        "rules": {
            "low_rating": {"points": 50, "hook": "{rating} star rating is sending customers to competitors"},
            "low_reviews": {"points": 30, "hook": "Only {reviews} reviews -- thin reputation"},
            "few_reviews": {"points": 15, "hook": "Only {reviews} reviews -- room to build trust"},
        },
    },
    "web_design": {
        "name": "Website Design / Build", "audience": "b2b", "goal": "close",
        "site_check": 1,
        "rules": {
            "no_website": {"points": 60, "hook": "No website -- customers can't find or trust you online"},
            "unclaimed": {"points": 10, "hook": "Listing unverified too"},
            "low_reviews": {"points": 10, "hook": ""},
        },
    },
    # B2B appointment-setting (e.g. TA Networks ICT: cloud comms, connectivity,
    # networking, cabling). Goal is a booked meeting for an AE, not a close. Targets
    # established multi-location businesses, so "no website" is NOT a buy signal here
    # — a real web/comms footprint is; scoring stays light and is tuned per campaign.
    "ict_appointment": {
        "name": "ICT / Business Communications — Appointment", "audience": "b2b",
        "goal": "appointment",
        # Keywords mined from customer reviews that signal comms pain (opt-in;
        # needs the review_signals setting on). Editable per offer in Settings.
        "pain_keywords": ["no answer", "on hold", "phone tree", "couldn't reach",
                          "hard to reach"],
        "rules": {
            "multi_location": {"points": 50, "hook": "{location_count} locations found — needs unified comms/network across sites"},
            # Each offer defines its OWN good-fit employee range (min/max live in
            # the rule, not hard-coded in the signal).
            "company_size_fit": {"points": 25, "min": 5, "max": 200,
                                 "hook": "{employee_count} employees — right size for a managed comms rollout"},
            "review_pain_match": {"points": 30, "hook": "Reviews mention reach/hold problems — direct comms pain"},
            "new_in_market":     {"points": 15, "hook": "New in market (first review {first_review_date}) — likely still choosing vendors"},
            "low_reviews":       {"points": 10, "hook": "Only {reviews} reviews — still growing, likely upgrading infrastructure"},
            "has_website":       {"points": 5,  "hook": "Established business — real comms/network footprint to modernize"},
        },
    },
    # --- B2C presets (framework). B2C leads come from a consumer-data source,
    # not Google Maps, so scoring rules stay empty until that source is wired in;
    # the campaign definitions exist now so the platform is B2C-ready. ---
    "solar": {"name": "Solar Installation", "audience": "b2c", "goal": "appointment", "rules": {}},
    "insurance_b2c": {"name": "Insurance", "audience": "b2c", "goal": "appointment", "rules": {}},
    "home_improvement": {"name": "Home Improvement", "audience": "b2c", "goal": "appointment", "rules": {}},
    "real_estate_b2c": {"name": "Real Estate Buyers/Sellers", "audience": "b2c", "goal": "appointment", "rules": {}},
    "mortgage": {"name": "Mortgage / Loan Inquiries", "audience": "b2c", "goal": "appointment", "rules": {}},
    "healthcare_appt": {"name": "Healthcare Appointments", "audience": "b2c", "goal": "appointment", "rules": {}},
    "auto_services_b2c": {"name": "Automotive Services", "audience": "b2c", "goal": "appointment", "rules": {}},
}
# The offer used to score leads that aren't attached to a campaign yet (legacy /
# unassigned leads). Kept under the old setting key name for back-compat.
DEFAULT_OFFER = "seo"

DEFAULT_QUERY_TEMPLATE = "{industry} in {city}"

# Bump this when SEED_INDUSTRIES gains new entries; init_db() then tops up an
# existing database once (INSERT OR IGNORE), preserving the user's own edits and
# not resurrecting industries they deleted after the previous version.
INDUSTRY_CATALOG_VERSION = 4

# Each industry carries its own search phrase (a natural Google Maps query) so
# the pull isn't locked to "... contractor in {city}". Users can edit the phrase
# or add their own industries in Settings. Chain lists exclude national brands
# whose local branches can't buy SEO.
SEED_INDUSTRIES = {
    # --- home trades ---
    "hvac": {"label": "HVAC", "query": "hvac contractor in {city}", "chains": [
        "One Hour Air", "Aire Serv", "Service Experts", "ARS Rescue Rooter",
        "Air Conditioning Express", "Sila Heating", "Horizon Services",
        "Lennox", "Trane Comfort Specialist", "American Residential Services",
        "Any Hour Services", "Del-Air", "TemperaturePro", "Mechanical One"]},
    "plumbing": {"label": "Plumbing", "query": "plumber in {city}", "chains": [
        "Roto-Rooter", "Mr. Rooter", "Benjamin Franklin Plumbing",
        "ARS Rescue Rooter", "American Leak Detection", "Any Hour Services"]},
    "electrician": {"label": "Electrician", "query": "electrician in {city}", "chains": [
        "Mister Sparky", "Puls", "One Hour Heating", "Any Hour Services"]},
    "roofing": {"label": "Roofing", "query": "roofing contractor in {city}", "chains": [
        "Erie Home", "Long Roofing", "Custom Exteriors", "1-800-HANSONS"]},
    "landscaping": {"label": "Landscaping", "query": "landscaping service in {city}", "chains": [
        "TruGreen", "The Grounds Guys", "U.S. Lawns", "Weed Man"]},
    "lawn_care": {"label": "Lawn Care", "query": "lawn care service in {city}", "chains": [
        "TruGreen", "Weed Man", "Lawn Doctor", "Sunday", "Spring-Green"]},
    "pest_control": {"label": "Pest Control", "query": "pest control service in {city}", "chains": [
        "Orkin", "Terminix", "Rollins", "Aptive", "Mosquito Joe", "Truly Nolen",
        "Massey Services", "Arrow Exterminators"]},
    "garage_door": {"label": "Garage Door", "query": "garage door repair in {city}", "chains": [
        "Precision Door", "Overhead Door", "A1 Garage Door"]},
    "locksmith": {"label": "Locksmith", "query": "locksmith in {city}", "chains": [
        "Pop-A-Lock", "The Flying Locksmiths"]},
    "painting": {"label": "Painting", "query": "painting contractor in {city}", "chains": [
        "CertaPro", "Five Star Painting", "360 Painting", "WOW 1 DAY PAINTING"]},
    "flooring": {"label": "Flooring", "query": "flooring contractor in {city}", "chains": [
        "Empire Today", "50 Floor", "Floor Coverings International"]},
    "fencing": {"label": "Fencing", "query": "fence contractor in {city}", "chains": []},
    "concrete": {"label": "Concrete", "query": "concrete contractor in {city}", "chains": []},
    "remodeling": {"label": "Remodeling", "query": "home remodeling contractor in {city}", "chains": [
        "Re-Bath", "Bath Fitter", "DreamMaker", "Renewal by Andersen"]},
    "handyman": {"label": "Handyman", "query": "handyman service in {city}", "chains": [
        "Ace Handyman", "Mr. Handyman", "Handyman Connection"]},
    "tree_service": {"label": "Tree Service", "query": "tree service in {city}", "chains": [
        "SavATree", "The Davey Tree"]},
    "pool_service": {"label": "Pool Service", "query": "pool cleaning service in {city}", "chains": [
        "ASP", "Leslie's", "Pinch A Penny", "America's Swimming Pool"]},
    "pressure_washing": {"label": "Pressure Washing", "query": "pressure washing service in {city}", "chains": [
        "Men In Kilts", "Window Genie"]},
    "gutter": {"label": "Gutter", "query": "gutter installation service in {city}", "chains": [
        "LeafFilter", "Leaf Home", "LeafGuard", "Gutter Helmet"]},
    "solar": {"label": "Solar", "query": "solar panel installer in {city}", "chains": [
        "SunRun", "Tesla", "Sunpower", "Momentum Solar", "ADT Solar", "Trinity Solar"]},
    "cleaning": {"label": "Cleaning Service", "query": "house cleaning service in {city}", "chains": [
        "Merry Maids", "Molly Maid", "The Cleaning Authority", "MaidPro",
        "Two Maids", "You've Got Maids"]},
    "carpet_cleaning": {"label": "Carpet Cleaning", "query": "carpet cleaning service in {city}", "chains": [
        "Stanley Steemer", "Chem-Dry", "Zerorez", "COIT"]},
    "junk_removal": {"label": "Junk Removal", "query": "junk removal service in {city}", "chains": [
        "1-800-GOT-JUNK", "College Hunks", "JDog", "LoadUp"]},
    "moving": {"label": "Moving Company", "query": "moving company in {city}", "chains": [
        "Two Men and a Truck", "College Hunks", "U-Haul", "PODS", "United Van Lines",
        "Allied", "Mayflower", "Bekins"]},
    "restoration": {"label": "Water Damage Restoration", "query": "water damage restoration in {city}", "chains": [
        "SERVPRO", "ServiceMaster", "Servpro", "PuroClean", "Rainbow Restoration",
        "911 Restoration", "BELFOR"]},
    "appliance_repair": {"label": "Appliance Repair", "query": "appliance repair service in {city}", "chains": [
        "Mr. Appliance", "Sears Home Services", "Puls"]},
    "chimney": {"label": "Chimney Sweep", "query": "chimney sweep service in {city}", "chains": []},
    "septic": {"label": "Septic Service", "query": "septic tank service in {city}", "chains": []},
    # --- auto ---
    "auto_repair": {"label": "Auto Repair", "query": "auto repair shop in {city}", "chains": [
        "Firestone", "Midas", "Jiffy Lube", "Meineke", "Walmart Auto Care",
        "AutoZone", "Advance Auto Parts", "O'Reilly", "Pep Boys", "Take 5",
        "Valvoline", "NTB", "Big O Tires", "Discount Tire", "Mavis", "Christian Brothers",
        "Tuffy", "Monro", "Grease Monkey", "Caliber"]},
    "auto_body": {"label": "Auto Body Shop", "query": "auto body shop in {city}", "chains": [
        "Caliber Collision", "Gerber Collision", "Maaco", "CARSTAR", "Service King",
        "Crash Champions", "Abra"]},
    "auto_detailing": {"label": "Auto Detailing", "query": "auto detailing service in {city}", "chains": []},
    "tire_shop": {"label": "Tire Shop", "query": "tire shop in {city}", "chains": [
        "Discount Tire", "Big O Tires", "NTB", "Firestone", "Mavis", "Tire Kingdom",
        "Les Schwab", "Goodyear", "America's Tire"]},
    "towing": {"label": "Towing", "query": "towing service in {city}", "chains": ["AAA"]},
    # --- professional / health (B2B SEO buyers too) ---
    "dentist": {"label": "Dentist", "query": "dentist in {city}", "chains": [
        "Aspen Dental", "Western Dental", "Heartland Dental", "Pacific Dental",
        "Great Expressions", "Dental Care Alliance"]},
    "chiropractor": {"label": "Chiropractor", "query": "chiropractor in {city}", "chains": [
        "The Joint Chiropractic"]},
    "med_spa": {"label": "Med Spa", "query": "medical spa in {city}", "chains": [
        "Ideal Image", "SkinSpirit", "LaserAway"]},
    "veterinarian": {"label": "Veterinarian", "query": "veterinarian in {city}", "chains": [
        "Banfield", "VCA", "BluePearl", "Thrive"]},
    "law_firm": {"label": "Law Firm", "query": "law firm in {city}", "chains": [
        "Morgan & Morgan", "Jacoby & Meyers"]},
    "accountant": {"label": "Accountant", "query": "accounting firm in {city}", "chains": [
        "H&R Block", "Jackson Hewitt", "Liberty Tax"]},
    "insurance_agency": {"label": "Insurance Agency", "query": "insurance agency in {city}", "chains": [
        "State Farm", "Allstate", "Farmers", "Geico", "Nationwide", "Liberty Mutual"]},
    "real_estate": {"label": "Real Estate Agency", "query": "real estate agency in {city}", "chains": [
        "RE/MAX", "Keller Williams", "Coldwell Banker", "Century 21", "eXp",
        "Berkshire Hathaway", "Compass", "Sotheby's"]},
    # --- B2B ICT / multi-location targets (e.g. TA Networks appointment-setting) ---
    "hotel": {"label": "Hotel", "query": "hotel in {city}", "chains": [
        "Marriott", "Hilton", "Holiday Inn", "Best Western", "Comfort Inn",
        "Fairfield Inn", "Days Inn", "Super 8"]},
    "medical_clinic": {"label": "Medical Clinic", "query": "medical clinic in {city}", "chains": []},
    "physiotherapy": {"label": "Physiotherapy Clinic", "query": "physiotherapy clinic in {city}", "chains": []},
    "consulting_firm": {"label": "Consulting Firm", "query": "consulting firm in {city}", "chains": []},
    "pharmacy": {"label": "Pharmacy", "query": "pharmacy in {city}", "chains": [
        "Shoppers Drug Mart", "Rexall", "London Drugs", "Jean Coutu", "Pharmasave",
        "CVS", "Walgreens", "Rite Aid"]},
    "optometrist": {"label": "Optometrist", "query": "optometrist in {city}", "chains": [
        "LensCrafters", "Pearle Vision", "Specsavers", "Hakim Optical", "FYidoctors",
        "Visionworks", "America's Best"]},
    "urgent_care": {"label": "Urgent Care Clinic", "query": "urgent care clinic in {city}", "chains": [
        "MedExpress", "CityMD", "NextCare", "AFC Urgent Care", "Concentra"]},
    # Eye-movement / binocular-vision specialty — distinct from general optometry.
    "orthoptics": {"label": "Orthoptics Clinic", "query": "orthoptist in {city}", "chains": []},
    "car_dealership": {"label": "Car Dealership", "query": "car dealership in {city}", "chains": []},
    "furniture_store": {"label": "Furniture Store", "query": "furniture store in {city}", "chains": [
        "IKEA", "Ashley", "Leon's", "The Brick"]},
}

DEFAULT_SETTINGS = {
    # Pre-filled lead-count default on the dashboard. Kept small so a first test
    # pull for a new campaign doesn't burn credits before anyone checks quality;
    # the user can type a larger number for any pull.
    "target_leads_per_day": "20",
    # Extra records fetched per lead wanted. Outscraper bills per record
    # RETURNED, so 2.0 meant paying for ~2x what we keep. Tunable in Settings;
    # each pull reports its keep-rate so this can be lowered with evidence.
    "buffer_multiplier": "1.4",
    "default_industry": "hvac",
    # Ask Outscraper for emails + contact people (decision-makers). Costs extra
    # Outscraper credits per lead; turn off in Settings if not worth it.
    "contact_enrichment": "1",
    # Validate each new lead's phone (line type + drop dead numbers) during the
    # pull. Costs extra Outscraper credits per number; off by default.
    "phone_validation": "0",
    # Mine each new lead's Google reviews (earliest review date + recent review
    # text) to power the new_in_market / review_pain_match signals. Outscraper
    # prices reviews SEPARATELY (~$3 per 1,000 reviews); off by default.
    "review_signals": "0",
    # Drop VOIP + toll-free numbers from dial exports (better B2B connect rate).
    "drop_voip_export": "0",
    # Daily requeue job time, EST (HH:MM). Reads VICIdial dispositions.
    "requeue_run_time": "23:30",
    # Apollo B2B contact enrichment (decision-maker name/title/email/direct dial).
    # Name + title are free; email reveal = 1 credit, phone reveal = 8 credits each,
    # so reveal_phone is OFF by default. Enrichment only runs when APOLLO_API_KEY is set.
    "enrich_reveal_email": "1",
    "enrich_reveal_phone": "0",
    # Offer used to score leads with no campaign (legacy/unassigned). The key name
    # is historical ("active_campaign"); it now holds the default OFFER slug.
    "active_campaign": DEFAULT_OFFER,
}

# Columns added after the first release; init_db() adds them to old databases.
LEAD_EXTRA_COLUMNS = {
    "email": "TEXT NOT NULL DEFAULT ''",
    "contact": "TEXT NOT NULL DEFAULT ''",       # decision-maker name if found (Apollo)
    "contact_title": "TEXT NOT NULL DEFAULT ''", # their job title (e.g. IT Manager)
    "direct_phone": "TEXT NOT NULL DEFAULT ''",  # decision-maker direct dial / mobile (revealed)
    "postcode": "TEXT NOT NULL DEFAULT ''",
    "search_query": "TEXT NOT NULL DEFAULT ''",  # exact query the lead came from
    "phone_type": "TEXT NOT NULL DEFAULT ''",    # fixed line / mobile / voip / invalid
    # 1 = assumed good until validated; set to 0 only when validation says invalid
    "phone_valid": "INTEGER NOT NULL DEFAULT 1",
    "validated_at": "TEXT",
    "run_id": "INTEGER",  # the pull_runs row this lead came from (NULL = legacy/import)
    # The campaign (client engagement) this lead belongs to. NULL = legacy /
    # unassigned (shown under the house client, scored under the default offer).
    "campaign_id": "INTEGER",
    # Neutral campaign signals (NULL = unknown, e.g. legacy leads). Any campaign
    # scores off these instead of anything SEO-specific being hard-coded.
    "reviews": "INTEGER",
    "rating": "REAL",
    "unclaimed": "INTEGER",  # 1 = Google listing unclaimed (Outscraper verified=False)
    # How many distinct locations this business was found at in its pull sweep
    # (>=2 => multi-site; powers the ICT multi_location signal). 1 = single-location.
    "location_count": "INTEGER NOT NULL DEFAULT 1",
    # B2B intent signals matched onto a lead from an external source (site-visitor
    # trackers like Leadfeeder/Albacross; intent tools like Bombora/G2).
    "site_visitor": "INTEGER NOT NULL DEFAULT 0",
    "site_visit_count": "INTEGER",
    "site_last_visit_date": "TEXT",
    "intent_topic": "TEXT NOT NULL DEFAULT ''",
    "intent_last_seen_date": "TEXT",
    # Company firmographics from Apollo's Organization Enrichment (1 credit/org).
    "employee_count": "INTEGER",
    "company_revenue": "TEXT",
    "company_industry": "TEXT",
    # Review mining (opt-in `review_signals`; Outscraper reviews cost per review).
    # Why a lead said no (one of NOT_INTERESTED_REASONS); only set while
    # status = not_interested.
    "not_interested_reason": "TEXT",
    "first_review_date": "TEXT",
    "review_text_sample": "TEXT",
    # Market + provenance. B2B leads are pulled from Maps; B2C leads arrive via
    # the intake API / CSV import and MUST carry consent for compliant calling.
    "market_type": "TEXT NOT NULL DEFAULT 'b2b'",       # b2b | b2c
    "lead_source": "TEXT NOT NULL DEFAULT ''",          # pull | api:<vendor> | csv:<file>
    "consent_status": "TEXT NOT NULL DEFAULT ''",       # opted_in | unknown | ...
    "consent_at": "TEXT",                                # when/how consent was given
    "product_interest": "TEXT NOT NULL DEFAULT ''",     # B2C: what they asked about
    "preferred_contact_time": "TEXT NOT NULL DEFAULT ''",
    # Extra fields their VICIdial upload uses (the red-tagged columns).
    "street_address": "TEXT NOT NULL DEFAULT ''",       # street line only (Outscraper 'street')
    "maps_url": "TEXT NOT NULL DEFAULT ''",             # Google Maps place link
    "country": "TEXT NOT NULL DEFAULT ''",              # for geo filtering
    "facebook": "TEXT NOT NULL DEFAULT ''",             # Facebook page URL if listed
    "business_status": "TEXT NOT NULL DEFAULT ''",      # OPERATIONAL / CLOSED_* (connect-rate)
}

# Extra columns on pull_runs added after first release.
PULL_RUN_EXTRA_COLUMNS = {
    "cancel": "INTEGER NOT NULL DEFAULT 0",
    "campaign_id": "INTEGER",   # the campaign this pull ran for (NULL = ad-hoc/legacy)
    "user_id": "INTEGER",       # who ran the pull (for per-user quotas; NULL = system/legacy)
    # Optional batch-level quality review (pre-dial list quality, NOT call outcome).
    "user_rating": "TEXT",      # 'good' | 'bad' | NULL (not rated)
    "user_comment": "TEXT",
}

# Per-user limits (admin-set). 0 = unlimited; allowed_campaigns '' = all campaigns.
USER_EXTRA_COLUMNS = {
    "lead_limit_total": "INTEGER NOT NULL DEFAULT 0",
    "lead_limit_daily": "INTEGER NOT NULL DEFAULT 0",
    "allowed_campaigns": "TEXT NOT NULL DEFAULT ''",  # comma-separated campaign ids
    # 1 = must set a new password at next login (set on create + admin reset).
    "must_change_password": "INTEGER NOT NULL DEFAULT 0",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    phone TEXT UNIQUE NOT NULL,
    business_name TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL DEFAULT '',
    score INTEGER NOT NULL DEFAULT 0,
    call_hook TEXT NOT NULL DEFAULT '',
    pulled_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    notes TEXT NOT NULL DEFAULT '',
    status_updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_leads_pulled_date ON leads(pulled_date);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);

CREATE TABLE IF NOT EXISTS cities (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    state TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_pulled_at TEXT,
    UNIQUE(name, state)
);

CREATE TABLE IF NOT EXISTS industries (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    label TEXT NOT NULL,
    query_template TEXT NOT NULL DEFAULT '{industry} contractor in {city}',
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS chains (
    id INTEGER PRIMARY KEY,
    industry_id INTEGER NOT NULL REFERENCES industries(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    UNIQUE(industry_id, name)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pull_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    industry TEXT NOT NULL DEFAULT '',
    target INTEGER NOT NULL DEFAULT 0,
    added INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    current_city TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'agent',      -- admin | agent
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS dnc_numbers (
    phone TEXT PRIMARY KEY,                  -- 10-digit, normalized
    source TEXT NOT NULL DEFAULT '',         -- upload | call_log | manual | federal | litigator
    reason TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL
);

-- Leads that came back not-reached and are queued for redial. attempt_count is
-- VICIdial's called_count (source of truth), not a counter we maintain.
CREATE TABLE IF NOT EXISTS requeue_leads (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    last_disposition TEXT NOT NULL DEFAULT '',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'active',     -- active | exhausted | excluded
    campaign TEXT NOT NULL DEFAULT '',        -- VICIdial campaign_id
    batch_date TEXT NOT NULL DEFAULT '',      -- EST disposition day (YYYY-MM-DD), the redial-batch key
    updated_at TEXT NOT NULL,
    UNIQUE(lead_id)
);

-- Not-interested (YPNI) numbers on a time-boxed cooldown (a temporary DNC).
CREATE TABLE IF NOT EXISTS suppressed_leads (
    phone TEXT PRIMARY KEY,                   -- 10-digit, normalized
    reason TEXT NOT NULL DEFAULT '',
    cooldown_until TEXT NOT NULL,
    added_at TEXT NOT NULL
);

-- B2B intent signals (site-visitor / research-intent) that couldn't be matched
-- to an existing lead — kept for review / backfill, never silently dropped.
CREATE TABLE IF NOT EXISTS unmatched_signals (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                       -- site_visitor | intent
    company_name TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT '',
    signal_strength TEXT NOT NULL DEFAULT '',
    last_seen_date TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- In-app alerts (e.g. a new regenerated list is ready to upload to VICIdial).
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT '',            -- e.g. 'regen_list'
    message TEXT NOT NULL,
    link TEXT NOT NULL DEFAULT '',            -- where to act on it
    created_at TEXT NOT NULL,
    seen INTEGER NOT NULL DEFAULT 0
);

-- Every record Outscraper returned that we chose NOT to keep, and why. These
-- are already paid for, so recording them costs nothing and makes the filters
-- auditable: a wrongly-dropped real business is invisible otherwise, and the
-- only way to trust an automatic filter is to be able to read what it rejected.
CREATE TABLE IF NOT EXISTS pull_rejects (
    id INTEGER PRIMARY KEY,
    run_id INTEGER,
    business_name TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',          -- off_industry | chain | closed | no_phone
    created_at TEXT NOT NULL DEFAULT ''
);

-- An OFFER is the scoring/pitch profile (what makes a lead hot + call-hook
-- wording). Was previously named "campaigns"; _migrate_campaigns_to_offers()
-- renames the old table in place before this runs.
CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    audience TEXT NOT NULL DEFAULT 'b2b',   -- b2b | b2c
    goal TEXT NOT NULL DEFAULT 'close',      -- close | appointment
    rules TEXT NOT NULL DEFAULT '{}',        -- JSON: {signal: {points, hook}}
    is_preset INTEGER NOT NULL DEFAULT 0,
    site_check INTEGER NOT NULL DEFAULT 0,   -- run live website-quality probe during pull?
    enabled INTEGER NOT NULL DEFAULT 1       -- show as a usable offer?
);

-- A CLIENT owns campaigns (the paying customer the leads are generated for).
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    contact_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT ''
);

-- A CAMPAIGN is a client engagement: it bundles a client + an offer + a target
-- industry + geography, and maps to one VICIdial campaign_id so redial cycles
-- stay tied to the same client with full history. Industry/geo are attributes
-- OF the campaign, never picked independently of it.
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    offer_slug TEXT NOT NULL DEFAULT '',      -- REFERENCES offers(slug)
    audience TEXT NOT NULL DEFAULT 'b2b',     -- copied from the offer at create time
    industry_slug TEXT NOT NULL DEFAULT '',   -- REFERENCES industries(slug); '' for B2C
    country TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    vici_campaign_id TEXT NOT NULL DEFAULT '',-- the VICIdial campaign_id (redial bridge)
    status TEXT NOT NULL DEFAULT 'active',    -- active | paused | archived
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_campaigns_vici ON campaigns(vici_campaign_id);

"""

# Extra columns on offers (the old campaigns table) added after its first release.
OFFER_EXTRA_COLUMNS = {
    "site_check": "INTEGER NOT NULL DEFAULT 0",
    "enabled": "INTEGER NOT NULL DEFAULT 1",
    # JSON list of review keywords that signal this offer's pain (review_pain_match).
    "pain_keywords": "TEXT NOT NULL DEFAULT '[]'",
}

# Extra columns on requeue_leads added after first release.
REQUEUE_EXTRA_COLUMNS = {
    "campaign": "TEXT NOT NULL DEFAULT ''",   # VICIdial campaign_id the disposition came from
    "batch_date": "TEXT NOT NULL DEFAULT ''", # EST disposition day — the redial-batch key
}

# Extra columns on clients added after the table's first release.
CLIENT_EXTRA_COLUMNS = {
    "address": "TEXT NOT NULL DEFAULT ''",
    "website": "TEXT NOT NULL DEFAULT ''",
}

# Market types a campaign can target.
MARKET_TYPES = ["b2b", "b2c", "hybrid"]


def connect(db_path=DB_FILE):
    if POSTGRES:
        # db_path is meaningless for a networked database; ignored so every
        # existing call site keeps working unchanged.
        return pgbackend.connect(DATABASE_URL)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Wait instead of erroring when another thread holds a write lock (the
    # threaded server + background pull can contend on the single SQLite file).
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def table_columns(conn, table):
    """Column names of `table`, whichever backend is in use."""
    if POSTGRES:
        return pgbackend.table_columns(conn, table)
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def table_names(conn):
    if POSTGRES:
        return pgbackend.table_names(conn)
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def default_offer_slug(conn):
    """Offer slug used to score leads with no campaign (setting key is historical)."""
    return get_setting(conn, "active_campaign", DEFAULT_OFFER)


def get_offer(conn, slug):
    return conn.execute("SELECT * FROM offers WHERE slug = ?", (slug,)).fetchone()


def offer_for_campaign(conn, campaign):
    """The offer row a campaign scores under; falls back to the default offer."""
    slug = ""
    if campaign is not None:
        try:
            slug = campaign["offer_slug"] or ""
        except (KeyError, IndexError):
            slug = ""
    slug = slug or default_offer_slug(conn)
    return get_offer(conn, slug) or get_offer(conn, DEFAULT_OFFER)


def campaign_by_vici(conn, vici_id):
    """The campaign engagement mapped to a VICIdial campaign_id, or None."""
    vici_id = (vici_id or "").strip()
    if not vici_id:
        return None
    return conn.execute(
        "SELECT * FROM campaigns WHERE vici_campaign_id = ? AND vici_campaign_id != '' "
        "LIMIT 1", (vici_id,)).fetchone()


def add_alert(conn, message, kind="", link=""):
    conn.execute(
        "INSERT INTO alerts (kind, message, link, created_at, seen) VALUES (?, ?, ?, ?, 0)",
        (kind, message, link, now_iso()),
    )


def unseen_alerts(conn):
    return conn.execute(
        "SELECT id, kind, message, link, created_at FROM alerts WHERE seen = 0 "
        "ORDER BY created_at DESC").fetchall()


def unseen_alert_count(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM alerts WHERE seen = 0").fetchone()["n"]


def mark_alert_seen(conn, alert_id):
    conn.execute("UPDATE alerts SET seen = 1 WHERE id = ?", (alert_id,))


def init_db(db_path=DB_FILE):
    """Create schema, seed defaults, and run the one-time migration."""
    conn = connect(db_path)
    # Rename the old offer table (campaigns->offers) BEFORE the schema runs, so the
    # CREATE TABLE for the new engagement `campaigns` builds a fresh table.
    _migrate_campaigns_to_offers(conn)
    conn.executescript(SCHEMA)
    _ensure_lead_columns(conn)
    _ensure_columns(conn, "pull_runs", PULL_RUN_EXTRA_COLUMNS)
    _ensure_columns(conn, "offers", OFFER_EXTRA_COLUMNS)
    _ensure_columns(conn, "requeue_leads", REQUEUE_EXTRA_COLUMNS)
    _ensure_columns(conn, "clients", CLIENT_EXTRA_COLUMNS)
    _ensure_columns(conn, "users", USER_EXTRA_COLUMNS)
    # Indexes on the hot filter columns. Created AFTER _ensure_columns because
    # run_id/campaign_id are migration-added, not part of the base schema.
    for ddl in (
        "CREATE INDEX IF NOT EXISTS idx_leads_run ON leads(run_id)",
        "CREATE INDEX IF NOT EXISTS idx_leads_campaign ON leads(campaign_id)",
        "CREATE INDEX IF NOT EXISTS idx_runs_campaign ON pull_runs(campaign_id)",
    ):
        conn.execute(ddl)

    for key, value in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))

    # One-time: lower the old 2.0 fetch buffer, which billed for ~2x the leads
    # kept. Runs once; a value the user has since chosen is left alone.
    if get_setting(conn, "buffer_default_v2", "") != "done":
        if get_setting(conn, "buffer_multiplier", "") == "2.0":
            set_setting(conn, "buffer_multiplier", "1.4")
        set_setting(conn, "buffer_default_v2", "done")

    _seed_industries(conn)
    _seed_offers(conn)
    _seed_default_client(conn)
    _seed_ta_networks(conn)
    _migrate_cities(conn)
    _migrate_leads(conn)
    _backfill_lead_fields(conn)
    _backfill_run_ids(conn)
    _backfill_reviews(conn)

    # A run left in 'running' means the app was killed mid-pull last time.
    conn.execute(
        "UPDATE pull_runs SET status = 'error', finished_at = ?, "
        "message = 'Interrupted: app was stopped during this run' WHERE status = 'running'",
        (now_iso(),),
    )

    conn.commit()
    conn.close()


def _ensure_columns(conn, table, columns):
    existing = table_columns(conn, table)
    for col, ddl in columns.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def _migrate_campaigns_to_offers(conn):
    """One-time, idempotent: the table historically called `campaigns` actually
    held OFFERS (scoring profiles). Rename it to `offers` so the name `campaigns`
    is free for the new client-engagement table. Only fires when an old-shape
    campaigns table exists (has a `rules` column) and `offers` doesn't yet."""
    tables = table_names(conn)
    if "offers" in tables or "campaigns" not in tables:
        return
    cols = table_columns(conn, "campaigns")
    if "rules" in cols:                       # old offer shape -> rename in place
        conn.execute("ALTER TABLE campaigns RENAME TO offers")


def _seed_offers(conn):
    import json
    for slug, info in OFFER_PRESETS.items():
        conn.execute(
            "INSERT OR IGNORE INTO offers (slug, name, audience, goal, rules, is_preset, "
            "site_check, pain_keywords) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (slug, info["name"], info["audience"], info["goal"],
             json.dumps(info["rules"]), info.get("site_check", 0),
             json.dumps(info.get("pain_keywords", []))),
        )
    _refresh_preset_offers(conn)


# Bump when OFFER_PRESETS' rules change, to push the new rules onto the PRESET
# offers of an existing database once (user-created offers are never touched).
OFFER_PRESET_VERSION = 2


def _refresh_preset_offers(conn):
    """One-time-per-version refresh of preset offers' rules + pain_keywords, so a
    preset's scoring change reaches databases seeded before it. Only rows with
    is_preset = 1 are touched."""
    import json
    if int(get_setting(conn, "offer_preset_version", "0") or 0) >= OFFER_PRESET_VERSION:
        return
    for slug, info in OFFER_PRESETS.items():
        conn.execute(
            "UPDATE offers SET rules = ?, pain_keywords = ? WHERE slug = ? AND is_preset = 1",
            (json.dumps(info["rules"]), json.dumps(info.get("pain_keywords", [])), slug),
        )
    set_setting(conn, "offer_preset_version", str(OFFER_PRESET_VERSION))


def _seed_default_client(conn):
    """A house client so legacy/unassigned leads have an owner to show under."""
    conn.execute(
        "INSERT OR IGNORE INTO clients (id, name, contact_name, created_at) "
        "VALUES (1, 'Unassigned (house)', '', ?)",
        (now_iso(),),
    )


def _seed_ta_networks(conn):
    """One-time setup for the TA Networks engagement (Canadian ICT provider,
    Mississauga; B2B appointment-setting per their dialer playbook). Creates the
    client + the four playbook campaigns (retail, hospitality, professional/legal,
    clinics). Guarded by a flag so it seeds ONCE and never resurrects if the user
    edits or deletes it. Industry/geo are optional defaults — the pull picks them."""
    if get_setting(conn, "seed_ta_networks_v1", "") == "done":
        return
    now = now_iso()
    cur = conn.execute(
        "INSERT INTO clients (name, contact_name, email, phone, website, address, notes, enabled, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
        ("TA Networks", "Ganshan", "", "1-877-362-6826", "https://tanetworks.ca",
         "Mississauga, Ontario, Canada",
         "Canadian ICT: HELLO cloud comms, connectivity (internet/LTE-5G failover), "
         "networking (Fortinet/Wi-Fi/SD-WAN), cabling. B2B appointment-setting for AEs. "
         "CRTC / Canadian National DNC rules apply.", now),
    )
    client_id = cur.lastrowid
    # (campaign name, default industry_slug) — the four playbook verticals.
    for name, industry in (
        ("TA Networks — Multi-location Retail", "car_dealership"),
        ("TA Networks — Hospitality & Hotels", "hotel"),
        ("TA Networks — Professional Services & Legal", "law_firm"),
        ("TA Networks — Clinics & Healthcare", "dentist"),
    ):
        conn.execute(
            "INSERT INTO campaigns (name, client_id, offer_slug, audience, industry_slug, "
            "country, state, city, vici_campaign_id, status, created_at) "
            "VALUES (?, ?, 'ict_appointment', 'b2b', ?, 'Canada', 'Ontario', 'Toronto', '', 'active', ?)",
            (name, client_id, industry, now),
        )
    set_setting(conn, "seed_ta_networks_v1", "done")


def _backfill_reviews(conn):
    """Legacy leads stored review counts only inside call_hook text
    ("Only 9 reviews -- ..."). Recover the number into the reviews column so
    campaign scoring can use it. Best-effort; leaves rating/unclaimed NULL."""
    rows = conn.execute(
        "SELECT id, call_hook FROM leads WHERE reviews IS NULL AND call_hook LIKE '%reviews%'"
    ).fetchall()
    for row in rows:
        m = re.search(r"Only (\d+) reviews", row["call_hook"])
        if m:
            conn.execute("UPDATE leads SET reviews = ? WHERE id = ?",
                         (int(m.group(1)), row["id"]))


def _ensure_lead_columns(conn):
    _ensure_columns(conn, "leads", LEAD_EXTRA_COLUMNS)


def _backfill_lead_fields(conn):
    """Fill search_query and postcode on rows created before those columns existed."""
    conn.execute(
        "UPDATE leads SET search_query = REPLACE(industry, '_', ' ') || "
        "' contractor in ' || city || ', ' || state "
        "WHERE search_query = '' AND industry != '' AND city != '' AND state != ''"
    )
    # Rows migrated from old CSVs have no industry; fall back to their category.
    conn.execute(
        "UPDATE leads SET search_query = LOWER(category) || ' in ' || city || ', ' || state "
        "WHERE search_query = '' AND category != '' AND city != '' AND state != ''"
    )
    rows = conn.execute(
        "SELECT id, address FROM leads WHERE postcode = '' AND address != ''"
    ).fetchall()
    for row in rows:
        m = re.search(r"\b(\d{5})(?:-\d{4})?\s*$", row["address"])
        if m:
            conn.execute("UPDATE leads SET postcode = ? WHERE id = ?",
                         (m.group(1), row["id"]))


def _backfill_run_ids(conn):
    """Reconstruct which pull each existing lead came from, so the dashboard can
    show just the current pull. Best-effort: walk completed runs oldest-first and
    claim that run's `added` count of still-unassigned leads matching its industry
    and date. Idempotent (only touches run_id IS NULL); leftover legacy/imported
    leads (no matching run) stay NULL and live in History only."""
    runs = conn.execute(
        "SELECT id, started_at, industry, added FROM pull_runs "
        "WHERE status IN ('done', 'cancelled') AND added > 0 ORDER BY id"
    ).fetchall()
    for run in runs:
        slugs = [s.strip() for s in (run["industry"] or "").split(",") if s.strip()]
        if not slugs:
            continue
        run_date = (run["started_at"] or "")[:10]
        placeholders = ",".join("?" * len(slugs))
        ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM leads WHERE run_id IS NULL AND pulled_date = ? "
            f"AND industry IN ({placeholders}) ORDER BY id LIMIT ?",
            (run_date, *slugs, run["added"]),
        )]
        if ids:
            conn.execute(
                f"UPDATE leads SET run_id = ? WHERE id IN ({','.join('?' * len(ids))})",
                (run["id"], *ids),
            )


def _seed_industries(conn):
    """Seed/top-up the industry catalog. Runs fully on a fresh DB, and once per
    INDUSTRY_CATALOG_VERSION bump on an existing DB (adding only new industries
    via INSERT OR IGNORE, so the user's own edits and deletions are preserved)."""
    fresh = conn.execute("SELECT 1 FROM industries LIMIT 1").fetchone() is None
    stored_version = int(get_setting(conn, "industry_catalog_version", "0") or 0)
    if not fresh and stored_version >= INDUSTRY_CATALOG_VERSION:
        return

    for slug, info in SEED_INDUSTRIES.items():
        cur = conn.execute(
            "INSERT OR IGNORE INTO industries (slug, label, query_template) VALUES (?, ?, ?)",
            (slug, info["label"], info.get("query", DEFAULT_QUERY_TEMPLATE)),
        )
        if cur.rowcount:  # newly inserted -> seed its chain list too
            for chain in info["chains"]:
                conn.execute(
                    "INSERT OR IGNORE INTO chains (industry_id, name) VALUES (?, ?)",
                    (cur.lastrowid, chain),
                )
    set_setting(conn, "industry_catalog_version", str(INDUSTRY_CATALOG_VERSION))


def _migrate_cities(conn):
    if conn.execute("SELECT 1 FROM cities LIMIT 1").fetchone():
        return
    if not OLD_CITIES_FILE.exists():
        return
    for line in OLD_CITIES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, state = line.partition(",")
        conn.execute(
            "INSERT OR IGNORE INTO cities (name, state) VALUES (?, ?)",
            (name.strip(), state.strip()),
        )


def _migrate_leads(conn):
    if conn.execute("SELECT 1 FROM leads LIMIT 1").fetchone():
        return

    # Old daily CSVs carry the full lead details; import them first.
    for csv_file in sorted(SCRIPT_DIR.glob("leads_*.csv")):
        m = re.search(r"leads_(\d{4}-\d{2}-\d{2})\.csv$", csv_file.name)
        pulled = m.group(1) if m else str(date.today())
        try:
            with open(csv_file, encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    phone = (row.get("phone") or "").strip()
                    name = (row.get("business_name") or "").strip()
                    if not phone or not name:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO leads (phone, business_name, address, city, state, "
                        "website, category, industry, score, call_hook, pulled_date) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            phone, name,
                            row.get("address") or "",
                            row.get("city") or "",
                            row.get("state") or "",
                            row.get("website") or "",
                            row.get("category") or "",
                            row.get("industry") or "",
                            int(row.get("score") or 0),
                            row.get("call_hook") or "",
                            pulled,
                        ),
                    )
        except (OSError, csv.Error):
            continue

    # Anything in the old dedupe DB not covered by a CSV still counts as seen:
    # import it as a bare lead so it is never served again.
    if OLD_SEEN_DB.exists():
        old = sqlite3.connect(OLD_SEEN_DB)
        try:
            rows = old.execute(
                "SELECT phone, business_name, first_seen_date FROM seen_leads"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            old.close()
        for phone, name, first_seen in rows:
            if not phone:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO leads (phone, business_name, pulled_date) VALUES (?, ?, ?)",
                (phone.strip(), (name or "(unknown)").strip(), first_seen or str(date.today())),
            )
