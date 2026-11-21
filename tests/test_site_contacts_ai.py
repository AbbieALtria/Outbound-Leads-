"""The Claude tier of website contact extraction: dormant without a key, runs
only after the patterns draw a blank, accepts a name only when it is verbatim in
the page inside the quoted passage with enough confidence, truncates the page,
caches per domain, caps calls per run, and reports its call count. The API is a
fake; the pattern tier is exercised for real on canned HTML."""
import json
from types import SimpleNamespace

import pytest

import ai_industries
import claude_config
import contacts
import db
import site_contacts as sc


class FakeClient:
    """Stands in for anthropic.Anthropic(): records every request."""

    def __init__(self, people=None, stop_reason="end_turn", raw=None):
        self.people, self.stop_reason, self.raw = people or [], stop_reason, raw
        self.calls = []
        self.messages = self            # so client.messages.create(...) works

    def create(self, **kw):
        self.calls.append(kw)
        text = self.raw if self.raw is not None else json.dumps({"people": self.people})
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text", text=text)])


# A page the pattern tier misses on purpose: nobody has a title or credential
# next to their name, which is exactly where the Claude tier earns its call.
PAGE = ("Welcome to Harbour Dental. Harbour Dental was started by Maya Chen in 2009 and "
        "she still sees patients every weekday. Lee Park keeps the front desk running and "
        "books every visit. Patients love us: Best cleaning ever - Sam R.")
MAYA_QUOTE = "Harbour Dental was started by Maya Chen in 2009 and she still sees patients every weekday."


def person(name, title, conf, quote):
    return {"name": name, "title": title, "confidence": conf, "quote": quote}


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    sc._ai_cache.clear()
    # Never build a real client in tests.
    monkeypatch.setattr(sc, "_client", lambda: (_ for _ in ()).throw(
        AssertionError("real Anthropic client constructed in a test")))
    yield
    sc._ai_cache.clear()


# --- the shared model constant -----------------------------------------------------

def test_model_is_shared_and_valid(monkeypatch):
    assert ai_industries.MODEL is claude_config.MODEL
    assert sc.claude_config.MODEL == claude_config.MODEL
    assert claude_config.DEFAULT_MODEL == "claude-sonnet-5-5"
    assert "-20" not in claude_config.MODEL          # never a date-suffixed id
    assert ai_industries.enabled() is False           # conftest clears the key


# --- dormant -----------------------------------------------------------------------

def test_dormant_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(sc, "_fetch", lambda url, timeout=6: f"<html><body>{PAGE}</body></html>")
    budget = sc.AIBudget()
    assert sc.ai_enabled() is False
    assert sc.find_team_contact("https://harbourdental.example", ai=budget) == {}
    assert budget.calls == 0 and budget.summary()["ai_calls"] == 0


# --- anti-hallucination checks -------------------------------------------------------

def test_only_verbatim_confident_quoted_names_survive():
    client = FakeClient(people=[
        person("Maya Chen", "founder", 0.95, MAYA_QUOTE),
        person("John Fabricated", "owner", 0.99, "John Fabricated, owner"),           # not on the page
        person("Lee Park", "office manager", 0.5, "Lee Park keeps the front desk running and books every visit."),         # too unsure
        person("Sam R.", "owner", 0.9, "Sam R. is the owner"),                          # quote not on the page
        person("Harbour", "owner", 0.9, "Welcome to Harbour Dental."),                  # name not in its quote? it is - but
    ])
    # The last entry: "Harbour" is verbatim and inside its quote, so the
    # mechanical checks let it through on purpose — the model is trusted on who
    # is a person, the checks only stop invention. Confidence ordering puts the
    # better-attested candidate first.
    budget = sc.AIBudget()
    out = sc.ai_extract_people(PAGE, budget, client=client)
    assert [p["name"] for p in out] == ["Maya Chen", "Harbour"]
    assert out[0]["title"] == "founder" and out[0]["confidence"] == 0.95
    assert budget.calls == 1
    req = client.calls[0]
    assert req["model"] == claude_config.MODEL
    assert req["output_config"]["format"]["schema"] is sc.PEOPLE_SCHEMA


def test_page_text_is_truncated():
    client = FakeClient()
    text = "Harbour Dental. " * 5000                  # ~80k chars
    sc.ai_extract_people(text, sc.AIBudget(), client=client)
    sent = client.calls[0]["messages"][0]["content"]
    assert len(sent) <= sc.AI_MAX_CHARS + len("PAGE TEXT:\n")


def test_refusal_and_bad_json_yield_nothing():
    assert sc.ai_extract_people(PAGE, sc.AIBudget(), client=FakeClient(stop_reason="refusal")) == []
    assert sc.ai_extract_people(PAGE, sc.AIBudget(), client=FakeClient(raw="not json")) == []


def test_api_failure_is_recorded_not_raised():
    class Boom(FakeClient):
        def create(self, **kw):
            raise RuntimeError("401 bad key")
    budget = sc.AIBudget()
    assert sc.ai_extract_people(PAGE, budget, client=Boom()) == []
    assert budget.calls == 1 and "401" in budget.error


# --- cache and cap -------------------------------------------------------------------

def test_one_call_per_domain(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    client = FakeClient(people=[person("Maya Chen", "founder", 0.9, MAYA_QUOTE)])
    monkeypatch.setattr(sc, "_fetch", lambda url, timeout=6: f"<html><body>{PAGE}</body></html>")
    monkeypatch.setattr(sc, "_client", lambda: client)
    budget = sc.AIBudget()
    a = sc.find_team_contact("https://www.harbourdental.example/", ai=budget)
    b = sc.find_team_contact("http://harbourdental.example/other-location", ai=budget)
    assert a["name"] == b["name"] == "Maya Chen" and a["via"] == "ai"
    assert len(client.calls) == 1
    assert budget.calls == 1 and budget.hits == 2 and budget.cache_hits == 1


def test_per_run_cap_stops_calls(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    client = FakeClient()
    monkeypatch.setattr(sc, "_fetch", lambda url, timeout=6: f"<html><body>{PAGE}</body></html>")
    monkeypatch.setattr(sc, "_client", lambda: client)
    budget = sc.AIBudget(cap=1)
    sc.find_team_contact("https://one.example", ai=budget)
    sc.find_team_contact("https://two.example", ai=budget)
    sc.find_team_contact("https://three.example", ai=budget)
    assert len(client.calls) == 1
    assert budget.summary() == {"ai_calls": 1, "ai_hits": 0, "ai_capped": 2,
                                "ai_cache_hits": 0, "ai_error": ""}
    # A domain skipped at the cap is not cached as "nobody here": a later run
    # with budget left still asks.
    assert "two.example" not in sc._ai_cache and "three.example" not in sc._ai_cache


def test_patterns_first_no_call_when_they_find_someone(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    client = FakeClient()
    monkeypatch.setattr(sc, "_client", lambda: client)
    monkeypatch.setattr(sc, "_fetch", lambda url, timeout=6:
                        "<html><body>Meet Dr. Maya Chen, our principal dentist.</body></html>")
    budget = sc.AIBudget()
    found = sc.find_team_contact("https://harbourdental.example", ai=budget)
    assert found["name"] == "Dr. Maya Chen" and "via" not in found
    assert client.calls == [] and budget.calls == 0


def test_team_page_text_preferred_over_homepage(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    team = ("Our team. " * 30) + PAGE
    pages = {"https://x.example": "<html><body>" + ("Welcome home. " * 40) +
             '<a href="/about">About us</a></body></html>',
             "https://x.example/about": f"<html><body>{team}</body></html>"}
    monkeypatch.setattr(sc, "_fetch", lambda url, timeout=6: pages.get(url, ""))
    client = FakeClient(people=[person("Maya Chen", "founder", 0.9, MAYA_QUOTE)])
    monkeypatch.setattr(sc, "_client", lambda: client)
    found = sc.find_team_contact("https://x.example", ai=sc.AIBudget())
    assert found["source_url"] == "https://x.example/about"
    assert "was started by Maya Chen" in client.calls[0]["messages"][0]["content"]


# --- reported through the enrichment run ---------------------------------------------

def test_enrich_leads_reports_ai_call_count(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    path = tmp_path / "leads.db"
    db.init_db(path)
    conn = db.connect(path)
    conn.execute("INSERT INTO leads (phone, business_name, pulled_date, website) "
                 "VALUES ('(813) 555-0100', 'Harbour Dental', '2026-11-21', 'https://harbourdental.example')")
    conn.execute("INSERT INTO leads (phone, business_name, pulled_date, website, contact) "
                 "VALUES ('(813) 555-0101', 'Known Co', '2026-11-21', 'https://known.example', 'Pat Known')")
    conn.commit()
    monkeypatch.setattr(sc, "_fetch", lambda url, timeout=6: f"<html><body>{PAGE}</body></html>")
    client = FakeClient(people=[person("Maya Chen", "founder", 0.9, MAYA_QUOTE)])
    monkeypatch.setattr(sc, "_client", lambda: client)

    rows = conn.execute("SELECT id, website, contact, email, employee_count FROM leads ORDER BY id").fetchall()
    result = contacts.enrich_leads(conn, rows, want_company_size=False, log=lambda *_: None)

    assert result["ai_calls"] == 1 and result["ai_hits"] == 1 and result["ai_capped"] == 0
    assert result["site_tried"] == 1 and result["site_hits"] == 1
    assert result["checked"] == 0 and result["credits"] == 0        # no Apollo
    got = {r["business_name"]: (r["contact"], r["contact_title"]) for r in
           conn.execute("SELECT business_name, contact, contact_title FROM leads")}
    assert got["Harbour Dental"] == ("Maya Chen", "founder")
    assert got["Known Co"] == ("Pat Known", "")                       # never overwritten
    conn.close()
