"""Hardening step 1: the legacy CSV import runs once per file, the preset
refresh never reverts an offer a person edited, and Apollo is never asked the
same question twice (partial or empty answers included) unless forced."""
import json
import sqlite3

import pytest

import contacts
import db
import site_contacts

CSV_HEADER = "phone,business_name,address,city,state,website,category,industry,score,call_hook\n"
ROW1 = "(813) 555-0100,Alpha HVAC,1 Main St,Tampa,FL,,HVAC,hvac,10,\n"
ROW2 = "(813) 555-0200,Beta Cooling,2 Main St,Tampa,FL,,HVAC,hvac,20,\n"
ROW3 = "(813) 555-0300,Gamma Air,3 Main St,Tampa,FL,,HVAC,hvac,30,\n"


@pytest.fixture
def legacy_dir(tmp_path, monkeypatch):
    """A script folder of our own, so the test controls which legacy files exist."""
    monkeypatch.setattr(db, "SCRIPT_DIR", tmp_path)
    monkeypatch.setattr(db, "OLD_SEEN_DB", tmp_path / "seen_leads.sqlite3")
    return tmp_path


def _count(path):
    conn = db.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    finally:
        conn.close()


def _setting(path, key):
    conn = db.connect(path)
    try:
        return db.get_setting(conn, key)
    finally:
        conn.close()


# --- 1a: legacy import runs exactly once -----------------------------------------------

def test_legacy_csv_is_imported_once_and_skipped_after(legacy_dir, capsys):
    csv_file = legacy_dir / "leads_2026-01-05.csv"
    csv_file.write_text(CSV_HEADER + ROW1 + ROW2, encoding="utf-8")
    path = legacy_dir / "leads.db"

    db.init_db(path)
    assert _count(path) == 2
    stamp = db._file_stamp(csv_file)
    assert _setting(path, "legacy_import:leads_2026-01-05.csv") == stamp
    assert "imported leads_2026-01-05.csv: 2 new lead(s)" in capsys.readouterr().out

    # Every later start (even with the table emptied) skips it, and says so.
    conn = db.connect(path)
    conn.execute("DELETE FROM leads")
    conn.commit()
    conn.close()
    db.init_db(path)
    assert _count(path) == 0
    assert "skipping leads_2026-01-05.csv: already imported" in capsys.readouterr().out

    # An edited file has a new stamp and is imported again (safely: OR IGNORE).
    csv_file.write_text(CSV_HEADER + ROW1 + ROW2 + ROW3, encoding="utf-8")
    db.init_db(path)
    assert _count(path) == 3
    assert _setting(path, "legacy_import:leads_2026-01-05.csv") == db._file_stamp(csv_file)
    db.init_db(path)
    assert _count(path) == 3


def test_database_that_already_has_leads_is_marked_not_imported(legacy_dir, capsys):
    path = legacy_dir / "leads.db"
    db.init_db(path)                       # no legacy files yet
    conn = db.connect(path)
    conn.execute("INSERT INTO leads (phone, business_name, pulled_date) VALUES ('(813) 555-0900', 'Live', '2026-11-01')")
    conn.commit()
    conn.close()
    (legacy_dir / "leads_2026-01-05.csv").write_text(CSV_HEADER + ROW1, encoding="utf-8")

    db.init_db(path)                       # the old gate: leads exist -> no import
    assert _count(path) == 1
    assert _setting(path, "legacy_import:leads_2026-01-05.csv") is not None
    assert "marking as imported without importing" in capsys.readouterr().out
    db.init_db(path)
    assert _count(path) == 1
    assert "skipping leads_2026-01-05.csv" in capsys.readouterr().out


def test_old_seen_leads_db_is_imported_once(legacy_dir, capsys):
    seen = legacy_dir / "seen_leads.sqlite3"
    old = sqlite3.connect(seen)
    old.execute("CREATE TABLE seen_leads (phone TEXT, business_name TEXT, first_seen_date TEXT)")
    old.execute("INSERT INTO seen_leads VALUES ('(813) 555-0700', 'Old Co', '2025-03-01')")
    old.commit()
    old.close()
    path = legacy_dir / "leads.db"
    db.init_db(path)
    assert _count(path) == 1
    out = capsys.readouterr().out
    assert "imported seen_leads.sqlite3: 1 new lead(s)" in out
    conn = db.connect(path)
    conn.execute("DELETE FROM leads")
    conn.commit()
    conn.close()
    db.init_db(path)
    assert _count(path) == 0
    assert "skipping seen_leads.sqlite3" in capsys.readouterr().out


# --- 1b: the preset refresh never reverts an edited offer ---------------------------------

def _offer(conn, slug):
    return conn.execute("SELECT rules, pain_keywords, user_edited FROM offers WHERE slug = ?",
                        (slug,)).fetchone()


def test_edited_preset_survives_a_preset_version_bump(tmp_path, monkeypatch):
    path = tmp_path / "leads.db"
    db.init_db(path)
    conn = db.connect(path)
    assert _offer(conn, "seo")["user_edited"] == 0           # fresh presets start untouched
    custom = json.dumps({"no_website": {"points": 99, "hook": "hand-tuned"}})
    conn.execute("UPDATE offers SET rules = ?, pain_keywords = ?, user_edited = 1 WHERE slug = 'seo'",
                 (custom, '["ouch"]'))
    # An untouched preset left behind by an older release: should be refreshed.
    conn.execute("UPDATE offers SET rules = '{}' WHERE slug = 'reputation'")
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "OFFER_PRESET_VERSION", db.OFFER_PRESET_VERSION + 1)
    db.init_db(path)                                          # "restart"
    conn = db.connect(path)
    seo, rep = _offer(conn, "seo"), _offer(conn, "reputation")
    assert seo["rules"] == custom and seo["pain_keywords"] == '["ouch"]'
    assert json.loads(rep["rules"]) == db.OFFER_PRESETS["reputation"]["rules"]
    conn.close()


def test_offers_from_before_the_flag_are_treated_as_edited(tmp_path, monkeypatch):
    path = tmp_path / "leads.db"
    db.init_db(path)
    conn = db.connect(path)
    # Simulate a database from before user_edited existed: flag unset, rows at 0.
    conn.execute("UPDATE offers SET user_edited = 0, rules = '{\"x\": {\"points\": 1}}'")
    conn.execute("DELETE FROM settings WHERE key = 'offers_user_edited_backfill_v1'")
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "OFFER_PRESET_VERSION", db.OFFER_PRESET_VERSION + 1)
    db.init_db(path)
    conn = db.connect(path)
    rows = conn.execute("SELECT user_edited, rules FROM offers").fetchall()
    assert rows and all(r["user_edited"] == 1 for r in rows)
    assert all(r["rules"] == '{"x": {"points": 1}}' for r in rows)   # nothing reverted
    conn.close()


def test_admin_edit_marks_the_offer_as_edited(monkeypatch):
    import app as A
    import users
    monkeypatch.setattr(users, "auth_enabled", lambda conn: False)
    client = A.app.test_client()
    conn = db.connect()
    conn.execute("UPDATE offers SET user_edited = 0 WHERE slug = 'seo'")
    conn.commit()
    offer_id = conn.execute("SELECT id FROM offers WHERE slug = 'seo'").fetchone()["id"]
    conn.close()
    r = client.post(f"/settings/campaigns/{offer_id}/edit", data={"pain_keywords": "slow, rude"})
    assert r.status_code == 302
    conn = db.connect()
    row = _offer(conn, "seo")
    conn.close()
    assert row["user_edited"] == 1 and json.loads(row["pain_keywords"]) == ["slow", "rude"]


# --- 1c: Apollo is asked once, partial answers included ----------------------------------

class Resp:
    status_code = 200
    text = ""

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


@pytest.fixture
def apollo(monkeypatch):
    """Apollo that answers every lookup with a PARTIAL company and NO people."""
    monkeypatch.setenv("APOLLO_API_KEY", "k")
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(("org", params["domain"]))
        return Resp({"organization": {"estimated_num_employees": None, "industry": "",
                                      "annual_revenue_printed": ""}})

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(("people", json["q_organization_domains_list"][0]))
        return Resp({"people": []})

    monkeypatch.setattr(contacts.requests, "get", fake_get)
    monkeypatch.setattr(contacts.requests, "post", fake_post)
    monkeypatch.setattr(site_contacts, "find_team_contact", lambda *a, **k: {})
    return calls


def _rows(conn):
    return conn.execute("SELECT id, website, contact, email, employee_count, apollo_checked_at "
                        "FROM leads ORDER BY id").fetchall()


def test_partial_apollo_answers_are_not_requeried(tmp_path, apollo):
    path = tmp_path / "leads.db"
    db.init_db(path)
    conn = db.connect(path)
    conn.execute("INSERT INTO leads (phone, business_name, pulled_date, website) "
                 "VALUES ('(813) 555-0100', 'Acme', '2026-11-01', 'https://www.acme.example/')")
    conn.commit()

    first = contacts.enrich_leads(conn, _rows(conn), want_company_size=True, log=lambda *_: None)
    assert first["org_calls"] == 1 and first["checked"] == 1
    assert [c[0] for c in apollo] == ["org", "people", "people"]   # titled, then untitled search
    lookup = conn.execute("SELECT * FROM apollo_org_lookups WHERE domain = 'acme.example'").fetchone()
    assert lookup is not None and lookup["employee_count"] is None   # the partial answer, remembered
    assert _rows(conn)[0]["apollo_checked_at"]

    apollo.clear()
    second = contacts.enrich_leads(conn, _rows(conn), want_company_size=True, log=lambda *_: None)
    assert apollo == []                                              # nothing re-spent
    assert second["org_calls"] == 0 and second["checked"] == 0
    assert second["skipped_already_looked_up"] == 2                  # company + person

    forced = contacts.enrich_leads(conn, _rows(conn), want_company_size=True, force=True,
                                   log=lambda *_: None)
    assert [c[0] for c in apollo] == ["org", "people", "people"]     # an explicit re-enrich asks again
    assert forced["org_calls"] == 1 and forced["checked"] == 1
    conn.close()


def test_two_leads_on_one_domain_cost_one_company_lookup_across_runs(tmp_path, apollo):
    path = tmp_path / "leads.db"
    db.init_db(path)
    conn = db.connect(path)
    conn.execute("INSERT INTO leads (phone, business_name, pulled_date, website) "
                 "VALUES ('(813) 555-0100', 'Acme North', '2026-11-01', 'https://acme.example/north')")
    conn.commit()
    contacts.enrich_leads(conn, _rows(conn), want_company_size=True, log=lambda *_: None)
    conn.execute("INSERT INTO leads (phone, business_name, pulled_date, website) "
                 "VALUES ('(813) 555-0200', 'Acme South', '2026-11-01', 'https://acme.example/south')")
    conn.commit()
    apollo.clear()
    contacts.enrich_leads(conn, _rows(conn), want_company_size=True, log=lambda *_: None)
    assert ("org", "acme.example") not in apollo       # company known from the first run
    assert ("people", "acme.example") in apollo        # the new lead's own search still runs
    conn.close()
