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

Optional second tier (see "Claude tier" at the bottom): when the patterns find
nobody and ANTHROPIC_API_KEY is set, the cleaned page text goes to Claude, which
returns decision-makers as structured output. Every name it returns must appear
verbatim in the page text, with the exact quote it came from, or it is dropped.
The regex tier stays the fallback and the only path without a key.
"""

import json
import os
import re
from urllib.parse import urljoin, urlparse

import requests

import claude_config

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
# One name word: a capital, then a lowercase/apostrophe/hyphen, then anything —
# so a capital INSIDE the word is allowed. Without that, McGinnis reads as "Mc"
# and MacDonald as "Mac", which mangles a large share of Canadian surnames.
# Requiring the second character to be non-capital still rejects "DDS" and "RCDSO".
_WORD = r"[" + _U + r"][" + _L + r"'\-][" + _U + _L + r"'\-]{0,18}"
# "Firstname Lastname" — two or three capitalised words, no digits.
NAME_RE = _WORD + r"(?:\s+[" + _U + r"]\.)?\s+" + _WORD
# Dr / Dre (docteure) / Docteur / Docteure, with or without the point.
DOCTOR_RE = r"\b(Dre?|Docteure?)\.?\s+"
# Words that look like names but aren't people.
NOT_NAMES = {
    "our team", "the team", "contact us", "about us", "read more", "learn more",
    "book now", "new patients", "meet the", "office hours", "get directions",
    "privacy policy", "terms of", "all rights", "family dentistry", "general dentistry",
    "dental care", "customer service", "emergency service", "free consultation",
}
# Words a capitalised pair can start with that no person's name does. On real
# sites these produced "Hi Steven" from an owner's reply to a review, where the
# greeting is the only thing that made it look like a name.
NOT_FIRST = {"hi", "hello", "hey", "dear", "thanks", "thank", "welcome", "meet",
             "and", "or", "the", "our", "your", "by", "from", "with", "for", "to",
             "at", "in", "of"}


def _clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def _looks_like_name(name):
    low = name.lower()
    if any(bad in low for bad in NOT_NAMES):
        return False
    if low.split()[0] in NOT_FIRST:
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
    # Case matters on both sides of the comma. The name must be capitalised the
    # way a name is: matched case-blind, "keep costs down, do not wait" read as
    # a person called "costs down" with the title DO. The longer credentials are
    # accepted in any case ("Dds", "BSc", "PhD"); the two-letter ones only in
    # capitals, because in lower case "do", "od" and "dc" are ordinary text.
    cred = ("(?i:" + "|".join(c for c in CREDENTIALS if len(c) > 2) + ")|"
            + "|".join(c.upper() for c in CREDENTIALS if len(c) <= 2))
    for m in re.finditer(r"\b(" + NAME_RE + r")\s*,\s*(" + cred + r")\b", text):
        name = _clean(m.group(1))
        if _looks_like_name(name) and name.lower() not in seen:
            seen.add(name.lower())
            found.append({"name": name, "title": m.group(2).upper(), "rank": 2})

    # 3. A name sitting next to an explicit decision-maker title, either order.
    # Only the title itself is case-blind ("Owner", "OWNER", "office manager").
    # The name is held to capitals as in pass 2, and the words a title may run
    # on into must be lower case ("Owner and lead dentist") — case-blind, they
    # swallowed the next person's name, and "senior-level counsel" passed as
    # somebody.
    # The title must also end at a word boundary ("Partnership with purpose" is
    # not a partner), a bare hyphen separates only when spaced (so a headline's
    # "President-Elect Designate" is not a president called Elect Designate),
    # and a run-on that stops on a joining word is trimmed ("Owner of" ->
    # "Owner").
    tit = "(?i:" + "|".join(re.escape(t) for t in TITLES) + r")\b"
    after_name = r"(?:\s*[,–—|]\s*|\s+-\s+)"
    after_title = r"(?:\s*[:–—|]\s*|\s+-\s+)"
    for pat, ni, ti in (
        (r"\b(" + NAME_RE + r")" + after_name + r"((?:" + tit + r")[a-z ]{0,20})", 1, 2),
        (r"\b((?:" + tit + r")[a-z ]{0,20})" + after_title + r"(" + NAME_RE + r")", 2, 1),
    ):
        for m in re.finditer(pat, text):
            name = _clean(m.group(ni))
            title = re.sub(r"(?:\s+(?:of|and|for|at|the|in|with))+$", "",
                           _clean(m.group(ti))).title()
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


def find_team_contact(website, timeout=6, max_pages=2, ai=None):
    """Best decision-maker published on the business's own site.
    Returns {name, title, source_url} or {} when nothing credible is found.

    Time-bounded on purpose: at most 1 + max_pages requests at `timeout` seconds
    each, so one slow site can't stall a whole enrichment batch.

    `ai`: an AIBudget for this run. When given, and a key is set, pages the
    patterns found nobody on are read by Claude (one call per domain, cached,
    capped per run). Without it — or without a key — only the patterns run."""
    url = (website or "").strip()
    if not url:
        return {}
    if not url.lower().startswith(("http://", "https://")):
        url = "http://" + url

    home = _fetch(url, timeout)
    if not home:
        return {}
    texts = []    # (page_url, cleaned text) the patterns drew a blank on
    # The homepage itself often names the principal ("Meet Dr. Patel").
    text = _strip_html(home)
    people = extract_people(text)
    if people:
        return {**people[0], "source_url": url}
    texts.append((url, text))

    for page in team_page_urls(url, home)[:max_pages]:
        html = _fetch(page, timeout)
        if not html:
            continue
        text = _strip_html(html)
        people = extract_people(text)
        if people:
            return {**people[0], "source_url": page}
        texts.append((page, text))

    if ai is not None and ai_enabled():
        return ai_team_contact(url, texts, ai)
    return {}


# --- Claude tier -----------------------------------------------------------------
# Runs only when the patterns above found nobody AND ANTHROPIC_API_KEY is set.
# The guards against a made-up name are mechanical, not prompt-based: a name is
# accepted only if it appears verbatim in the page text, inside the quote the
# model copied, with confidence >= AI_MIN_CONFIDENCE. Each domain is asked once
# per process (cache) and each run makes at most AIBudget.cap calls.

AI_MAX_CHARS = 12000          # page text sent per call (about 3k tokens)
AI_MIN_CONFIDENCE = 0.7
AI_DEFAULT_MAX_CALLS = int(os.environ.get("SITE_CONTACTS_AI_MAX_CALLS", "40"))

PEOPLE_SCHEMA = {
    "type": "object",
    "properties": {
        "people": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "The person's name exactly as written in the text"},
                    "title": {"type": "string",
                              "description": "Their role at this business, as the text gives it (Owner, Dr., Practice Manager...)"},
                    "confidence": {"type": "number",
                                   "description": "0-1: how sure you are this person makes buying decisions at THIS business"},
                    "quote": {"type": "string",
                              "description": "The exact passage from the text, copied verbatim, in which this person and their role appear"},
                },
                "required": ["name", "title", "confidence", "quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["people"],
    "additionalProperties": False,
}

AI_SYSTEM = (
    "You read the text of a local business's own website and list the people who "
    "run it: owner, founder, principal, the doctor/dentist/lawyer whose practice it "
    "is, practice or office manager, general manager. Only list a person whose name "
    "appears in the text exactly as you write it, and copy the exact passage where "
    "the name and role appear into `quote` without changing a character. Do not "
    "list testimonial authors, patients, suppliers, or people mentioned in passing. "
    "Confidence is how sure you are this person makes buying decisions at this "
    "business. When the text names nobody who runs the business, return an empty list."
)

_ai_cache = {}    # domain -> result dict ({} when nothing credible), for the process lifetime


class AIBudget:
    """Per-run cap on Claude calls, plus the counters the run reports."""

    def __init__(self, cap=None):
        self.cap = AI_DEFAULT_MAX_CALLS if cap is None else cap
        self.calls = 0        # requests actually sent
        self.hits = 0         # contacts the tier supplied (cached ones included)
        self.capped = 0       # domains skipped because the cap was reached
        self.cache_hits = 0   # domains answered from the cache, no request
        self.error = ""       # first API failure, if any

    def allow(self):
        if self.calls >= self.cap:
            self.capped += 1
            return False
        return True

    def summary(self):
        return {"ai_calls": self.calls, "ai_hits": self.hits, "ai_capped": self.capped,
                "ai_cache_hits": self.cache_hits, "ai_error": self.error}


def ai_enabled():
    return claude_config.enabled()


def _client():
    import anthropic
    return anthropic.Anthropic()    # reads ANTHROPIC_API_KEY from the env


def _domain(url):
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def ai_extract_people(text, budget, client=None, model=None):
    """Decision-makers Claude finds in `text`, best-first, each
    {name, title, confidence, quote}. Only people whose name appears verbatim in
    the text, inside their quote, with confidence >= AI_MIN_CONFIDENCE survive.
    Spends one call against `budget` (none when the cap is reached)."""
    text = _clean(text)[:AI_MAX_CHARS]
    if not text or not budget.allow():
        return []
    budget.calls += 1
    try:
        client = client or _client()
        resp = client.messages.create(
            model=model or claude_config.MODEL,
            max_tokens=2000,
            system=AI_SYSTEM,
            output_config={"format": {"type": "json_schema", "schema": PEOPLE_SCHEMA},
                           "effort": "low"},
            messages=[{"role": "user", "content": f"PAGE TEXT:\n{text}"}],
        )
    except Exception as e:                 # noqa: BLE001 - never let the tier break a batch
        budget.error = budget.error or str(e)
        return []
    if resp.stop_reason == "refusal":
        return []
    raw = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return []
    accepted = []
    for p in data.get("people") or []:
        if not isinstance(p, dict):
            continue
        name, title, quote = _clean(p.get("name")), _clean(p.get("title")), _clean(p.get("quote"))
        try:
            conf = float(p.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        if not name or not title or conf < AI_MIN_CONFIDENCE:
            continue
        # The anti-hallucination check: the name must be in the page, in the
        # passage the model says it came from, and that passage must be real.
        if name not in text or quote not in text or name not in quote:
            continue
        accepted.append({"name": name, "title": title, "confidence": conf, "quote": quote})
    accepted.sort(key=lambda p: -p["confidence"])
    return accepted


def ai_team_contact(url, texts, ai, client=None):
    """The Claude tier for one site: `texts` are the (page_url, text) pairs the
    patterns found nobody on. One call per domain per process; the team page is
    preferred over the homepage because that is where people are named."""
    domain = _domain(url)
    if domain in _ai_cache:
        ai.cache_hits += 1
        result = _ai_cache[domain]
    else:
        candidates = [t for t in texts if len(t[1]) > 200]
        if not candidates:
            return {}
        page_url, text = (candidates[1:] or candidates)[0]
        before = ai.calls
        people = ai_extract_people(text, ai, client)
        result = ({**people[0], "source_url": page_url, "via": "ai"} if people else {})
        if ai.calls > before:
            # Only a real answer is cached. A domain skipped at the cap (no call
            # made) must not be remembered as "nobody here" for the next run.
            _ai_cache[domain] = result
    if result:
        ai.hits += 1
    return result
