"""The one place the Claude model is named.

Both AI features (industry suggestions in ai_industries.py, decision-maker
extraction in site_contacts.py) read MODEL from here, so a model change is one
edit — or none: ANTHROPIC_MODEL in the environment overrides it.

Current model IDs (Anthropic, Nov 2026): claude-opus-5-5 (most capable default,
$4/$20 per MTok), claude-sonnet-5-5 (current Sonnet, $2/$10), claude-haiku-5-5
($0.10/$0.50). The previous value here, "claude-sonnet-5", is a valid but
previous-generation Sonnet; the same tier's current generation is used instead.
IDs are complete as written — never append a date suffix.

Dormant unless ANTHROPIC_API_KEY is set (env / Railway variable — never in code,
the DB, or the UI), same pattern as OUTSCRAPER_API_KEY / APOLLO_API_KEY.
"""
import os

DEFAULT_MODEL = "claude-sonnet-5-5"
MODEL = os.environ.get("ANTHROPIC_MODEL", "").strip() or DEFAULT_MODEL


def api_key():
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def enabled():
    """True when the Anthropic API key is configured."""
    return bool(api_key())
