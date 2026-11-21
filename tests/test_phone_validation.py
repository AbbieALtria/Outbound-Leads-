"""Phone validation pairs each phones-enricher result with the number it echoes
in `query`, never by position; a chunk that cannot be paired is skipped and its
numbers stay unvalidated. Outscraper is replaced by canned responses."""
import db
import pipeline


class FakeResp:
    status_code = 200
    text = ""

    def __init__(self, data):
        self._data = data

    def json(self):
        return {"id": "x", "status": "Success", "data": self._data}


def _serve(monkeypatch, data, seen=None):
    def fake_get(url, params=None, headers=None, timeout=None):
        if seen is not None:
            seen.append(params["query"])
        return FakeResp(data)
    monkeypatch.setattr(pipeline.requests, "get", fake_get)


A, B, C = "(813) 555-0100", "(813) 555-0200", "+1 813-555-0300"


def test_results_are_matched_by_echoed_number_not_position(monkeypatch):
    _serve(monkeypatch, [   # answered in the opposite order, and one echoed with a +1
        {"query": "18135550200", "carrier_name": "VERIZON", "carrier_type": "mobile"},
        {"query": "+1 813 555 0100", "carrier_name": "", "carrier_type": ""},
    ])
    out = pipeline.validate_phones("k", [A, B], log=lambda *_: None)
    assert out[B] == {"type": "mobile", "valid": True, "carrier": "VERIZON"}
    assert out[A] == {"type": "invalid", "valid": False, "carrier": ""}


def test_unkeyed_results_fall_back_to_position_only_when_counts_agree(monkeypatch):
    _serve(monkeypatch, [{"carrier_type": "landline", "carrier_name": "AT&T"},
                         {"carrier_type": "voip", "carrier_name": "BANDWIDTH"}])
    out = pipeline.validate_phones("k", [A, B], log=lambda *_: None)
    assert out[A]["type"] == "landline" and out[B]["type"] == "voip"


def test_unkeyed_count_mismatch_skips_the_chunk_with_a_warning(monkeypatch):
    _serve(monkeypatch, [{"carrier_type": "landline", "carrier_name": "AT&T"}])
    warnings = []
    out = pipeline.validate_phones("k", [A, B], log=warnings.append)
    assert out == {}
    assert any("WARNING" in w and "skipping these 2 numbers" in w for w in warnings)


def test_stray_and_unkeyed_rows_are_ignored_when_others_match(monkeypatch):
    _serve(monkeypatch, [
        {"query": "18135550100", "carrier_type": "mobile", "carrier_name": "T"},
        {"query": "19995550000", "carrier_type": "mobile", "carrier_name": "?"},   # not asked
        {"carrier_type": "landline", "carrier_name": "?"},                          # no number
    ])
    warnings = []
    out = pipeline.validate_phones("k", [A, B], log=warnings.append)
    assert set(out) == {A}
    assert any("ignoring 1 result(s) without a number and 1" in w for w in warnings)


def test_chunks_of_25_each_query_once(monkeypatch):
    phones = [f"(813) 555-{i:04d}" for i in range(30)]
    seen = []
    # Echo every number back as asked, so each chunk pairs fully.
    def fake_get(url, params=None, headers=None, timeout=None):
        seen.append(list(params["query"]))
        return FakeResp([{"query": q, "carrier_type": "mobile"} for q in params["query"]])
    monkeypatch.setattr(pipeline.requests, "get", fake_get)
    out = pipeline.validate_phones("k", phones, log=lambda *_: None)
    assert [len(s) for s in seen] == [25, 5]
    assert len(out) == 30 and all(v["valid"] for v in out.values())


def test_verify_lead_phones_leaves_unmatched_numbers_unvalidated(monkeypatch, tmp_path):
    path = tmp_path / "leads.db"
    db.init_db(path)
    conn = db.connect(path)
    for phone in (A, B, C):
        conn.execute("INSERT INTO leads (phone, business_name, pulled_date) VALUES (?, 'B', '2026-11-21')",
                     (phone,))
    conn.commit()
    _serve(monkeypatch, [
        {"query": "18135550100", "carrier_type": "mobile", "carrier_name": "T"},
        {"query": "18135550300", "carrier_type": "", "carrier_name": ""},
    ])                                                  # nothing came back for B
    rows = conn.execute("SELECT id, phone FROM leads").fetchall()
    summary = pipeline.verify_lead_phones(conn, "k", rows, log=lambda *_: None)
    assert summary == {"checked": 2, "invalid": 1, "unmatched": 1}
    got = {r["phone"]: (r["phone_valid"], r["validated_at"] is not None) for r in
           conn.execute("SELECT phone, phone_valid, validated_at FROM leads")}
    assert got[A] == (1, True)
    assert got[C] == (0, True)
    assert got[B] == (1, False)        # untouched: still dialable, still unverified
    conn.close()


def test_phone_key_normalises_country_code():
    assert pipeline._phone_key("(281) 236-8208") == pipeline._phone_key("12812368208")
    assert pipeline._phone_key("+44 20 7946 0958") == "442079460958"
