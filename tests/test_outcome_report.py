"""The call-outcome report: per campaign, per signal, with/without rates and
sample sizes; under-sampled signals are flagged; suggestions are computed but
NOTHING is written. Synthetic leads and outcomes, no network."""
import hashlib
import json

import pytest

import db
import outcome_report as rep


def _fresh(tmp_path):
    path = tmp_path / "leads.db"
    db.init_db(path)
    return path


def _campaign(conn, name="Camp", offer_slug="seo"):
    cur = conn.execute(
        "INSERT INTO campaigns (name, offer_slug, created_at) VALUES (?, ?, ?)",
        (name, offer_slug, db.now_iso()))
    return cur.lastrowid


_n = [0]


def _lead(conn, campaign_id, status, website="", reviews=50, unclaimed=0):
    _n[0] += 1
    conn.execute(
        "INSERT INTO leads (phone, business_name, pulled_date, status, website, reviews, "
        "unclaimed, campaign_id, market_type) VALUES (?, 'B', '2026-11-01', ?, ?, ?, ?, ?, 'b2b')",
        (f"(727) 555-{_n[0]:04d}", status, website, reviews, unclaimed, campaign_id))


def _seed_outcomes(conn, camp):
    """80 contacted leads. no_website fires on 40: 20 interested, 4 appointments,
    16 not interested. The other 40 (with a website): 4 interested, 1
    appointment, 35 not interested. Plus 10 'new' leads that must not count."""
    for _ in range(20):
        _lead(conn, camp, "interested")
    for _ in range(4):
        _lead(conn, camp, "appointment")
    for _ in range(16):
        _lead(conn, camp, "not_interested")
    for _ in range(4):
        _lead(conn, camp, "interested", website="https://x.com")
    _lead(conn, camp, "appointment", website="https://x.com")
    for _ in range(35):
        _lead(conn, camp, "not_interested", website="https://x.com")
    for _ in range(10):
        _lead(conn, camp, "new")
    conn.commit()


def _signal(report, name):
    return next(s for s in report["signals"] if s["signal"] == name)


def test_rates_with_and_without_signal_and_sample_sizes(tmp_path):
    conn = db.connect(_fresh(tmp_path))
    camp = _campaign(conn)
    _seed_outcomes(conn, camp)
    (r,) = rep.report(conn, camp)

    assert r["campaign"] == "Camp" and r["offer_slug"] == "seo"
    assert r["contacted"] == 80                       # the 10 'new' leads are not counted
    s = _signal(r, "no_website")
    assert s["with"]["n"] == 40 and s["without"]["n"] == 40
    assert s["with"]["interested"] == pytest.approx(0.5)
    assert s["with"]["appointment"] == pytest.approx(0.1)
    assert s["with"]["not_interested"] == pytest.approx(0.4)
    assert s["without"]["interested"] == pytest.approx(0.1)
    assert s["without"]["not_interested"] == pytest.approx(0.875)
    assert s["lift"] == pytest.approx(0.6 - 0.125)
    assert s["enough_data"]
    # positive 60% with vs 12.5% without: ratio 4.8, capped at double the points
    assert s["suggested"] == 2 * s["points"]
    assert s["note"].startswith("raise")
    conn.close()


def test_under_sampled_signal_is_flagged_not_rated(tmp_path):
    conn = db.connect(_fresh(tmp_path))
    camp = _campaign(conn)
    _seed_outcomes(conn, camp)
    # low_reviews fires on nobody (all have 50 reviews) -> 0 of 30.
    (r,) = rep.report(conn, camp)
    s = _signal(r, "low_reviews")
    assert s["with"]["n"] == 0 and not s["enough_data"]
    assert s["suggested"] is None and "not enough data" in s["note"]
    # A handful of leads with it is still not enough.
    for _ in range(12):
        _lead(conn, camp, "interested", website="https://y.com", reviews=3)
    conn.commit()
    (r,) = rep.report(conn, camp)
    s = _signal(r, "low_reviews")
    assert s["with"]["n"] == 12 and not s["enough_data"]
    assert s["suggested"] is None and "12 of 30" in s["note"]
    conn.close()


def test_report_never_changes_rules_or_scores(tmp_path):
    path = _fresh(tmp_path)
    conn = db.connect(path)
    camp = _campaign(conn)
    _seed_outcomes(conn, camp)
    conn.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    conn = db.connect(path)
    rep.report(conn)
    rep.format_text(rep.report(conn, camp))
    conn.close()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("points,pw,po,expected,note_start", [
    (50, 0.60, 0.30, 100, "raise"),      # ratio 2 -> doubled (cap)
    (50, 0.30, 0.30, 50, "keep"),        # same rate -> keep
    (50, 0.10, 0.50, 10, "lower"),       # ratio .2 -> 10
    (0, 0.60, 0.20, 20, "raise"),        # unscored signal that helps -> 20
    (0, 0.20, 0.20, 0, "keep"),
    (40, 0.30, 0.00, 80, "raise"),       # nothing positive without it -> treated as x2
])
def test_suggest_points_rule(points, pw, po, expected, note_start):
    w = {"n": 40, "positive": pw}
    wo = {"n": 40, "positive": po}
    suggested, note = rep.suggest_points(points, w, wo)
    assert suggested == expected
    assert note.startswith(note_start)


def test_suggest_points_needs_both_groups():
    assert rep.suggest_points(50, {"n": 29, "positive": 1}, {"n": 100, "positive": 0.1})[0] is None
    s, note = rep.suggest_points(50, {"n": 100, "positive": 1}, {"n": 5, "positive": 0.1})
    assert s is None and "without the signal" in note
    s, note = rep.suggest_points(50, {"n": 40, "positive": 0}, {"n": 40, "positive": 0})
    assert s is None and "no positive outcomes" in note


def test_unassigned_pool_uses_default_offer_and_all_campaigns_listed(tmp_path):
    conn = db.connect(_fresh(tmp_path))
    a = _campaign(conn, "A", "seo")
    _campaign(conn, "Empty", "seo")            # no leads: left out
    _lead(conn, a, "interested")
    _lead(conn, None, "not_interested")
    conn.commit()
    reports = rep.report(conn)
    assert [r["campaign"] for r in reports] == ["A", "Unassigned leads"]
    assert reports[1]["offer_slug"] == db.default_offer_slug(conn)
    conn.close()


def test_b2c_lead_never_fires_b2b_signals():
    rules = {"no_website": {"points": 50}, "unclaimed": {"points": 10}}
    b2b = {"website": "", "unclaimed": 1, "market_type": "b2b"}
    b2c = dict(b2b, market_type="b2c")
    assert rep.fired_signals(b2b, rules) == {"no_website", "unclaimed"}
    assert rep.fired_signals(b2c, rules) == set()


def test_cli_prints_the_report_read_only(tmp_path, capsys):
    path = _fresh(tmp_path)
    conn = db.connect(path)
    camp = _campaign(conn)
    _seed_outcomes(conn, camp)
    conn.close()
    assert rep.main(["--db", str(path)]) == 0
    out = capsys.readouterr().out
    assert "analysis only" in out and "== Camp" in out
    assert "no_website" in out and "suggest 100" in out
    assert "not enough data" in out            # the unfired signals
    assert rep.main(["--db", str(path), "--campaign", str(camp)]) == 0


def test_admin_page_renders_and_is_guarded(monkeypatch):
    import app as A
    import users
    monkeypatch.setattr(users, "auth_enabled", lambda conn: False)   # local mode: allowed
    client = A.app.test_client()
    conn = db.connect()
    camp = _campaign(conn, "Page camp")
    _seed_outcomes(conn, camp)
    conn.close()
    r = client.get("/reports/outcomes")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Analysis only" in body and "Page camp" in body and "no_website" in body
    assert "not enough data" in body
    r = client.get(f"/reports/outcomes?campaign_id={camp}")
    assert r.status_code == 200 and "Page camp" in r.get_data(as_text=True)

    # With accounts enabled and nobody signed in as admin, the guard redirects.
    monkeypatch.setattr(users, "auth_enabled", lambda conn: True)
    monkeypatch.setattr(A, "_require_admin", lambda: A.redirect("/"))
    assert client.get("/reports/outcomes").status_code == 302
