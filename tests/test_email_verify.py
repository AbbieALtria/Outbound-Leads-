"""Email verification: dormant without a key, statuses written per lead, a
provider failure stops the spend, invalid emails leave the exports, and the
dashboard route estimates before it spends. No network: the provider is a stub
and requests.get is forbidden."""
import csv
import io
import json
import time

import pytest

import db
import email_verify


class StubProvider:
    name = "stub"
    credits_per_check = 1

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def verify(self, email):
        self.calls.append(email)
        answer = self.answers[email]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("tests must not call the network")
    monkeypatch.setattr(email_verify.requests, "get", boom)
    monkeypatch.setattr(email_verify.requests, "post", boom)


def _add_lead(conn, phone, email, status="", checked_at=None):
    cur = conn.execute(
        "INSERT INTO leads (phone, business_name, pulled_date, email, email_status, "
        "email_checked_at) VALUES (?, 'Biz', '2026-11-21', ?, ?, ?)",
        (phone, email, status, checked_at))
    conn.commit()
    return cur.lastrowid


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "leads.db"
    db.init_db(path)
    c = db.connect(path)
    yield c
    c.close()


# --- dormant without a key -------------------------------------------------------

def test_disabled_without_key(monkeypatch, conn):
    monkeypatch.delenv("EMAIL_VERIFY_API_KEY", raising=False)
    assert not email_verify.enabled()
    assert email_verify.provider() is None
    assert email_verify.estimate_credits(40) == 0
    lid = _add_lead(conn, "(813) 555-0001", "a@example.com")
    summary = email_verify.verify_leads(conn, conn.execute(
        "SELECT id, email FROM leads").fetchall(), log=lambda *_: None)
    assert summary["checked"] == 0 and summary["credits"] == 0
    row = conn.execute("SELECT email_status, email_checked_at FROM leads WHERE id = ?",
                       (lid,)).fetchone()
    assert (row["email_status"], row["email_checked_at"]) == ("", None)


def test_enabled_needs_a_known_provider(monkeypatch):
    monkeypatch.setenv("EMAIL_VERIFY_API_KEY", "k")
    monkeypatch.setenv("EMAIL_VERIFY_PROVIDER", "nobody")
    assert not email_verify.enabled()
    monkeypatch.setenv("EMAIL_VERIFY_PROVIDER", "zerobounce")
    assert email_verify.enabled()
    assert isinstance(email_verify.provider(), email_verify.ZeroBounce)
    assert email_verify.estimate_credits(40) == 40


# --- statuses --------------------------------------------------------------------

def test_statuses_written_and_counted(conn):
    answers = {
        "ok@example.com": ("valid", ""), "dead@example.com": ("invalid", "mailbox_not_found"),
        "any@example.com": ("catch_all", ""), "trap@example.com": ("risky", "spamtrap"),
        "who@example.com": ("unknown", "timeout"),
    }
    ids = {e: _add_lead(conn, f"(813) 555-01{i:02d}", e) for i, e in enumerate(answers)}
    _add_lead(conn, "(813) 555-0199", "")                        # no email: skipped
    stub = StubProvider(answers)
    summary = email_verify.verify_leads(
        conn, conn.execute("SELECT id, email FROM leads").fetchall(),
        log=lambda *_: None, prov=stub)

    assert sorted(stub.calls) == sorted(answers)
    assert summary["checked"] == 5 and summary["credits"] == 5 and summary["error"] == ""
    assert {s: summary[s] for s in email_verify.STATUSES} == {
        "valid": 1, "invalid": 1, "catch_all": 1, "risky": 1, "unknown": 1}
    for email, lid in ids.items():
        row = conn.execute("SELECT email_status, email_checked_at FROM leads WHERE id = ?",
                           (lid,)).fetchone()
        assert row["email_status"] == answers[email][0]
        assert row["email_checked_at"]


def test_provider_failure_stops_the_run_and_is_reported(conn):
    a = _add_lead(conn, "(813) 555-0201", "a@example.com")
    b = _add_lead(conn, "(813) 555-0202", "b@example.com")
    c = _add_lead(conn, "(813) 555-0203", "c@example.com")
    stub = StubProvider({"a@example.com": ("valid", ""),
                         "b@example.com": RuntimeError("stub: out of credits"),
                         "c@example.com": ("valid", "")})
    summary = email_verify.verify_leads(
        conn, conn.execute("SELECT id, email FROM leads ORDER BY id").fetchall(),
        log=lambda *_: None, prov=stub)
    assert summary["checked"] == 1 and summary["credits"] == 1
    assert "out of credits" in summary["error"]
    status = lambda lid: conn.execute(
        "SELECT email_status FROM leads WHERE id = ?", (lid,)).fetchone()[0]
    assert (status(a), status(b), status(c)) == ("valid", "", "")
    assert stub.calls == ["a@example.com", "b@example.com"]   # c was never sent


@pytest.mark.parametrize("raw,expected", [
    ("valid", "valid"), ("invalid", "invalid"), ("catch-all", "catch_all"),
    ("unknown", "unknown"), ("spamtrap", "risky"), ("abuse", "risky"),
    ("do_not_mail", "risky"), ("", "unknown"), ("something-new", "unknown"),
])
def test_zerobounce_status_mapping(raw, expected):
    assert email_verify.ZeroBounce.map_status(raw) == expected


def test_zerobounce_parses_a_response(monkeypatch):
    class Resp:
        status_code = 200
        text = ""
        def json(self):
            return {"status": "catch-all", "sub_status": "", "address": "x@y.com"}
    seen = {}
    def fake_get(url, params=None, timeout=None):
        seen.update(url=url, params=params)
        return Resp()
    monkeypatch.setattr(email_verify.requests, "get", fake_get)
    assert email_verify.ZeroBounce("key-1").verify("x@y.com") == ("catch_all", "")
    assert seen["params"]["email"] == "x@y.com" and seen["params"]["api_key"] == "key-1"


def test_zerobounce_error_body_raises(monkeypatch):
    class Resp:
        status_code = 200
        text = ""
        def json(self):
            return {"error": "Invalid API key"}
    monkeypatch.setattr(email_verify.requests, "get", lambda *a, **k: Resp())
    with pytest.raises(RuntimeError, match="Invalid API key"):
        email_verify.ZeroBounce("bad").verify("x@y.com")


# --- exports and routes (Flask test client on the conftest's temp database) ------

@pytest.fixture
def client(monkeypatch):
    import app as A
    import users
    monkeypatch.setattr(users, "auth_enabled", lambda conn: False)
    return A.app.test_client()


def _rows(resp):
    text = resp.get_data(as_text=True).lstrip("\N{BYTE ORDER MARK}")
    return list(csv.DictReader(io.StringIO(text)))


def test_pages_render_with_and_without_a_provider(client, monkeypatch):
    for page in ("/", "/history", "/settings"):
        r = client.get(page)
        assert r.status_code == 200, page
        body = r.get_data(as_text=True)
        if page != "/settings":
            assert "Verify emails" in body
            assert "email-verify-modal" not in body         # dormant: no modal markup
        else:
            assert "EMAIL_VERIFY_API_KEY" in body
    monkeypatch.setenv("EMAIL_VERIFY_API_KEY", "k")
    for page in ("/", "/history"):
        body = client.get(page).get_data(as_text=True)
        assert "email-verify-modal" in body and "zerobounce" in body
    assert "zerobounce key present" in client.get("/settings").get_data(as_text=True)


def test_exports_leave_out_invalid_emails(client):
    conn = db.connect()
    good = _add_lead(conn, "(813) 555-0301", "good@example.com", "valid", "2026-11-21T00:00:00")
    bad = _add_lead(conn, "(813) 555-0302", "bad@example.com", "invalid", "2026-11-21T00:00:00")
    risky = _add_lead(conn, "(813) 555-0303", "risky@example.com", "risky", "2026-11-21T00:00:00")
    conn.close()

    by_phone = {r["phone"]: r for r in _rows(client.get("/export.csv"))}
    assert by_phone["(813) 555-0301"]["email"] == "good@example.com"
    assert by_phone["(813) 555-0302"]["email"] == ""          # lead kept, email dropped
    assert by_phone["(813) 555-0303"]["email"] == "risky@example.com"

    vici = {r["Name"] + r["Phone"]: r for r in _rows(client.get("/export/vicidial.csv"))}
    emails = {r["Email"] for r in vici.values()}
    assert "good@example.com" in emails and "bad@example.com" not in emails
    assert len(vici) >= 3                                       # the invalid-email lead is still dialled


def test_estimate_counts_unchecked_emails_and_run_refuses_without_key(client, monkeypatch):
    monkeypatch.delenv("EMAIL_VERIFY_API_KEY", raising=False)
    conn = db.connect()
    run = conn.execute("INSERT INTO pull_runs (started_at, industry, target) "
                       "VALUES (?, 'hvac', 5)", (db.now_iso(),)).lastrowid
    for i, (email, status, at) in enumerate([
            ("new1@example.com", "", None), ("new2@example.com", "", None),
            ("done@example.com", "valid", "2026-11-21T00:00:00"), ("", "", None)]):
        conn.execute("INSERT INTO leads (phone, business_name, pulled_date, email, "
                     "email_status, email_checked_at, run_id) VALUES (?, 'B', '2026-11-21', "
                     "?, ?, ?, ?)", (f"(813) 555-04{i:02d}", email, status, at, run))
    conn.commit()
    conn.close()

    est = client.post("/api/verify_emails/estimate", json={"run_id": run}).get_json()
    assert est == {"enabled": False, "provider": "zerobounce", "count": 2, "credits": 0}
    est = client.post("/api/verify_emails/estimate",
                      json={"run_id": run, "recheck": True}).get_json()
    assert est["count"] == 3
    r = client.post("/api/verify_emails", json={"run_id": run})
    assert r.status_code == 400 and "EMAIL_VERIFY_API_KEY" in r.get_json()["error"]


def test_run_route_checks_only_unchecked_emails(client, monkeypatch):
    stub = StubProvider({"n1@example.com": ("valid", ""), "n2@example.com": ("invalid", "x")})
    monkeypatch.setattr(email_verify, "enabled", lambda: True)
    monkeypatch.setattr(email_verify, "provider", lambda: stub)
    conn = db.connect()
    run = conn.execute("INSERT INTO pull_runs (started_at, industry, target) "
                       "VALUES (?, 'hvac', 5)", (db.now_iso(),)).lastrowid
    for i, (email, status, at) in enumerate([
            ("n1@example.com", "", None), ("n2@example.com", "", None),
            ("old@example.com", "valid", "2026-11-21T00:00:00")]):
        conn.execute("INSERT INTO leads (phone, business_name, pulled_date, email, "
                     "email_status, email_checked_at, run_id) VALUES (?, 'B', '2026-11-21', "
                     "?, ?, ?, ?)", (f"(813) 555-05{i:02d}", email, status, at, run))
    conn.commit()
    conn.close()

    r = client.post("/api/verify_emails", json={"run_id": run})
    assert r.status_code == 200 and r.get_json()["count"] == 2
    for _ in range(100):                      # background worker; wait for it
        if not client.get("/api/verify_emails/status").get_json()["running"]:
            break
        time.sleep(0.05)
    last = client.get("/api/verify_emails/status").get_json()["last"]
    assert last["checked"] == 2 and last["valid"] == 1 and last["invalid"] == 1
    assert sorted(stub.calls) == ["n1@example.com", "n2@example.com"]
    conn = db.connect()
    got = {r["email"]: r["email_status"] for r in conn.execute(
        "SELECT email, email_status FROM leads WHERE run_id = ?", (run,))}
    conn.close()
    assert got == {"n1@example.com": "valid", "n2@example.com": "invalid",
                   "old@example.com": "valid"}
