"""Dedupe beyond the UNIQUE phone: place_id, then website domain + street,
then name + postcode. The same place is stored once; a second branch of the
same business at another address is kept. No network: Outscraper is mocked.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db        # noqa: E402
import dedupe    # noqa: E402
import pipeline  # noqa: E402

TAMPA = {"city": "Tampa", "state": "FL", "country": "United States"}


def _isolate(db_module, tmp_path, monkeypatch):
    """Keep init_db from seeding clients or importing the project's own legacy
    leads_*.csv / seen-leads files into the test database."""
    monkeypatch.setattr(db_module, "SEED_CLIENTS_FILE", tmp_path / "absent.json")
    monkeypatch.setattr(db_module, "SCRIPT_DIR", tmp_path)
    monkeypatch.setattr(db_module, "OLD_SEEN_DB", tmp_path / "absent.sqlite3")


def place(name, phone, place_id="", site="", street="", postal="33602",
          city="Tampa", state="FL", category="HVAC contractor"):
    """One Outscraper-shaped Google Maps record."""
    return {
        "name": name, "phone": phone, "place_id": place_id, "site": site,
        "street": street, "postal_code": postal, "city": city, "state": state,
        "full_address": ", ".join(p for p in (street, f"{city}, {state} {postal}") if p),
        "type": category, "business_status": "OPERATIONAL", "reviews": 12,
        "rating": 4.5, "verified": True,
    }


@pytest.fixture
def pull(tmp_path, monkeypatch):
    """A fresh database plus a `run(places_by_query, location=..., locations=...)`
    helper that drives pipeline.run_pull with Outscraper replaced by canned
    records (keyed on the query text, '*' for any) and returns the pull_runs row."""
    _isolate(db, tmp_path, monkeypatch)
    path = tmp_path / "leads.db"
    db.init_db(path)
    monkeypatch.setattr(pipeline, "check_new_websites", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_: None)
    # phone_validation now defaults ON for fresh databases; without this the pull
    # calls Outscraper's phones-enricher for real. Tests must never hit the network.
    monkeypatch.setattr(pipeline, "validate_phones", lambda *a, **k: {})

    def run(places_by_query, **where):
        def fake_outscraper(api_key, query_text, limit, enrich_contacts=False):
            return list(places_by_query.get(query_text) or places_by_query.get("*") or [])
        monkeypatch.setattr(pipeline, "query_outscraper", fake_outscraper)
        conn = db.connect(path)
        cur = conn.execute(
            "INSERT INTO pull_runs (started_at, industry, target) VALUES (?, 'hvac', 10)",
            (db.now_iso(),))
        conn.commit()
        run_id = cur.lastrowid
        conn.close()
        if not where:
            where = {"location": TAMPA}
        pipeline.run_pull(["hvac"], 10, "test-key", run_id=run_id, db_path=path,
                          log=lambda *_: None, **where)
        conn = db.connect(path)
        try:
            return dict(conn.execute("SELECT * FROM pull_runs WHERE id = ?", (run_id,)).fetchone())
        finally:
            conn.close()

    def table(sql, *params):
        conn = db.connect(path)
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    run.table = table
    run.path = path
    return run


def leads(pull):
    return pull.table("SELECT business_name, phone, place_id, site_key, name_key FROM leads ORDER BY id")


def duplicate_rejects(pull):
    return pull.table("SELECT business_name, reason FROM pull_rejects WHERE reason = 'duplicate'")


# --- the three scenarios the brief names -------------------------------------

def test_same_place_with_new_tracking_number_is_skipped(pull):
    original = place("Bay Heating & Air", "(813) 555-0100", place_id="ChIJ-bay",
                     site="https://bayheating.com", street="100 Kennedy Blvd")
    pull({"*": [original]})
    renumbered = dict(original, phone="(813) 555-0199")   # new tracking line, same place
    run = pull({"*": [renumbered]})

    assert [l["business_name"] for l in leads(pull)] == ["Bay Heating & Air"]
    assert leads(pull)[0]["phone"] == "(813) 555-0100"
    assert duplicate_rejects(pull) == [{"business_name": "Bay Heating & Air", "reason": "duplicate"}]
    assert "1 duplicates skipped" in run["message"]
    assert run["status"] == "done"


def test_second_branch_at_another_address_is_kept(pull):
    downtown = place("Bay Heating & Air", "(813) 555-0100", place_id="ChIJ-bay-1",
                     site="https://bayheating.com", street="100 Kennedy Blvd", postal="33602")
    brandon = place("Bay Heating & Air", "(813) 555-0200", place_id="ChIJ-bay-2",
                    site="https://bayheating.com", street="900 Oakfield Dr", postal="33511")
    run = pull({"*": [downtown, brandon]})

    assert len(leads(pull)) == 2
    assert duplicate_rejects(pull) == []
    assert "duplicates" not in run["message"]
    # Same domain, different street -> different site keys, by design.
    assert len({l["site_key"] for l in leads(pull)}) == 2


def test_postcode_sweep_overlap_adds_the_place_once(pull):
    shared = place("Bay Heating & Air", "(813) 555-0100", place_id="ChIJ-bay",
                   site="https://bayheating.com", street="100 Kennedy Blvd")
    zips = [dict(TAMPA, postcode="33602"), dict(TAMPA, postcode="33605")]
    run = pull({"*": [shared]}, locations=zips)

    assert len(leads(pull)) == 1
    assert len(duplicate_rejects(pull)) == 1
    assert run["added"] == 1
    assert "1 duplicates skipped" in run["message"]


# --- each key on its own -------------------------------------------------------

def test_domain_plus_street_catches_a_relisting_without_place_id(pull):
    first = place("Bay Heating & Air", "(813) 555-0100",
                  site="http://www.bayheating.com/", street="100 Kennedy Boulevard, Suite 200")
    relisted = place("Bay Heating and Air Conditioning", "(813) 555-0177",
                     site="https://bayheating.com", street="100 Kennedy Blvd")
    pull({"*": [first]})
    pull({"*": [relisted]})
    assert len(leads(pull)) == 1
    assert len(duplicate_rejects(pull)) == 1


def test_name_plus_postcode_catches_a_relisting_without_website(pull):
    first = place("Cool Breeze Heating Inc.", "(813) 555-0300", street="12 Pine St")
    relisted = place("Cool Breeze Heating", "(813) 555-0333", street="12 Pine Street")
    pull({"*": [first]})
    pull({"*": [relisted]})
    assert len(leads(pull)) == 1


def test_same_name_in_another_postcode_is_a_different_place(pull):
    a = place("Cool Breeze Heating", "(813) 555-0300", street="12 Pine St", postal="33602")
    b = place("Cool Breeze Heating", "(813) 555-0400", street="80 Elm St", postal="33611")
    pull({"*": [a, b]})
    assert len(leads(pull)) == 2


def test_shared_phone_still_dedupes_and_is_counted(pull):
    a = place("Bay Heating & Air", "(813) 555-0100", place_id="ChIJ-a",
              site="https://bayheating.com", street="100 Kennedy Blvd")
    b = place("Gulf Cooling", "(813) 555-0100", place_id="ChIJ-b",
              site="https://gulfcooling.com", street="5 Bay St")
    run = pull({"*": [a, b]})
    assert len(leads(pull)) == 1
    assert "1 duplicates skipped" in run["message"]


def test_new_leads_carry_their_keys(pull):
    pull({"*": [place("Bay Heating & Air", "(813) 555-0100", place_id="ChIJ-bay",
                      site="https://www.bayheating.com/contact", street="100 Kennedy Blvd")]})
    (lead,) = leads(pull)
    assert lead["place_id"] == "ChIJ-bay"
    assert lead["site_key"] == "bayheating.com|100 kennedy blvd"
    assert lead["name_key"] == "bay heating air|33602"


# --- key functions -------------------------------------------------------------

@pytest.mark.parametrize("street,address,expected", [
    ("100 Kennedy Boulevard, Suite 200", "", "100 kennedy blvd"),
    ("100 Kennedy Blvd #200", "", "100 kennedy blvd"),
    ("", "1702 E 5th Ave, Tampa, FL 33605", "1702 e 5th ave"),
    ("", "Tampa, FL 33605", ""),           # no house number: a city, not a street
    ("Downtown", "", ""),
])
def test_norm_street(street, address, expected):
    assert dedupe.norm_street(street, address) == expected


def test_generic_website_hosts_never_make_a_site_key():
    k = dedupe.lead_keys("Shop", "https://shop.wixsite.com/home", street="1 Main St", postcode="33602")
    assert k["site_key"] == ""
    assert k["name_key"] == "shop|33602"


def test_site_key_falls_back_to_postcode_and_offers_an_alternate():
    with_street = dedupe.lead_keys("X", "https://x.com", street="1 Main St", postcode="33602-1234")
    assert with_street["site_key"] == "x.com|1 main st"
    assert with_street["site_key_alt"] == "x.com|pc:33602"
    no_street = dedupe.lead_keys("X", "https://x.com", postcode="33602")
    assert no_street["site_key"] == "x.com|pc:33602"
    assert dedupe.lead_keys("X", "https://x.com")["site_key"] == ""


def test_canadian_postcode_normalises_spacing():
    assert dedupe.lead_keys("X", "", postcode="m5v2t6")["name_key"] == "x|M5V2T6"
    assert dedupe.norm_postcode(" m5v  2t6 ") == "M5V 2T6"


# --- migration -----------------------------------------------------------------

def test_backfill_keys_legacy_rows_once(tmp_path, monkeypatch):
    _isolate(db, tmp_path, monkeypatch)
    path = tmp_path / "leads.db"
    db.init_db(path)
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO leads (phone, business_name, address, city, state, website, "
        "pulled_date, postcode) VALUES ('(813) 555-0100', 'All State AC & Duct Pros', "
        "'1702 E 5th Ave, Tampa, FL 33605', 'Tampa', 'FL', 'https://allstateacducts.com/', "
        "'2026-07-15', '33605')")
    db.set_setting(conn, "dedupe_keys_v1", "")   # as a database from before this change
    conn.commit()
    conn.close()

    db.init_db(path)
    conn = db.connect(path)
    row = dict(conn.execute("SELECT place_id, site_key, name_key FROM leads").fetchone())
    assert row == {"place_id": "", "site_key": "allstateacducts.com|1702 e 5th ave",
                   "name_key": "all state ac duct pros|33605"}
    assert db.get_setting(conn, "dedupe_keys_v1", "") == "done"
    # Keys computed by the migration catch the same place on the next pull.
    assert dedupe.find_existing(conn, dedupe.lead_keys(
        "All State AC and Duct Pros", "http://allstateacducts.com",
        street="1702 East 5th Avenue", postcode="33605")) == "site_key"
    conn.close()
