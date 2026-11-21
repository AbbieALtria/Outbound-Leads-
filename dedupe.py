"""Dedupe keys for leads: the same PLACE must be stored once, while a second
branch of the same business at another address must be kept (the multi-location
score signal rewards it).

Three keys, in order of trust:
  1. place_id   - Google's id for the listing (Outscraper `place_id`/`google_id`).
  2. site_key   - owning website domain + normalised street (falls back to the
                  postal code when the street is unusable). Same domain at a
                  different street is a different key, so branches survive.
  3. name_key   - normalised business name + postal code.
Phone stays UNIQUE on the table as the fourth, oldest key.

This module is imported by both db.py (backfill) and pipeline.py (pull), so it
must not import either.
"""
import re


def norm_name(name):
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


def org_domain(value):
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


# Street-suffix spellings that Maps and site footers mix freely.
_STREET_ABBREV = {
    "street": "st", "avenue": "ave", "av": "ave", "boulevard": "blvd", "road": "rd",
    "drive": "dr", "lane": "ln", "court": "ct", "place": "pl", "parkway": "pkwy",
    "highway": "hwy", "terrace": "ter", "circle": "cir", "square": "sq",
    "north": "n", "south": "s", "east": "e", "west": "w",
}
_UNIT = re.compile(r"\b(suite|ste|unit|apt|apartment|floor|fl|bldg|building)\s*#?\s*[a-z0-9-]+|#\s*[a-z0-9-]+", re.I)


def norm_street(street, address=""):
    """Comparable street line, or '' when there isn't a usable one.

    Uses the `street` field when present, else the text before the first comma
    of the full address. Unit/suite designators go (two tenants of one building
    sharing a domain are one business), suffixes are abbreviated, and a line
    with no digit at all is rejected: that is a city or area name, which would
    merge every branch in town."""
    s = (street or "").strip() or (address or "").split(",")[0].strip()
    s = _UNIT.sub(" ", s.lower())
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    words = [_STREET_ABBREV.get(w, w) for w in s.split()]
    s = " ".join(w for w in words if w)
    return s if re.search(r"\d", s) else ""


def norm_postcode(postcode):
    """Upper-cased, single-spaced; a US ZIP+4 collapses to its 5-digit ZIP."""
    p = re.sub(r"\s+", " ", (postcode or "").strip().upper())
    m = re.fullmatch(r"(\d{5})-\d{4}", p)
    return m.group(1) if m else p


def lead_keys(name, website, street="", address="", postcode="", place_id=""):
    """The dedupe keys for one lead/place as a dict:
    place_id, site_key, name_key (what gets stored) and site_key_alt, a
    postal-code-based site key used only when LOOKING UP, so a record that
    arrives without a street can still meet one stored with it."""
    domain = org_domain(website)
    pc = norm_postcode(postcode)
    street_n = norm_street(street, address)
    site_key = site_key_alt = ""
    if domain:
        if street_n:
            site_key = f"{domain}|{street_n}"
            site_key_alt = f"{domain}|pc:{pc}" if pc else ""
        elif pc:
            site_key = f"{domain}|pc:{pc}"
    nm = norm_name(name)
    name_key = f"{nm}|{pc}" if nm and pc else ""
    return {"place_id": (place_id or "").strip(), "site_key": site_key,
            "site_key_alt": site_key_alt, "name_key": name_key}


def find_existing(conn, keys):
    """Which key an already-stored lead shares with `keys`: 'place_id',
    'site_key', 'name_key', or '' when none. Checked in order of trust."""
    if keys.get("place_id"):
        if conn.execute("SELECT 1 FROM leads WHERE place_id = ? LIMIT 1",
                        (keys["place_id"],)).fetchone():
            return "place_id"
    site = [k for k in (keys.get("site_key"), keys.get("site_key_alt")) if k]
    if site:
        ph = ",".join("?" * len(site))
        if conn.execute(f"SELECT 1 FROM leads WHERE site_key IN ({ph}) LIMIT 1",
                        site).fetchone():
            return "site_key"
    if keys.get("name_key"):
        if conn.execute("SELECT 1 FROM leads WHERE name_key = ? LIMIT 1",
                        (keys["name_key"],)).fetchone():
            return "name_key"
    return ""
