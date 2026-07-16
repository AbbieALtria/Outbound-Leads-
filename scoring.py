"""
Campaign-agnostic lead scoring.

A lead carries only NEUTRAL signals (no website, unclaimed listing, review count,
rating, no email, ...). A *campaign* decides which signals matter, how many points
each is worth, and the call-hook wording. Switching campaigns re-scores the same
leads without re-pulling. SEO is just one campaign preset among many.
"""

import json

# Each signal: does it apply to this lead? Returns True/False.
# lead is a sqlite3.Row (or dict). Neutral columns may be NULL (unknown) on
# legacy leads — an unknown signal is treated as not-firing.
SIGNALS = {
    "no_website":  lambda l: not (l["website"] or "").strip(),
    "has_website": lambda l: bool((l["website"] or "").strip()),
    "unclaimed":   lambda l: l["unclaimed"] == 1,
    "no_email":    lambda l: not (l["email"] or "").strip(),
    "low_reviews": lambda l: l["reviews"] is not None and l["reviews"] < 10,
    "few_reviews": lambda l: l["reviews"] is not None and 10 <= l["reviews"] < 25,
    "low_rating":  lambda l: l["rating"] is not None and l["reviews"] not in (None, 0)
                             and l["rating"] < 4.0,
}


# Signals that only make sense for B2B (business) leads. They must never fire
# for a B2C consumer lead, even if a B2B campaign happens to be active.
B2B_ONLY_SIGNALS = {"no_website", "has_website", "unclaimed",
                    "low_reviews", "few_reviews", "low_rating"}


def _mv(lead, key, default=None):
    """Read a key from a dict or sqlite3.Row, returning default if absent."""
    try:
        return lead[key]
    except (KeyError, IndexError):
        return default


def _fmt(hook, lead):
    try:
        return hook.format(reviews=_mv(lead, "reviews"), rating=_mv(lead, "rating"))
    except Exception:
        return hook


def evaluate(lead, rules):
    """rules: {signal: {points, hook}}. Returns (score:int, hook:str)."""
    score = 0
    hooks = []
    market = _mv(lead, "market_type", "b2b")
    for signal, cfg in rules.items():
        if signal in B2B_ONLY_SIGNALS and market != "b2b":
            continue  # don't apply business signals to consumer leads
        check = SIGNALS.get(signal)
        if check is None:
            continue
        try:
            fired = check(lead)
        except (KeyError, TypeError, IndexError):
            fired = False
        if fired:
            score += int(cfg.get("points", 0))
            text = (cfg.get("hook") or "").strip()
            if text:
                hooks.append(_fmt(text, lead))

    # Hooks are signal-driven only. No generic filler line: if nothing fired,
    # the lead simply has no angle for this campaign (score 0) and should be
    # skipped — a blank hook is honest information. Standing script lines belong
    # in VICIdial, not here.
    return score, " | ".join(hooks)


def load_rules(campaign_row):
    try:
        return json.loads(campaign_row["rules"]) if campaign_row else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def rescore_all(conn, campaign_row):
    """Recompute score + call_hook for every lead under the given campaign."""
    rules = load_rules(campaign_row)
    updates = []
    for lead in conn.execute("SELECT * FROM leads"):
        score, hook = evaluate(lead, rules)
        updates.append((score, hook, lead["id"]))
    conn.executemany("UPDATE leads SET score = ?, call_hook = ? WHERE id = ?", updates)
    conn.commit()
    return len(updates)
