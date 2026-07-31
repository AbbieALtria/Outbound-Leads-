"""
AI-suggested industries for a campaign.

Asks Claude for additional target verticals related to a campaign's offer and
already-curated industries, each shaped for a Google Maps business search:
slug, label, search-query phrase, and franchise/chain names to exclude.

Suggestions are ONLY suggestions — nothing is written to the catalog here. The
caller reviews (and can edit) each one and approves it through the normal
add-industry path, so a human always gates an unproven query before it costs
Outscraper credits.

Dormant unless ANTHROPIC_API_KEY is set (env / Railway variable — never in code,
the DB, or the UI), same pattern as OUTSCRAPER_API_KEY / APOLLO_API_KEY.
"""

import os
import re

MODEL = "claude-sonnet-5"

# Shape we ask Claude to return. Structured outputs guarantee it parses.
SUGGESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "industries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string",
                             "description": "lower_snake_case identifier, e.g. dental_lab"},
                    "label": {"type": "string",
                              "description": "Human-readable name, e.g. Dental Lab"},
                    "query": {"type": "string",
                              "description": "Google Maps search phrase containing the literal {city} placeholder, e.g. 'dental lab in {city}'"},
                    "chains": {"type": "array", "items": {"type": "string"},
                               "description": "Known franchise/chain brand names to exclude (may be empty)"},
                    "why": {"type": "string",
                            "description": "One short line on why this vertical fits the campaign"},
                },
                "required": ["slug", "label", "query", "chains", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["industries"],
    "additionalProperties": False,
}


def enabled():
    """True when the Anthropic API key is configured."""
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def _slugify(text):
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")


def suggest(campaign_name, offer_name, existing_labels, country="", count=6):
    """Ask Claude for `count` related industries for this campaign.
    Returns a list of {slug, label, query, chains, why}. Raises on API failure so
    the caller can show the real error."""
    import anthropic

    client = anthropic.Anthropic()      # reads ANTHROPIC_API_KEY from the env
    known = ", ".join(sorted(existing_labels)) or "(none yet)"
    prompt = (
        f"I run a B2B lead-generation platform that finds businesses via Google Maps "
        f"searches, then cold-calls them.\n\n"
        f"Campaign: {campaign_name}\n"
        f"Offer being sold: {offer_name}\n"
        f"{f'Country/region: {country}' if country else ''}\n"
        f"Industries already in my catalog: {known}\n\n"
        f"Suggest {count} ADDITIONAL business types (verticals) that would be good "
        f"targets for this offer and are NOT already in my catalog. For each:\n"
        f"- slug: lower_snake_case\n"
        f"- label: short human-readable name\n"
        f"- query: a natural Google Maps search phrase that MUST contain the literal "
        f"placeholder {{city}} (e.g. 'dental lab in {{city}}')\n"
        f"- chains: known franchise/chain brand names in that vertical to exclude "
        f"(local branches of national chains can't buy). Empty list if none.\n"
        f"- why: one short line on why this vertical fits the offer.\n\n"
        f"Favour verticals with real local businesses that own their own phone line "
        f"and buying decisions."
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"format": {"type": "json_schema", "schema": SUGGESTION_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request.")
    import json
    text = next((b.text for b in resp.content if b.type == "text"), "")
    data = json.loads(text or "{}")

    known_slugs = {_slugify(l) for l in existing_labels}
    out = []
    for item in data.get("industries", []):
        label = (item.get("label") or "").strip()
        slug = _slugify(item.get("slug") or label)
        if not label or not slug or slug in known_slugs:
            continue          # drop anything we already have
        query = (item.get("query") or "").strip()
        if "{city}" not in query:
            query = f"{label.lower()} in {{city}}"
        out.append({
            "slug": slug, "label": label, "query": query,
            "chains": [c.strip() for c in (item.get("chains") or []) if str(c).strip()],
            "why": (item.get("why") or "").strip(),
        })
        known_slugs.add(slug)
    return out
