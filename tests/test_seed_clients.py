"""Client seed data comes from an optional JSON file, never from the source.

Covers: no file -> nothing seeded; a file -> client + campaigns created once,
with the flag set; a pre-existing flag (older database) -> untouched; and a
broken file -> ignored without stopping the app.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db  # noqa: E402

ENTRY = {
    "flag": "seed_test_client_v1",
    "name": "Test Client Ltd.",
    "contact_name": "Pat Example",
    "email": "pat@example.com",
    "phone": "1-555-010-0000",
    "website": "https://example.com",
    "address": "Toronto, Ontario, Canada",
    "notes": "test",
    "campaigns": [
        {"name": "Test Client — Retail", "industry_slug": "car_dealership",
         "offer_slug": "ict_appointment", "audience": "b2b",
         "country": "Canada", "state": "Ontario", "city": "Toronto"},
        {"name": "Test Client — Hotels", "industry_slug": "hotel",
         "offer_slug": "ict_appointment", "audience": "b2b",
         "country": "Canada", "state": "Ontario", "city": "Toronto"},
    ],
}


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """An initialised SQLite database in a temp folder, with no seed file."""
    monkeypatch.setattr(db, "SEED_CLIENTS_FILE", tmp_path / "absent.json")
    path = tmp_path / "leads.db"
    db.init_db(path)
    conn = db.connect(path)
    yield conn, path
    conn.close()


def _clients(conn):
    return [r["name"] for r in conn.execute("SELECT name FROM clients ORDER BY id")]


def test_no_seed_file_means_no_client_rows(fresh_db):
    conn, _ = fresh_db
    assert _clients(conn) == ["Unassigned (house)"]


def test_seed_file_creates_client_and_campaigns_once(fresh_db, tmp_path):
    conn, _ = fresh_db
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps([ENTRY]), encoding="utf-8")

    assert db._seed_clients_from_file(conn, seed) == 1
    assert _clients(conn) == ["Unassigned (house)", "Test Client Ltd."]
    client = conn.execute(
        "SELECT * FROM clients WHERE name = 'Test Client Ltd.'").fetchone()
    assert client["phone"] == "1-555-010-0000"
    assert client["enabled"] == 1
    camps = conn.execute(
        "SELECT name, industry_slug, offer_slug, country, status FROM campaigns "
        "WHERE client_id = ? ORDER BY id", (client["id"],)).fetchall()
    assert [c["name"] for c in camps] == ["Test Client — Retail", "Test Client — Hotels"]
    assert {c["offer_slug"] for c in camps} == {"ict_appointment"}
    assert {c["status"] for c in camps} == {"active"}
    assert db.get_setting(conn, "seed_test_client_v1", "") == "done"

    # Second run: the flag stops a re-seed, even after the user deletes the client.
    conn.execute("DELETE FROM campaigns WHERE client_id = ?", (client["id"],))
    conn.execute("DELETE FROM clients WHERE id = ?", (client["id"],))
    assert db._seed_clients_from_file(conn, seed) == 0
    assert _clients(conn) == ["Unassigned (house)"]


def test_existing_flag_from_older_database_is_respected(fresh_db, tmp_path):
    """Databases seeded by the old hard-coded seeder hold its flag; the file
    seeder keys on that same flag so those rows are neither duplicated nor
    recreated."""
    conn, _ = fresh_db
    db.set_setting(conn, "seed_test_client_v1", "done")
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps([ENTRY]), encoding="utf-8")
    assert db._seed_clients_from_file(conn, seed) == 0
    assert _clients(conn) == ["Unassigned (house)"]


def test_init_db_reads_the_configured_seed_file(tmp_path, monkeypatch):
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps([ENTRY]), encoding="utf-8")
    monkeypatch.setattr(db, "SEED_CLIENTS_FILE", seed)
    path = tmp_path / "leads.db"
    db.init_db(path)
    db.init_db(path)            # a second start must not duplicate anything
    conn = db.connect(path)
    try:
        assert _clients(conn) == ["Unassigned (house)", "Test Client Ltd."]
        assert conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] == 2
    finally:
        conn.close()


def test_flag_defaults_to_a_slug_of_the_name():
    assert db._seed_flag({"name": "Acme & Sons, Inc."}) == "seed_client_acme_sons_inc_v1"
    assert db._seed_flag({"name": "X", "flag": "custom"}) == "custom"


@pytest.mark.parametrize("content", ["not json {", '{"name": "a dict, not a list"}'])
def test_broken_seed_file_is_ignored(fresh_db, tmp_path, content, capsys):
    conn, _ = fresh_db
    seed = tmp_path / "seed.json"
    seed.write_text(content, encoding="utf-8")
    assert db._seed_clients_from_file(conn, seed) == 0
    assert _clients(conn) == ["Unassigned (house)"]
    assert "[seed] ignoring" in capsys.readouterr().out


def test_entries_without_a_name_are_skipped(fresh_db, tmp_path):
    conn, _ = fresh_db
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps([{"flag": "x"}, "junk", {"name": "  "}]), encoding="utf-8")
    assert db._seed_clients_from_file(conn, seed) == 0
    assert _clients(conn) == ["Unassigned (house)"]
