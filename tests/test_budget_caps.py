"""Daily caps per paid provider: counted per day, stop each provider cleanly at
the limit, reset the next day, and never crash a pull. Every provider is faked."""
import json
from types import SimpleNamespace

import pytest

import budget
import contacts
import db
import email_verify
import pipeline
import site_contacts as sc

TAMPA = {"city": "Tampa", "state": "FL", "country": "United States"}


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "leads.db"
    db.init_db(path)
    return path


@pytest.fixture
def conn(db_path):
    c = db.connect(db_path)
    yield c
    c.close()


# --- the counter itself ------------------------------------------------------------------

def test_defaults_are_conservative_and_never_unlimited(conn):
    for key, label, unit, default in budget.PROVIDERS:
        assert default > 0, key
        assert budget.cap(conn, key) == default
        assert budget.remaining(conn, key) == default


def test_record_used_remaining_and_zero_means_unlimited(conn):
    budget.set_cap(conn, "apollo", 3)
    assert budget.remaining(conn, "apollo") == 3
    budget.record(conn, "apollo", 2)
    assert budget.used(conn, "apollo") == 2 and budget.remaining(conn, "apollo") == 1
    assert budget.allowance(conn, "apollo", 5) == 1
    budget.record(conn, "apollo", 5)                      # over-run still counted
    assert budget.remaining(conn, "apollo") == 0 and budget.allowance(conn, "apollo", 5) == 0
    assert budget.cap_message(conn, "apollo") == "Apollo daily cap reached: 7 of 3 lookups used"
    budget.set_cap(conn, "apollo", 0)                     # explicit "no limit"
    assert budget.remaining(conn, "apollo") is None and budget.allowance(conn, "apollo", 5) == 5
    assert budget.usage_report(conn)[1]["remaining"] is None


def test_counter_resets_the_next_day(conn, monkeypatch):
    budget.set_cap(conn, "phone", 2)
    budget.record(conn, "phone", 2)
    assert budget.remaining(conn, "phone") == 0
    monkeypatch.setattr(budget, "today", lambda: "2099-01-02")
    assert budget.used(conn, "phone") == 0 and budget.remaining(conn, "phone") == 2
    assert budget.used(conn, "phone", day=budget_today_before()) == 2   # yesterday kept


def budget_today_before():
    from datetime import date
    return str(date.today())


def test_unreadable_cap_setting_falls_back_to_default(conn):
    db.set_setting(conn, "cap_email", "lots")
    assert budget.cap(conn, "email") == 200


# --- Outscraper: a pull stops cleanly with a partial result ------------------------------

def _place(i):
    return {"name": f"Bay Heating {i}", "phone": f"(813) 555-{i:04d}", "place_id": f"P{i}",
            "site": "", "street": f"{i} Main St", "postal_code": "33602", "city": "Tampa",
            "state": "FL", "full_address": f"{i} Main St, Tampa, FL 33602",
            "type": "HVAC contractor", "business_status": "OPERATIONAL", "reviews": 12,
            "rating": 4.5, "verified": True}


def test_pull_stops_at_the_outscraper_cap_and_finishes_cleanly(conn, db_path, monkeypatch):
    budget.set_cap(conn, "outscraper", 7)
    conn.commit()
    counter, asked = [0], []

    def fake_outscraper(api_key, query_text, limit, enrich_contacts=False):
        asked.append(limit)
        out = [_place(counter[0] + i) for i in range(limit)]
        counter[0] += limit
        return out

    monkeypatch.setattr(pipeline, "query_outscraper", fake_outscraper)
    monkeypatch.setattr(pipeline, "validate_phones", lambda *a, **k: {})
    monkeypatch.setattr(pipeline, "check_new_websites", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_: None)
    run_id = conn.execute("INSERT INTO pull_runs (started_at, industry, target) VALUES (?, 'hvac', 10)",
                          (db.now_iso(),)).lastrowid
    conn.commit()

    added = pipeline.run_pull(["hvac"], 10, "k", location=TAMPA, run_id=run_id,
                              db_path=db_path, log=lambda *_: None)
    run = conn.execute("SELECT * FROM pull_runs WHERE id = ?", (run_id,)).fetchone()
    assert added == 7 and run["status"] == "done"             # partial, but closed normally
    assert asked == [7]                                       # the request was trimmed to the cap
    assert "Outscraper daily cap reached: 7 of 7 records fetched used" in run["message"]
    assert budget.used(conn, "outscraper") == 7


def test_pull_with_nothing_left_today_makes_no_request(conn, db_path, monkeypatch):
    budget.set_cap(conn, "outscraper", 5)
    budget.record(conn, "outscraper", 5)
    calls = []
    monkeypatch.setattr(pipeline, "query_outscraper", lambda *a, **k: calls.append(a) or [])
    run_id = conn.execute("INSERT INTO pull_runs (started_at, industry, target) VALUES (?, 'hvac', 10)",
                          (db.now_iso(),)).lastrowid
    conn.commit()
    added = pipeline.run_pull(["hvac"], 10, "k", location=TAMPA, run_id=run_id,
                              db_path=db_path, log=lambda *_: None)
    run = conn.execute("SELECT status, message FROM pull_runs WHERE id = ?", (run_id,)).fetchone()
    assert added == 0 and calls == [] and run["status"] == "done"
    assert "Outscraper daily cap reached: 5 of 5" in run["message"]


# --- phone validator ----------------------------------------------------------------------

def test_phone_validation_checks_only_what_the_cap_allows(conn, monkeypatch):
    budget.set_cap(conn, "phone", 2)
    for i in range(3):
        conn.execute("INSERT INTO leads (phone, business_name, pulled_date) VALUES (?, 'B', '2026-11-01')",
                     (f"(813) 555-01{i:02d}",))
    conn.commit()
    asked = []

    def fake_get(url, params=None, headers=None, timeout=None):
        asked.extend(params["query"])
        return SimpleNamespace(status_code=200, text="", json=lambda: {
            "data": [{"query": q, "carrier_type": "mobile"} for q in params["query"]]})
    monkeypatch.setattr(pipeline.requests, "get", fake_get)

    rows = conn.execute("SELECT id, phone FROM leads ORDER BY id").fetchall()
    summary = pipeline.verify_lead_phones(conn, "k", rows, log=lambda *_: None)
    assert summary["checked"] == 2 and len(asked) == 2
    assert summary["cap_note"] == "Phone validator daily cap reached: 2 of 2 numbers used"
    unvalidated = conn.execute("SELECT COUNT(*) FROM leads WHERE validated_at IS NULL").fetchone()[0]
    assert unvalidated == 1
    # Nothing left: no request, clear note.
    summary = pipeline.verify_lead_phones(conn, "k", rows, log=lambda *_: None)
    assert summary["checked"] == 0 and len(asked) == 2 and "cap reached" in summary["cap_note"]


# --- email verifier -------------------------------------------------------------------------

class StubProvider:
    name = "stub"
    credits_per_check = 1

    def __init__(self):
        self.calls = []

    def verify(self, email):
        self.calls.append(email)
        return "valid", ""


def test_email_verification_stops_at_the_cap(conn):
    budget.set_cap(conn, "email", 1)
    for i, email in enumerate(["a@x.com", "b@x.com"]):
        conn.execute("INSERT INTO leads (phone, business_name, pulled_date, email) VALUES (?, 'B', '2026-11-01', ?)",
                     (f"(813) 555-02{i:02d}", email))
    conn.commit()
    stub = StubProvider()
    summary = email_verify.verify_leads(conn, conn.execute("SELECT id, email FROM leads ORDER BY id").fetchall(),
                                        log=lambda *_: None, prov=stub)
    assert stub.calls == ["a@x.com"] and summary["checked"] == 1
    assert summary["cap_note"] == "Email verifier daily cap reached: 1 of 1 checks used"
    assert budget.used(conn, "email") == 1


# --- Apollo and Claude inside an enrichment run ---------------------------------------------

class Resp:
    status_code = 200
    text = ""

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def test_apollo_stops_at_the_cap_and_the_run_says_so(conn, monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "k")
    budget.set_cap(conn, "apollo", 1)
    calls = []
    monkeypatch.setattr(contacts.requests, "get", lambda url, params=None, headers=None, timeout=None:
                        calls.append("org") or Resp({"organization": {"estimated_num_employees": 12}}))
    monkeypatch.setattr(contacts.requests, "post", lambda url, json=None, headers=None, timeout=None:
                        calls.append("people") or Resp({"people": []}))
    monkeypatch.setattr(sc, "find_team_contact", lambda *a, **k: {})
    conn.execute("INSERT INTO leads (phone, business_name, pulled_date, website) "
                 "VALUES ('(813) 555-0300', 'Acme', '2026-11-01', 'https://acme.example')")
    conn.commit()
    rows = conn.execute("SELECT id, website, contact, email, employee_count, apollo_checked_at FROM leads").fetchall()
    result = contacts.enrich_leads(conn, rows, want_company_size=True, log=lambda *_: None)
    assert calls == ["org"]                                   # the second lookup was not made
    assert result["org_calls"] == 1 and result["checked"] == 0
    assert result["cap_note"] == "Apollo daily cap reached: 1 of 1 lookups used"
    # Not marked as asked: the people search is still owed, another day.
    assert conn.execute("SELECT apollo_checked_at FROM leads").fetchone()[0] is None


PAGE = "Harbour Dental was started by Maya Chen in 2009 and she still sees patients every weekday. " * 3


def test_claude_tier_honours_the_daily_cap(conn, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    budget.set_cap(conn, "claude", 1)
    sc._ai_cache.clear()
    requests_made = []

    class FakeClient:
        def __init__(self):
            self.messages = self

        def create(self, **kw):
            requests_made.append(kw)
            return SimpleNamespace(stop_reason="end_turn",
                                   content=[SimpleNamespace(type="text", text=json.dumps({"people": []}))])
    monkeypatch.setattr(sc, "_client", lambda: FakeClient())
    monkeypatch.setattr(sc, "_fetch", lambda url, timeout=6: f"<html><body>{PAGE}</body></html>")
    for i, site in enumerate(["https://one.example", "https://two.example"]):
        conn.execute("INSERT INTO leads (phone, business_name, pulled_date, website) VALUES (?, 'B', '2026-11-01', ?)",
                     (f"(813) 555-04{i:02d}", site))
    conn.commit()
    rows = conn.execute("SELECT id, website, contact, email, employee_count, apollo_checked_at FROM leads").fetchall()
    result = contacts.enrich_leads(conn, rows, want_company_size=False, log=lambda *_: None)
    assert len(requests_made) == 1 and result["ai_calls"] == 1 and result["ai_capped"] == 1
    assert result["cap_note"] == "Claude daily cap reached: 1 of 1 calls used"
    assert budget.used(conn, "claude") == 1
    sc._ai_cache.clear()


# --- Settings page -------------------------------------------------------------------------

def test_settings_shows_usage_and_saves_caps(monkeypatch):
    import app as A
    import users
    monkeypatch.setattr(users, "auth_enabled", lambda conn: False)
    client = A.app.test_client()
    body = client.get("/settings").get_data(as_text=True)
    assert "Daily caps" in body and 'name="cap_apollo"' in body and "records fetched" in body

    r = client.post("/settings/general", data={"target": "20", "cap_apollo": "5", "cap_claude": "0",
                                               "cap_email": "nope"})
    assert r.status_code == 302
    conn = db.connect()
    try:
        assert budget.cap(conn, "apollo") == 5
        assert budget.cap(conn, "claude") == 0            # explicit no-limit is allowed
        assert budget.cap(conn, "email") == 200           # junk ignored, default kept
        budget.record(conn, "apollo", 5)
    finally:
        conn.close()
    body = client.get("/settings").get_data(as_text=True)
    assert "5 of 5" in body and "cap reached" in body
