"""
Offer-driven lead scoring.

A lead carries only NEUTRAL signals (no website, unclaimed listing, review count,
rating, no email, ...). An *offer* decides which signals matter, how many points
each is worth, and the call-hook wording. Each lead is scored under the offer of
the CAMPAIGN it belongs to (leads with no campaign use the default offer). SEO is
just one offer preset among many.
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


def load_rules(offer_row):
    try:
        return json.loads(offer_row["rules"]) if offer_row else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _rescore_rows(conn, rows, rules):
    updates = [(*evaluate(lead, rules), lead["id"]) for lead in rows]
    conn.executemany("UPDATE leads SET score = ?, call_hook = ? WHERE id = ?", updates)
    return len(updates)


def rescore_all(conn, offer_row):
    """Recompute score + call_hook for EVERY lead under one offer's rules. Used for
    the unassigned/global path (leads not attached to a campaign)."""
    n = _rescore_rows(conn, conn.execute("SELECT * FROM leads"), load_rules(offer_row))
    conn.commit()
    return n


def rescore_campaign(conn, campaign):
    """Recompute score + call_hook for one campaign's leads, using that campaign's
    offer. `campaign` is a campaigns-table row."""
    import db
    rules = load_rules(db.offer_for_campaign(conn, campaign))
    rows = conn.execute("SELECT * FROM leads WHERE campaign_id = ?", (campaign["id"],))
    n = _rescore_rows(conn, rows, rules)
    conn.commit()
    return n


def rescore_everything(conn):
    """Rescore all leads: each campaign's leads under its own offer, and any
    unassigned (campaign_id NULL) leads under the default offer."""
    import db
    total = 0
    for campaign in conn.execute("SELECT * FROM campaigns").fetchall():
        total += rescore_campaign(conn, campaign)
    default_offer = db.get_offer(conn, db.default_offer_slug(conn))
    rows = conn.execute("SELECT * FROM leads WHERE campaign_id IS NULL")
    total += _rescore_rows(conn, rows, load_rules(default_offer))
    conn.commit()
    return total
