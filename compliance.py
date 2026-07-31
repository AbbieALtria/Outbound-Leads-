"""
Calling-hours compliance (CRTC for Canada; US TSR as a sensible default).

CRTC restricts telemarketing to 9:00-21:30 Mon-Fri and 10:00-18:00 Sat-Sun in
the RECIPIENT's local time zone. This applies to B2B calls too — B2B is only
exempt from the National DNC LIST, not the calling-hour rules (so we do NOT
scrub Canadian B2B numbers against the National DNC list here).

This module only answers "is it legal to call this lead right now?" given their
country/province(state). The real-time, per-number gate belongs in VICIdial's
per-timezone call-time settings; the export surfaces this as a safety warning.
"""

from datetime import datetime, time

try:
    from zoneinfo import ZoneInfo
except Exception:                       # pragma: no cover
    ZoneInfo = None

# Province -> IANA timezone (Canada).
PROVINCE_TZ = {
    "ontario": "America/Toronto", "quebec": "America/Toronto",
    "nova scotia": "America/Halifax", "new brunswick": "America/Halifax",
    "prince edward island": "America/Halifax", "pei": "America/Halifax",
    "newfoundland and labrador": "America/St_Johns", "newfoundland": "America/St_Johns",
    "manitoba": "America/Winnipeg", "saskatchewan": "America/Regina",
    "alberta": "America/Edmonton", "british columbia": "America/Vancouver",
    "yukon": "America/Whitehorse", "northwest territories": "America/Yellowknife",
    "nunavut": "America/Iqaluit",
    # postal abbreviations
    "on": "America/Toronto", "qc": "America/Toronto", "ns": "America/Halifax",
    "nb": "America/Halifax", "pe": "America/Halifax", "nl": "America/St_Johns",
    "mb": "America/Winnipeg", "sk": "America/Regina", "ab": "America/Edmonton",
    "bc": "America/Vancouver", "yt": "America/Whitehorse", "nt": "America/Yellowknife",
    "nu": "America/Iqaluit",
}

# State -> IANA timezone (US), by the state's dominant zone. Precise per-number
# timezone is VICIdial's job; this is a compliance approximation.
STATE_TZ = {
    "connecticut": "America/New_York", "delaware": "America/New_York",
    "florida": "America/New_York", "georgia": "America/New_York",
    "indiana": "America/New_York", "kentucky": "America/New_York",
    "maine": "America/New_York", "maryland": "America/New_York",
    "massachusetts": "America/New_York", "michigan": "America/New_York",
    "new hampshire": "America/New_York", "new jersey": "America/New_York",
    "new york": "America/New_York", "north carolina": "America/New_York",
    "ohio": "America/New_York", "pennsylvania": "America/New_York",
    "rhode island": "America/New_York", "south carolina": "America/New_York",
    "vermont": "America/New_York", "virginia": "America/New_York",
    "west virginia": "America/New_York", "district of columbia": "America/New_York",
    "alabama": "America/Chicago", "arkansas": "America/Chicago",
    "illinois": "America/Chicago", "iowa": "America/Chicago",
    "kansas": "America/Chicago", "louisiana": "America/Chicago",
    "minnesota": "America/Chicago", "mississippi": "America/Chicago",
    "missouri": "America/Chicago", "nebraska": "America/Chicago",
    "north dakota": "America/Chicago", "oklahoma": "America/Chicago",
    "south dakota": "America/Chicago", "tennessee": "America/Chicago",
    "texas": "America/Chicago", "wisconsin": "America/Chicago",
    "colorado": "America/Denver", "idaho": "America/Denver",
    "montana": "America/Denver", "new mexico": "America/Denver",
    "utah": "America/Denver", "wyoming": "America/Denver",
    "arizona": "America/Phoenix",
    "california": "America/Los_Angeles", "nevada": "America/Los_Angeles",
    "oregon": "America/Los_Angeles", "washington": "America/Los_Angeles",
    "alaska": "America/Anchorage", "hawaii": "Pacific/Honolulu",
}


def _is_canada(country):
    c = (country or "").strip().lower()
    return c in ("canada", "ca", "can")


def tz_for(country, state, city=""):
    """IANA timezone name for a lead, or None if it can't be determined."""
    key = (state or "").strip().lower()
    if _is_canada(country):
        return PROVINCE_TZ.get(key)
    return STATE_TZ.get(key)


def calling_window(country, weekend):
    """(open, close) legal calling times for the day."""
    if _is_canada(country):
        return (time(10, 0), time(18, 0)) if weekend else (time(9, 0), time(21, 30))
    return time(8, 0), time(21, 0)      # US TSR, every day


def within_calling_hours(country, state, city="", now=None):
    """Is it legal to call this lead right now (recipient local time)?
    Returns (ok: bool, note: str). When the timezone can't be determined we
    return ok=True (don't block) with an 'unknown' note — VICIdial is the real gate."""
    tzname = tz_for(country, state, city)
    if not tzname or ZoneInfo is None:
        return True, "timezone unknown — not checked"
    z = ZoneInfo(tzname)
    local = now.astimezone(z) if now else datetime.now(z)
    open_t, close_t = calling_window(country, local.weekday() >= 5)
    ok = open_t <= local.time() <= close_t
    region = "CRTC" if _is_canada(country) else "US TSR"
    return ok, (f"{region}: {local:%a %H:%M} {tzname} "
                f"(legal {open_t:%H:%M}-{close_t:%H:%M})")
