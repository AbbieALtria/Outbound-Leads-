"""
Free decision-maker discovery from a lead's OWN website.

The contact databases (Apollo, enrich.so, Lusha, ...) are all LinkedIn-derived,
so they miss the operators of local businesses — a Toronto dental clinic has no
LinkedIn company page, but its website almost always says "Meet Dr. Patel" or
lists an office manager on an About/Team page.

This reads that page. No API key, no credits, no vendor: just the two HTTP
requests we already make for the site check. It runs as tier 0 of the
enrichment waterfall, before any paid provider.

Deliberately conservative: it would rather return nothing than a wrong name, so
every candidate needs either a Dr./credential marker or an explicit
decision-maker title next to it.
"""

import re
from urllib.parse import urljoin, urlparse

import requests

UA = {"User-Agent": "Mozilla/5.0 (compatible; lead-research)"}

# Pages that carry the people. Ordered best-first.
TEAM_PATHS = [
    "/about", "/about-us", "/our-team", "/team", "/meet-the-team", "/meet-our-team",
    "/staff", "/our-staff", "/doctors", "/our-doctors", "/dentists", "/physicians",
    "/providers", "/leadership", "/management", "/who-we-are", "/our-practice",
]
# Link text that suggests a people page, when the paths above don't exist.
TEAM_LINK_WORDS = ("about", "team", "staff", "doctor", "dentist", "provider",
                   "physician", "leadership", "our practice", "who we are",
                   "meet ")

# Decision-maker titles worth asking for by name. Order = priority.
TITLES = [
    "owner", "practice owner", "founder", "co-founder", "president", "principal",
    "managing partner", "partner", "chief executive", "ceo", "proprietor",
    "practice manager", "office manager", "clinic manager", "general manager",
    "practice administrator", "administrator", "director of operations",
    "operations manager", "it manager", "director",
    # French equivalents — a Quebec or New Brunswick site names its owner in
    # French, and the English list above never matches it.
    "propriétaire", "proprietaire", "directeur", "directrice", "gérant",
    "gerant", "gérante", "gerante", "responsable", "associé", "associe",
    "associée", "associee", "fondateur", "fondatrice", "président", "president",
    "présidente", "presidente", "chef de clinique",
]
# Professional credentials that usually mark the practice's principal.
CREDENTIALS = ["dds", "dmd", "md", "do", "dc", "od", "dvm", "rmt", "bsc", "phd",
               "cpa", "llb", "jd"]

# Latin letters including the accented ones. Canada is bilingual and a quarter of
# this market is francophone: an [A-Z][a-z] name pattern silently skips every
# René, Côté and Lévesque, which is most of a New Brunswick or Quebec list.
_U = "A-ZÀ-ÖØ-Þ"
_L = "a-zà-öø-ÿ"
# "Firstname Lastname" — two or three capitalised words, no digits.
NAME_RE = (r"[" + _U + r"][" + _L + r"]{1,15}(?:\s+[" + _U + r"]\.)?"
           r"\s+[" + _U + r"][" + _L + r"'\-]{1,20}")
# Dr / Dre (docteure) / Docteur / Docteure, with or without the point.
DOCTOR_RE = r"\b(Dre?|Docteure?)\.?\s+"
# Words that look like names but aren't people.
NOT_NAMES = {
    "our team", "the team", "contact us", "about us", "read more", "learn more",
    "book now", "new patients", "meet the", "office hours", "get directions",
    "privacy policy", "terms of", "all rights", "family dentistry", "general dentistry",
    "dental care", "customer service", "emergency service", "free consultation",
}


def _clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def _looks_like_name(name):
    low = name.lower()
    if any(bad in low for bad in NOT_NAMES):
        return False
    # Reject if a word is a known title/credential rather than a name part.
    parts = low.split()
    if any(p.strip(".,") in TITLES or p.strip(".,") in CREDENTIALS for p in parts):
        return False
    return True


def _fetch(url, timeout=6):
    # A redirect chain multiplies the timeout (each hop gets its own budget), so
    # cap the hops — otherwise one badly-configured site can stall a batch.
    try:
        sess = requests.Session()
        sess.max_redirects = 3
        r = sess.get(url, timeout=timeout, allow_redirects=True, headers=UA)
        if r.status_code != 200 or "text/html" not in r.headers.get("Content-Type", ""):
            return ""
        return r.text[:400000]
    except requests.RequestException:
        return ""


def _strip_html(html):
    html = re.sub(r"(?is)<(script|style|nav|footer)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    return _clean(html)


def extract_people(text):
    """People found in page text, best-first. Each: {name, title}."""
    found, seen = [], set()

    # 1. "Dr. Firstname Lastname" — the strongest signal for clinics. The
    # honorific is kept as written: "Dre" is the French feminine form, and an
    # agent reading "Dr." for a Dre starts the call by getting it wrong.
    for m in re.finditer(DOCTOR_RE + r"(" + NAME_RE + r")", text):
        name = _clean(m.group(2))
        honorific = "Dre." if m.group(1).lower() in ("dre", "docteure") else "Dr."
        if _looks_like_name(name) and name.lower() not in seen:
            seen.add(name.lower())
            found.append({"name": f"{honorific} {name}", "title": "", "rank": 1})

    # 2. "Firstname Lastname, DDS" / ", MD" — credentialed principal.
    cred = "|".join(CREDENTIALS)
    for m in re.finditer(r"\b(" + NAME_RE + r")\s*,\s*(" + cred + r")\b", text, re.I):
        name = _clean(m.group(1))
        if _looks_like_name(name) and name.lower() not in seen:
            seen.add(name.lower())
            found.append({"name": name, "title": m.group(2).upper(), "rank": 2})

    # 3. A name sitting next to an explicit decision-maker title, either order.
    tit = "|".join(re.escape(t) for t in TITLES)
    for pat, ni, ti in (
        (r"\b(" + NAME_RE + r")\s*[,–—\-–—|]\s*((?:" + tit + r")[a-z ]{0,20})", 1, 2),
        (r"\b((?:" + tit + r")[a-z ]{0,20})\s*[:–—\-–—|]\s*(" + NAME_RE + r")", 2, 1),
    ):
        for m in re.finditer(pat, text, re.I):
            name = _clean(m.group(ni))
            title = _clean(m.group(ti)).title()
            if _looks_like_name(name) and name.lower() not in seen:
                seen.add(name.lower())
                found.append({"name": name, "title": title, "rank": 0})

    found.sort(key=lambda p: p["rank"])
    return [{"name": p["name"], "title": p["title"]} for p in found]


def team_page_urls(base_url, html):
    """Candidate people-pages: the conventional paths, plus any link whose text
    suggests a team page."""
    urls, seen = [], set()

    def add(u):
        if u and u not in seen:
            seen.add(u)
            urls.append(u)

    for path in TEAM_PATHS:
        add(urljoin(base_url, path))
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html or "", re.I | re.S):
        href, label = m.group(1), _strip_html(m.group(2)).lower()
        if label and any(w in label for w in TEAM_LINK_WORDS) and not href.startswith("#"):
            full = urljoin(base_url, href)
            if urlparse(full).netloc == urlparse(base_url).netloc:
                add(full)
    return urls


def find_team_contact(website, timeout=6, max_pages=2):
    """Best decision-maker published on the business's own site.
    Returns {name, title, source_url} or {} when nothing credible is found.

    Time-bounded on purpose: at most 1 + max_pages requests at `timeout` seconds
    each, so one slow site can't stall a whole enrichment batch."""
    url = (website or "").strip()
    if not url:
        return {}
    if not url.lower().startswith(("http://", "https://")):
        url = "http://" + url

    home = _fetch(url, timeout)
    if not home:
        return {}
    # The homepage itself often names the principal ("Meet Dr. Patel").
    people = extract_people(_strip_html(home))
    if people:
        return {**people[0], "source_url": url}

    for page in team_page_urls(url, home)[:max_pages]:
        html = _fetch(page, timeout)
        if not html:
            continue
        people = extract_people(_strip_html(html))
        if people:
            return {**people[0], "source_url": page}
    return {}
