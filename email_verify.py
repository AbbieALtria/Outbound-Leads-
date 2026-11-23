"""Email verification — is a lead's email address deliverable?

Dormant unless keyed, like contacts.py: the provider and its key come from env
vars (EMAIL_VERIFY_API_KEY, and EMAIL_VERIFY_PROVIDER — default "zerobounce").
With no key, enabled() is False, nothing is called and nothing is spent.

Each checked lead gets leads.email_status — one of STATUSES — plus
email_checked_at. Exports keep the lead but leave out an email marked invalid.

Provider interface (one small class; add another to PROVIDERS to swap):
    name                 shown in the UI
    credits_per_check    for the cost estimate the confirmation modal shows
    verify(email) -> (status, detail)   status in STATUSES; raises RuntimeError
                                        or requests.RequestException on failure
"""
import os
import time

import requests

import budget
import db

STATUSES = ("valid", "risky", "catch_all", "invalid", "unknown")


class ZeroBounce:
    """ZeroBounce single-email validation (GET /v2/validate).

    Response statuses per their docs: valid, invalid, catch-all, unknown,
    spamtrap, abuse, do_not_mail. The last three are deliverable-but-dangerous,
    so they map to "risky". Written from the published API shape; confirm
    against a live account before running at scale."""
    name = "zerobounce"
    credits_per_check = 1
    URL = "https://api.zerobounce.net/v2/validate"
    _MAP = {"valid": "valid", "invalid": "invalid", "catch-all": "catch_all",
            "unknown": "unknown", "spamtrap": "risky", "abuse": "risky",
            "do_not_mail": "risky"}

    def __init__(self, key):
        self.key = key

    @classmethod
    def map_status(cls, raw):
        return cls._MAP.get((raw or "").strip().lower(), "unknown")

    def verify(self, email, timeout=20):
        resp = requests.get(self.URL, params={"api_key": self.key, "email": email,
                                              "ip_address": ""}, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"{self.name} returned status {resp.status_code}: "
                               f"{resp.text[:200]}")
        data = resp.json()
        if data.get("error"):
            raise RuntimeError(f"{self.name}: {data['error']}")
        return self.map_status(data.get("status")), (data.get("sub_status") or "")


PROVIDERS = {"zerobounce": ZeroBounce}


def api_key():
    return os.environ.get("EMAIL_VERIFY_API_KEY", "").strip()


def provider_name():
    return (os.environ.get("EMAIL_VERIFY_PROVIDER", "").strip().lower()
            or "zerobounce")


def enabled():
    """True when a key is set for a provider this module knows."""
    return bool(api_key()) and provider_name() in PROVIDERS


def provider():
    """The configured provider instance, or None when verification is off."""
    if not enabled():
        return None
    return PROVIDERS[provider_name()](api_key())


def estimate_credits(n_emails, prov=None):
    """Worst-case credits for checking n addresses (shown before confirming)."""
    prov = prov or provider()
    return n_emails * (prov.credits_per_check if prov else 0)


def verify_leads(conn, rows, log=print, prov=None, pause=0.0):
    """Check each (id, email) row and write email_status / email_checked_at.

    Leads without an email are skipped. A provider failure stops the run —
    continuing would spend credits against a provider that is refusing us —
    and is reported in the summary's "error". Returns counts per status plus
    checked, credits (worst case: credits_per_check × checked) and error."""
    summary = {s: 0 for s in STATUSES}
    summary.update(checked=0, credits=0, error="", cap_note="")
    prov = prov or provider()
    rows = [r for r in rows if (r["email"] or "").strip()]
    if prov is None or not rows:
        return summary
    # Daily cap: check only what today still allows; the rest wait for tomorrow.
    allowed = budget.allowance(conn, "email", len(rows))
    capped = allowed < len(rows)
    rows = rows[:allowed]
    if not rows:
        summary["cap_note"] = budget.cap_message(conn, "email")
        log(f"  {summary['cap_note']}")
        return summary
    log(f"Verifying {len(rows)} email addresses via {prov.name}...")
    for r in rows:
        email = r["email"].strip()
        try:
            status, detail = prov.verify(email)
        except (requests.RequestException, RuntimeError, ValueError) as e:
            summary["error"] = str(e)
            log(f"  WARNING: email verification stopped: {e}")
            break
        if status not in STATUSES:
            status = "unknown"
        conn.execute(
            "UPDATE leads SET email_status = ?, email_checked_at = ? WHERE id = ?",
            (status, db.now_iso(), r["id"]))
        summary[status] += 1
        summary["checked"] += 1
        summary["credits"] += prov.credits_per_check
        if detail and status != "valid":
            log(f"  {email}: {status} ({detail})")
        if pause:
            time.sleep(pause)
    conn.commit()
    budget.record(conn, "email", summary["checked"])
    if capped:
        summary["cap_note"] = budget.cap_message(conn, "email")
        log(f"  {summary['cap_note']}")
    return summary
