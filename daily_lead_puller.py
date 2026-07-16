#!/usr/bin/env python3
"""
Daily SEO Lead Pipeline
=======================
Pulls fresh, real leads from Outscraper (Google Maps data) for your target
industry, filters out franchise/chain locations, removes anything you've
already been given before (so agents never get a repeat), scores what's
left, and writes a ranked CSV ready to dial.

HOW TO RUN (every day, same folder as this file):

    Windows:
        set OUTSCRAPER_API_KEY=your_key_here
        python daily_lead_puller.py

That's it. Defaults below control everything else. Edit TARGET_LEADS_PER_DAY,
INDUSTRY, or cities.txt any time -- no need to touch the rest of the script.

FILES THIS SCRIPT USES/CREATES (all in the same folder):
    cities.txt            -- your list of target cities (edit any time)
    rotation_state.json    -- remembers which city to pull from next
    seen_leads.sqlite3     -- every phone number ever exported (prevents repeats)
    leads_YYYY-MM-DD.csv   -- today's ranked, ready-to-dial output

COMPLIANCE NOTE: this pulls B2B (business) contact data, which is generally
treated differently from consumer numbers under US telemarketing rules, but
you should still run your own DNC/complaint-list scrub before dialing and
confirm compliance requirements for your specific campaigns -- this script
does not replace that check.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# EASY-EDIT DEFAULTS -- change these any time without touching the rest
# ---------------------------------------------------------------------------
TARGET_LEADS_PER_DAY = 100
INDUSTRY = "hvac"          # matches a key in CHAIN_LISTS below
BUFFER_MULTIPLIER = 2.0    # pull extra per city to survive filtering/dedup

# ---------------------------------------------------------------------------
# Franchise/chain names to exclude per industry (local manager can't buy SEO --
# decisions are made at corporate). Add more any time.
# ---------------------------------------------------------------------------
CHAIN_LISTS = {
    "hvac": [
        "One Hour Air", "Aire Serv", "Service Experts", "ARS Rescue Rooter",
        "Air Conditioning Express", "Sila Heating", "Horizon Services",
        "Lennox", "Trane Comfort Specialist", "American Residential Services",
        "Any Hour Services", "Del-Air", "TemperaturePro", "Mechanical One",
    ],
    "plumbing": [
        "Roto-Rooter", "Mr. Rooter", "Benjamin Franklin Plumbing",
        "ARS Rescue Rooter", "American Leak Detection", "Any Hour Services",
    ],
    "electrician": [
        "Mister Sparky", "Puls", "One Hour Heating", "Any Hour Services",
    ],
    "roofing": [
        "Erie Home", "Long Roofing", "Custom Exteriors", "1-800-HANSONS",
    ],
    "auto_repair": [
        "Firestone", "Midas", "Jiffy Lube", "Meineke", "Walmart Auto Care",
        "AutoZone", "Advance Auto Parts", "O'Reilly", "Pep Boys", "Take 5",
        "Valvoline", "NTB", "Big O Tires", "Discount Tire", "Mavis",
    ],
}

OUTSCRAPER_URL = "https://api.outscraper.com/maps/search-v3"

SCRIPT_DIR = Path(__file__).resolve().parent
CITIES_FILE = SCRIPT_DIR / "cities.txt"
STATE_FILE = SCRIPT_DIR / "rotation_state.json"
DB_FILE = SCRIPT_DIR / "seen_leads.sqlite3"


def load_cities():
    if not CITIES_FILE.exists():
        sys.exit(f"Missing {CITIES_FILE}. Create it with one 'City, ST' per line.")
    cities = []
    for line in CITIES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            cities.append(line)
    if not cities:
        sys.exit("cities.txt has no cities in it.")
    return cities


def load_rotation_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"next_city_index": 0}


def save_rotation_state(state):
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_leads (
            phone TEXT PRIMARY KEY,
            business_name TEXT,
            first_seen_date TEXT
        )
    """)
    conn.commit()
    return conn


def is_already_seen(conn, phone):
    if not phone:
        return False
    row = conn.execute("SELECT 1 FROM seen_leads WHERE phone = ?", (phone,)).fetchone()
    return row is not None


def mark_seen(conn, phone, business_name):
    if not phone:
        return
    conn.execute(
        "INSERT OR IGNORE INTO seen_leads (phone, business_name, first_seen_date) VALUES (?, ?, ?)",
        (phone, business_name, str(date.today())),
    )


def is_chain(business_name, chain_list):
    if not business_name:
        return False
    name_lower = business_name.lower()
    return any(chain.lower() in name_lower for chain in chain_list)


def query_outscraper(api_key, query_text, limit):
    params = {"query": query_text, "limit": limit, "async": "false"}
    headers = {"X-API-KEY": api_key}
    resp = requests.get(OUTSCRAPER_URL, params=params, headers=headers, timeout=120)
    if resp.status_code != 200:
        print(f"  WARNING: Outscraper returned status {resp.status_code} for '{query_text}': {resp.text[:300]}")
        return []
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


def main():
    parser = argparse.ArgumentParser(description="Pull, filter, and rank fresh SEO leads.")
    parser.add_argument("--leads", type=int, default=TARGET_LEADS_PER_DAY,
                         help="How many fresh leads to produce today")
    parser.add_argument("--industry", default=INDUSTRY, choices=list(CHAIN_LISTS.keys()),
                         help="Industry vertical to search")
    args = parser.parse_args()

    api_key = os.environ.get("OUTSCRAPER_API_KEY", "").strip()
    if not api_key:
        sys.exit("Set OUTSCRAPER_API_KEY first, e.g.:\n  set OUTSCRAPER_API_KEY=your_key_here")

    chain_list = CHAIN_LISTS.get(args.industry, [])
    cities = load_cities()
    rotation_state = load_rotation_state()
    start_index = rotation_state.get("next_city_index", 0) % len(cities)

    conn = init_db()
    hot_leads = []
    cities_tried = 0
    city_index = start_index

    print(f"Target: {args.leads} fresh {args.industry} leads")
    print(f"Cities available: {len(cities)} (starting at '{cities[start_index]}')")
    print("-" * 60)

    while len(hot_leads) < args.leads and cities_tried < len(cities):
        city = cities[city_index]
        remaining_needed = args.leads - len(hot_leads)
        pull_limit = max(20, int(remaining_needed * BUFFER_MULTIPLIER))

        query_text = f"{args.industry.replace('_', ' ')} contractor in {city}"
        print(f"Querying Outscraper: '{query_text}' (requesting up to {pull_limit})")

        places = query_outscraper(api_key, query_text, pull_limit)
        print(f"  -> got {len(places)} raw results")

        added_this_city = 0
        for place in places:
            if len(hot_leads) >= args.leads:
                break

            name = (place.get("name") or place.get("title") or "").strip()
            phone = (place.get("phone") or place.get("phone_number") or "").strip()

            if not name or not phone:
                continue
            if is_chain(name, chain_list):
                continue
            if is_already_seen(conn, phone):
                continue

            score, hook = score_place(place)
            hot_leads.append({
                "business_name": name,
                "phone": phone,
                "address": place.get("full_address") or place.get("address") or "",
                "city": place.get("city") or city.split(",")[0].strip(),
                "state": place.get("state") or place.get("region") or "",
                "website": place.get("site") or place.get("website") or "",
                "category": place.get("type") or place.get("category") or args.industry,
                "score": score,
                "call_hook": hook,
            })
            mark_seen(conn, phone, name)
            added_this_city += 1

        print(f"  -> {added_this_city} new qualified leads added from {city}")
        conn.commit()

        cities_tried += 1
        city_index = (city_index + 1) % len(cities)
        time.sleep(1)  # be polite to the API between calls

    rotation_state["next_city_index"] = city_index
    save_rotation_state(rotation_state)
    conn.close()

    hot_leads.sort(key=lambda r: r["score"], reverse=True)

    output_file = SCRIPT_DIR / f"leads_{date.today().isoformat()}.csv"
    import csv
    fieldnames = ["business_name", "phone", "address", "city", "state",
                  "website", "category", "score", "call_hook"]
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(hot_leads)

    no_website_count = sum(1 for r in hot_leads if not r["website"])

    print("-" * 60)
    print(f"DONE. {len(hot_leads)} fresh leads written to {output_file.name}")
    print(f"  -> {no_website_count} have no website (highest priority to call first)")
    if len(hot_leads) < args.leads:
        print(f"  NOTE: only found {len(hot_leads)} of {args.leads} requested -- "
              f"add more cities to cities.txt to hit your daily target.")


if __name__ == "__main__":
    main()
