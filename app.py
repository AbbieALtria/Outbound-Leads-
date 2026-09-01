"""
Local dashboard for the SEO lead pipeline.

    python app.py        ->  http://localhost:5000

Needs OUTSCRAPER_API_KEY in the environment or in a .env file next to this
script (only for pulling; browsing existing leads works without it).
"""

import csv
import io
import json
import os
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta

from flask import (Flask, g, jsonify, redirect, render_template,
                   request, session, url_for)

import ai_industries
import nppes
import compliance
import contacts
import db
import dialer_import
import dnc
import intent_signals
import geo_data
import leads_intake
import ops_dispositions
import pipeline
import requeue
import scoring
import users
from dialer_import import normalize_phone

app = Flask(__name__)
# Signs the login session cookie. Stable across restarts when set in the env so
# users aren't logged out on every redeploy.
app.secret_key = (os.environ.get("SECRET_KEY")
                  or os.environ.get("ADMIN_PASSWORD")
                  or os.environ.get("APP_PASSWORD")
                  or "local-dev-secret-key")

# Initialize the database at import time so it works under gunicorn (which never
# runs the __main__ block below), not only when started via `python app.py`.
db.init_db()
_seed_conn = db.connect()
users.ensure_admin(_seed_conn)

# Bump when scoring/hook logic changes, so stored score+call_hook are refreshed
# once on the next start instead of showing stale hooks. Bumped to 3 for the
# per-campaign scoring model; 4 for the ICT multi_location signal + rules.
SCORING_VERSION = "5"
if db.get_setting(_seed_conn, "scoring_version") != SCORING_VERSION:
    scoring.rescore_everything(_seed_conn)
    db.set_setting(_seed_conn, "scoring_version", SCORING_VERSION)
    _seed_conn.commit()
_seed_conn.close()

_pull_lock = threading.Lock()

# Paths reachable without a login.
_PUBLIC_ENDPOINTS = {"login", "logout", "static", "api_intake", "api_signal"}


@app.before_request
def require_login():
    """Require a logged-in user for the whole app once auth is enabled (a
    bootstrap password is set or users exist). The vendor intake webhook uses
    its own API key, and the login page must be reachable, so both are exempt.
    Locally, with no auth configured, the app stays open."""
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return None
    conn = get_db()
    if not users.auth_enabled(conn):
        return None
    user_id = session.get("user_id")
    if user_id:
        row = users.get(conn, user_id)
        if row and row["enabled"]:
            g.user = row
            # First login (or after an admin reset): make them set a new password
            # before they can use anything else.
            if row["must_change_password"] and request.endpoint not in (
                    "change_password", "logout", "static"):
                return redirect(url_for("change_password"))
            return None
        session.clear()
    return redirect(url_for("login", next=request.path))


@app.template_filter("fromjson")
def _fromjson(value):
    """Parse a JSON column (e.g. an offer's pain_keywords list) in a template."""
    try:
        out = json.loads(value or "[]")
        return out if isinstance(out, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


@app.context_processor
def inject_user():
    ctx = {"current_user": getattr(g, "user", None), "alert_count": 0,
           "apollo_enabled": contacts.enabled()}
    # Show the unseen-alert badge in the nav for signed-in users.
    if getattr(g, "user", None):
        try:
            ctx["alert_count"] = db.unseen_alert_count(get_db())
        except Exception:
            pass
    return ctx


@app.route("/login", methods=["GET", "POST"])
def login():
    conn = get_db()
    error = None
    if request.method == "POST":
        user = users.authenticate(conn, request.form.get("username", ""),
                                  request.form.get("password", ""))
        if user:
            session.clear()
            session["user_id"] = user["id"]
            dest = request.args.get("next") or url_for("dashboard")
            return redirect(dest if dest.startswith("/") else url_for("dashboard"))
        error = "Wrong username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/help")
def help_page():
    return render_template("help.html", apollo_enabled=contacts.enabled())


@app.route("/activity")
def activity_page():
    """Admin audit view: every pull — who, when, campaign, industry, count."""
    guard = _require_admin()
    if guard:
        return guard
    conn = get_db()
    runs = conn.execute(
        "SELECT r.*, u.username, cp.name AS campaign_name, cl.name AS client_name, "
        "  (SELECT COUNT(*) FROM leads l WHERE l.run_id = r.id) AS lead_count "
        "FROM pull_runs r "
        "LEFT JOIN users u ON u.id = r.user_id "
        "LEFT JOIN campaigns cp ON cp.id = r.campaign_id "
        "LEFT JOIN clients cl ON cl.id = cp.client_id "
        "ORDER BY r.id DESC LIMIT 200"
    ).fetchall()
    diag = score_outcome_report(conn, request.args)
    campaigns = conn.execute(
        "SELECT id, name FROM campaigns ORDER BY status, name").fetchall()
    return render_template("activity.html", runs=runs, diag=diag,
                           campaigns=campaigns, f=request.args)


# Outcome buckets. 'dnc' is a compliance removal, not a sales outcome, so it is
# excluded everywhere below; 'callback' means contacted-but-undecided, so it sits
# with 'called' rather than counting as a positive or a rejection.
_BUCKET_SQL = """CASE
    WHEN status = 'not_interested' THEN 'rejected'
    WHEN status IN ('interested', 'appointment') THEN 'positive'
    WHEN status IN ('called', 'callback') THEN 'contacted'
    WHEN status = 'new' THEN 'new'
    ELSE 'other' END"""


def score_outcome_report(conn, args):
    """Does a higher score correlate with MORE rejection? If the multi-location
    signal is really finding big businesses that already have a vendor (rather
    than an unsolved need), 'not_interested' will skew to higher scores and
    multi-location leads will reject at a higher rate than single-location ones.

    Every figure carries its own sample size — with small n these are directional
    at best, so the template must never show a bare percentage."""
    where, params = ["status != 'dnc'"], []
    if args.get("campaign_id"):
        where.append("campaign_id = ?")
        params.append(args["campaign_id"])
    if args.get("from"):
        where.append("pulled_date >= ?")
        params.append(args["from"])
    if args.get("to"):
        where.append("pulled_date <= ?")
        params.append(args["to"])
    clause = " AND ".join(where)

    by_outcome = conn.execute(
        f"SELECT {_BUCKET_SQL} AS bucket, COUNT(*) AS n, "
        f"       ROUND(AVG(score), 1) AS avg_score, MIN(score) AS lo, MAX(score) AS hi "
        f"FROM leads WHERE {clause} GROUP BY bucket", params).fetchall()
    order = {"rejected": 0, "positive": 1, "contacted": 2, "new": 3, "other": 4}
    outcomes = sorted((dict(r) for r in by_outcome),
                      key=lambda d: order.get(d["bucket"], 9))

    # Rejection rate is only meaningful among leads actually CONTACTED — a 'new'
    # lead hasn't had the chance to reject.
    by_loc = conn.execute(
        f"SELECT CASE WHEN COALESCE(location_count, 1) >= 2 THEN 'multi' ELSE 'single' END AS loc, "
        f"       COUNT(*) AS contacted, "
        f"       SUM(CASE WHEN status = 'not_interested' THEN 1 ELSE 0 END) AS rejected, "
        f"       ROUND(AVG(score), 1) AS avg_score "
        f"FROM leads WHERE {clause} AND status NOT IN ('new') GROUP BY loc", params).fetchall()
    loc_rows = []
    for r in by_loc:
        d = dict(r)
        d["rate"] = round(100.0 * d["rejected"] / d["contacted"], 1) if d["contacted"] else None
        loc_rows.append(d)
    loc_rows.sort(key=lambda d: 0 if d["loc"] == "multi" else 1)

    reasons = [dict(r) for r in conn.execute(
        f"SELECT COALESCE(NULLIF(not_interested_reason, ''), '(not recorded)') AS reason, "
        f"       COUNT(*) AS n, ROUND(AVG(score), 1) AS avg_score "
        f"FROM leads WHERE {clause} AND status = 'not_interested' "
        f"GROUP BY reason ORDER BY n DESC", params)]
    rejected_total = sum(r["n"] for r in reasons)
    for r in reasons:
        r["pct"] = round(100.0 * r["n"] / rejected_total, 1) if rejected_total else 0

    contacted_total = sum(d["contacted"] for d in loc_rows)
    return {"reasons": reasons, "rejected_total": rejected_total,
            "outcomes": outcomes, "by_location": loc_rows,
            "contacted_total": contacted_total,
            "total": sum(d["n"] for d in outcomes)}


@app.route("/change-password", methods=["GET", "POST"])
def change_password():
    """Any signed-in user sets their OWN password. Forced on first login / after an
    admin reset (the must_change_password flag), and reachable any time from the nav."""
    user = getattr(g, "user", None)
    if not user:
        return redirect(url_for("login"))
    forced = bool(user["must_change_password"])
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        pw2 = request.form.get("password2", "")
        if len(pw) < 6:
            error = "Password must be at least 6 characters."
        elif pw != pw2:
            error = "The two passwords don't match."
        else:
            users.change_own_password(get_db(), user["id"], pw)
            return redirect(url_for("dashboard"))
    return render_template("change_password.html", error=error, forced=forced)


def _require_admin():
    """Return None if the request is allowed to perform admin actions, else a
    redirect. Allowed when the current user is an admin, or when auth is disabled
    entirely (local single-user mode has no accounts to be admin of)."""
    user = getattr(g, "user", None)
    if user and user["role"] == "admin":
        return None
    if not users.auth_enabled(get_db()):
        return None
    return redirect(url_for("dashboard"))


@app.route("/users")
def users_page():
    guard = _require_admin()
    if guard:
        return guard
    conn = get_db()
    rows = []
    for u in users.list_users(conn):
        d = dict(u)
        d["used_total"], d["used_today"] = users.usage(conn, u["id"])
        d["allowed_ids"] = users.allowed_campaign_ids(u)
        rows.append(d)
    campaigns = conn.execute(
        "SELECT c.id, c.name, cl.name AS client_name FROM campaigns c "
        "LEFT JOIN clients cl ON cl.id = c.client_id ORDER BY c.status, c.name"
    ).fetchall()
    return render_template("users.html", users=rows, roles=users.ROLES,
                           campaigns=campaigns, error=request.args.get("error"))


@app.route("/users/create", methods=["POST"])
def users_create():
    guard = _require_admin()
    if guard:
        return guard
    conn = get_db()
    creator = g.user["username"] if getattr(g, "user", None) else "admin"
    role = request.form.get("role", "agent")
    ok, result = users.create_user(
        conn, request.form.get("username", ""), request.form.get("password", ""),
        role, created_by=creator,
    )
    if not ok:
        return redirect(url_for("users_page", error=result))
    # Set caps + campaign access right at creation (agents only; admins are unlimited).
    if role != "admin":
        users.set_limits(conn, result,
                         request.form.get("lead_limit_total", "0"),
                         request.form.get("lead_limit_daily", "0"),
                         request.form.getlist("allowed_campaigns"))
    return redirect(url_for("users_page"))


@app.route("/users/<int:user_id>/password", methods=["POST"])
def users_password(user_id):
    guard = _require_admin()
    if guard:
        return guard
    conn = get_db()
    if not users.set_password(conn, user_id, request.form.get("password", "")):
        return redirect(url_for("users_page", error="Password must be at least 6 characters."))
    return redirect(url_for("users_page"))


@app.route("/users/<int:user_id>/toggle", methods=["POST"])
def users_toggle(user_id):
    guard = _require_admin()
    if guard:
        return guard
    conn = get_db()
    target = users.get(conn, user_id)
    # Never disable the last active admin (would lock everyone out of user mgmt).
    if target and target["role"] == "admin" and target["enabled"] and users.admin_count(conn) <= 1:
        return redirect(url_for("users_page", error="Can't disable the only admin."))
    users.toggle_enabled(conn, user_id)
    return redirect(url_for("users_page"))


@app.route("/users/<int:user_id>/delete", methods=["POST"])
def users_delete(user_id):
    guard = _require_admin()
    if guard:
        return guard
    conn = get_db()
    target = users.get(conn, user_id)
    if target and target["role"] == "admin" and users.admin_count(conn) <= 1:
        return redirect(url_for("users_page", error="Can't delete the only admin."))
    if target and target["id"] == g.user["id"]:
        return redirect(url_for("users_page", error="You can't delete your own account."))
    users.delete_user(conn, user_id)
    return redirect(url_for("users_page"))


@app.route("/users/<int:user_id>/limits", methods=["POST"])
def users_limits(user_id):
    guard = _require_admin()
    if guard:
        return guard
    conn = get_db()
    users.set_limits(
        conn, user_id,
        request.form.get("lead_limit_total", "0"),
        request.form.get("lead_limit_daily", "0"),
        request.form.getlist("allowed_campaigns"),
    )
    return redirect(url_for("users_page"))


@app.route("/users/<int:user_id>/reset_usage", methods=["POST"])
def users_reset_usage(user_id):
    guard = _require_admin()
    if guard:
        return guard
    conn = get_db()
    users.reset_usage(conn, user_id)
    return redirect(url_for("users_page"))


# ---------------------------------------------------------------- clients

@app.route("/clients")
def clients_page():
    conn = get_db()
    clients = conn.execute(
        "SELECT c.*, "
        "  (SELECT COUNT(*) FROM campaigns cp WHERE cp.client_id = c.id) AS campaign_count "
        "FROM clients c ORDER BY c.enabled DESC, c.name"
    ).fetchall()
    return render_template("clients.html", clients=clients,
                           error=request.args.get("error"))


@app.route("/clients/create", methods=["POST"])
def clients_create():
    guard = _require_admin()
    if guard:
        return guard
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for("clients_page", error="Client name is required."))
    conn = get_db()
    conn.execute(
        "INSERT INTO clients (name, contact_name, email, phone, website, address, notes, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, request.form.get("contact_name", "").strip(),
         request.form.get("email", "").strip(), request.form.get("phone", "").strip(),
         request.form.get("website", "").strip(), request.form.get("address", "").strip(),
         request.form.get("notes", "").strip(), db.now_iso()),
    )
    conn.commit()
    return redirect(url_for("clients_page"))


@app.route("/clients/<int:client_id>/edit", methods=["POST"])
def clients_edit(client_id):
    guard = _require_admin()
    if guard:
        return guard
    conn = get_db()
    for field in ("name", "contact_name", "email", "phone", "website", "address", "notes"):
        if field in request.form:
            val = request.form.get(field, "").strip()
            if field == "name" and not val:
                continue
            conn.execute(f"UPDATE clients SET {field} = ? WHERE id = ?", (val, client_id))
    conn.commit()
    return redirect(url_for("clients_page"))


@app.route("/clients/<int:client_id>/toggle", methods=["POST"])
def clients_toggle(client_id):
    guard = _require_admin()
    if guard:
        return guard
    conn = get_db()
    conn.execute("UPDATE clients SET enabled = 1 - enabled WHERE id = ?", (client_id,))
    conn.commit()
    return redirect(url_for("clients_page"))


# ---------------------------------------------------------------- campaigns

CAMPAIGN_STATUSES = ("active", "paused", "archived")


@app.route("/campaigns")
def campaigns_page():
    conn = get_db()
    campaigns = conn.execute(
        "SELECT cp.*, cl.name AS client_name, "
        "  (SELECT COUNT(*) FROM leads l WHERE l.campaign_id = cp.id) AS lead_count "
        "FROM campaigns cp LEFT JOIN clients cl ON cl.id = cp.client_id "
        "ORDER BY cp.status, cp.name"
    ).fetchall()
    clients = conn.execute(
        "SELECT * FROM clients WHERE enabled = 1 ORDER BY name").fetchall()
    offers = conn.execute(
        "SELECT * FROM offers WHERE enabled = 1 ORDER BY audience, name").fetchall()
    industries = conn.execute(
        "SELECT * FROM industries WHERE enabled = 1 ORDER BY label").fetchall()
    return render_template(
        "campaigns.html", campaigns=campaigns, clients=clients, offers=offers,
        industries=industries, statuses=CAMPAIGN_STATUSES,
        countries=COUNTRIES, states_by_country=STATES_BY_COUNTRY,
        error=request.args.get("error"))


@app.route("/campaigns/create", methods=["POST"])
def campaigns_create():
    guard = _require_admin()
    if guard:
        return guard
    conn = get_db()
    name = request.form.get("name", "").strip()
    offer_slug = request.form.get("offer_slug", "").strip()
    offer = db.get_offer(conn, offer_slug)
    if not name or not offer:
        return redirect(url_for("campaigns_page", error="Name and a valid offer are required."))
    try:
        client_id = int(request.form.get("client_id") or 0) or None
    except ValueError:
        client_id = None
    conn.execute(
        "INSERT INTO campaigns (name, client_id, offer_slug, audience, industry_slug, "
        "country, state, city, vici_campaign_id, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
        (name, client_id, offer_slug, offer["audience"],
         request.form.get("industry_slug", "").strip(),
         request.form.get("country", "").strip(), request.form.get("state", "").strip(),
         request.form.get("city", "").strip(),
         request.form.get("vici_campaign_id", "").strip(), db.now_iso()),
    )
    conn.commit()
    return redirect(url_for("campaigns_page"))


@app.route("/campaigns/<int:campaign_id>/suggest_industries", methods=["POST"])
def campaign_suggest_industries(campaign_id):
    """Ask Claude for related industries for this campaign. Suggestions are shown
    for REVIEW only — nothing is added until a human approves below."""
    guard = _require_admin()
    if guard:
        return guard
    conn = get_db()
    camp = conn.execute(
        "SELECT cp.*, cl.name AS client_name FROM campaigns cp "
        "LEFT JOIN clients cl ON cl.id = cp.client_id WHERE cp.id = ?", (campaign_id,)
    ).fetchone()
    if not camp:
        return redirect(url_for("campaigns_page", error="Campaign not found."))
    offer = db.offer_for_campaign(conn, camp)
    existing = [r["label"] for r in conn.execute("SELECT label FROM industries")]
    suggestions, error = [], None
    if not ai_industries.enabled():
        error = ("AI suggestions are off — set ANTHROPIC_API_KEY in Railway → "
                 "Variables to enable them.")
    else:
        try:
            suggestions = ai_industries.suggest(
                camp["name"], offer["name"] if offer else "",
                existing, country=camp["country"] or "")
        except Exception as e:
            error = f"Couldn't get suggestions: {e}"
    return render_template("campaign_suggestions.html", campaign=camp,
                           suggestions=suggestions, error=error,
                           next=request.form.get("next", ""))


@app.route("/campaigns/<int:campaign_id>/approve_industries", methods=["POST"])
def campaign_approve_industries(campaign_id):
    """Add the checked (and possibly edited) suggestions via the SAME path as a
    manually added industry."""
    guard = _require_admin()
    if guard:
        return guard
    conn = get_db()
    added, first_slug = 0, ""
    for idx in request.form.getlist("approve"):
        label = request.form.get(f"label_{idx}", "")
        query = request.form.get(f"query_{idx}", "")
        chains = [c.strip() for c in request.form.get(f"chains_{idx}", "").split(",")
                  if c.strip()]
        slug = create_industry(conn, label, query, chains)
        if slug:
            added += 1
            first_slug = first_slug or slug
    # Approved from the dashboard: preselect the first new industry for the next
    # pull and go back there, same as the manual '+ add industry' quick-add.
    if request.form.get("next") == "dashboard":
        if first_slug:
            db.set_setting(conn, "default_industry", first_slug)
        conn.commit()
        return redirect(url_for("dashboard", campaign_id=campaign_id, added=added))
    conn.commit()
    return redirect(url_for("campaign_detail", campaign_id=campaign_id, added=added))


@app.route("/campaigns/<int:campaign_id>/edit", methods=["POST"])
def campaigns_edit(campaign_id):
    guard = _require_admin()
    if guard:
        return guard
    conn = get_db()
    # Editable text/select fields (offer change also refreshes the cached audience).
    for field in ("name", "industry_slug", "country", "state", "city", "vici_campaign_id"):
        if field in request.form:
            conn.execute(f"UPDATE campaigns SET {field} = ? WHERE id = ?",
                         (request.form.get(field, "").strip(), campaign_id))
    if "client_id" in request.form:
        try:
            cid = int(request.form.get("client_id") or 0) or None
        except ValueError:
            cid = None
        conn.execute("UPDATE campaigns SET client_id = ? WHERE id = ?", (cid, campaign_id))
    offer_slug = request.form.get("offer_slug", "").strip()
    if offer_slug:
        offer = db.get_offer(conn, offer_slug)
        if offer:
            conn.execute("UPDATE campaigns SET offer_slug = ?, audience = ? WHERE id = ?",
                         (offer_slug, offer["audience"], campaign_id))
    status = request.form.get("status", "").strip()
    if status in CAMPAIGN_STATUSES:
        conn.execute("UPDATE campaigns SET status = ? WHERE id = ?", (status, campaign_id))
    conn.commit()
    # Re-rank this campaign's leads if its offer may have changed.
    camp = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if camp:
        scoring.rescore_campaign(conn, camp)
    return redirect(url_for("campaign_detail", campaign_id=campaign_id))


@app.route("/campaigns/<int:campaign_id>")
def campaign_detail(campaign_id):
    conn = get_db()
    campaign = conn.execute(
        "SELECT cp.*, cl.name AS client_name FROM campaigns cp "
        "LEFT JOIN clients cl ON cl.id = cp.client_id WHERE cp.id = ?", (campaign_id,)
    ).fetchone()
    if not campaign:
        return redirect(url_for("campaigns_page", error="Campaign not found."))
    offer = db.offer_for_campaign(conn, campaign)
    # Lead lifecycle for this campaign: counts by status.
    status_counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM leads WHERE campaign_id = ? GROUP BY status",
        (campaign_id,))}
    # Pull runs (generation cycles) for this campaign.
    runs = conn.execute(
        "SELECT * FROM pull_runs WHERE campaign_id = ? ORDER BY id DESC LIMIT 50",
        (campaign_id,)).fetchall()
    # Redial cycle: not-reached leads currently queued, via the VICIdial bridge.
    requeue_rows = conn.execute(
        "SELECT r.*, l.business_name, l.phone FROM requeue_leads r "
        "JOIN leads l ON l.id = r.lead_id WHERE l.campaign_id = ? "
        "ORDER BY r.updated_at DESC LIMIT 100", (campaign_id,)).fetchall()
    clients = conn.execute("SELECT * FROM clients WHERE enabled = 1 ORDER BY name").fetchall()
    offers = conn.execute("SELECT * FROM offers WHERE enabled = 1 ORDER BY audience, name").fetchall()
    industries = conn.execute("SELECT * FROM industries WHERE enabled = 1 ORDER BY label").fetchall()
    return render_template(
        "campaign_detail.html", campaign=campaign, offer=offer,
        status_counts=status_counts, statuses=db.LEAD_STATUSES, runs=runs,
        requeue_rows=requeue_rows, clients=clients, offers=offers,
        industries=industries, campaign_statuses=CAMPAIGN_STATUSES)


def get_db():
    if "db" not in g:
        g.db = db.connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def lead_filters(args):
    """Build WHERE clause + params from query-string filters."""
    where, params = [], []
    if args.get("run_id"):
        where.append("run_id = ?")
        params.append(args["run_id"])
    if args.get("user_id"):
        where.append("run_id IN (SELECT id FROM pull_runs WHERE user_id = ?)")
        params.append(args["user_id"])
    if args.get("date"):
        where.append("pulled_date = ?")
        params.append(args["date"])
    if args.get("status"):
        where.append("status = ?")
        params.append(args["status"])
    if args.get("country"):
        where.append("country = ?")
        params.append(args["country"])
    if args.get("state"):
        where.append("state = ?")
        params.append(args["state"])
    if args.get("city"):
        where.append("city = ?")
        params.append(args["city"])
    if args.get("industry"):
        where.append("industry = ?")
        params.append(args["industry"])
    if args.get("market_type"):
        where.append("market_type = ?")
        params.append(args["market_type"])
    if args.get("requeue") == "active":
        # Segment the redial pool by campaign source and batch date (both live on
        # requeue_leads) so the admin can download one VICIdial file per campaign/
        # date/industry. industry is filtered on the leads table above.
        sub = "SELECT lead_id FROM requeue_leads WHERE state = 'active'"
        subp = []
        if args.get("campaign"):
            sub += " AND campaign = ?"
            subp.append(args["campaign"])
        if args.get("rq_date"):
            sub += " AND batch_date = ?"
            subp.append(args["rq_date"])
        where.append(f"id IN ({sub})")
        params.extend(subp)
    # Numbers registered from VICIdial dispositions (source-agnostic requeue) are
    # dialable but aren't lead-gen leads — keep them out of the lead-gen lists
    # (Dashboard/History) unless we're building the requeue export or explicitly
    # filtering by source.
    if args.get("requeue") != "active" and not args.get("lead_source"):
        where.append("lead_source != 'vicidial'")
    if args.get("q"):
        where.append("(business_name LIKE ? OR phone LIKE ?)")
        like = f"%{args['q']}%"
        params.extend([like, like])
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    return clause, params


def fetch_leads(conn, args):
    clause, params = lead_filters(args)
    return conn.execute(
        f"SELECT * FROM leads {clause} ORDER BY pulled_date DESC, score DESC, id",
        params,
    ).fetchall()


def _known_cities_by_state(conn):
    """Cities already pulled, grouped by state, to merge into the seeded city
    dropdown so previously-used cities also show up."""
    out = {}
    for r in conn.execute(
        "SELECT DISTINCT state, city FROM leads WHERE city != '' AND state != ''"
    ):
        out.setdefault(r["state"], []).append(r["city"])
    return out


# ---------------------------------------------------------------- pages

@app.route("/")
def dashboard():
    """Shows only the current pull (latest run's leads). Everything else,
    including older pulls, lives in History."""
    conn = get_db()
    # The batch to show: an explicit ?run_id=, else the most recent run with leads.
    run_id = request.args.get("run_id")
    if not run_id:
        row = conn.execute("SELECT MAX(run_id) AS r FROM leads").fetchone()
        run_id = row["r"]

    batch = None
    batch_user = ""
    if run_id:
        batch = conn.execute("SELECT * FROM pull_runs WHERE id = ?", (run_id,)).fetchone()
        if batch and batch["user_id"]:
            row = conn.execute("SELECT username FROM users WHERE id = ?",
                               (batch["user_id"],)).fetchone()
            batch_user = row["username"] if row else ""

    # With no run_id at all (fresh DB, nothing pulled) show nothing on the dashboard.
    if run_id:
        args = {"run_id": run_id}
        if request.args.get("status"):
            args["status"] = request.args["status"]
        leads = fetch_leads(conn, args)
    else:
        leads = []

    industries = conn.execute(
        "SELECT * FROM industries WHERE enabled = 1 ORDER BY label"
    ).fetchall()
    counts = {s: 0 for s in db.LEAD_STATUSES}
    for lead in leads:
        counts[lead["status"]] = counts.get(lead["status"], 0) + 1
    total_leads = conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"]
    # Campaigns (client engagements) drive the dashboard now — industry + geo are
    # attributes of the chosen campaign, so they can't be mismatched.
    campaigns = conn.execute(
        "SELECT cp.*, cl.name AS client_name FROM campaigns cp "
        "LEFT JOIN clients cl ON cl.id = cp.client_id "
        "WHERE cp.status != 'archived' ORDER BY cp.status, cp.name"
    ).fetchall()
    # Per-user limits: a restricted (non-admin) user sees only their campaigns and
    # their remaining quota; admins are unrestricted.
    user = getattr(g, "user", None)
    quota = None
    restrict_campaigns = False
    if user and user["role"] != "admin":
        allowed = users.allowed_campaign_ids(user)
        if allowed:
            restrict_campaigns = True
            campaigns = [c for c in campaigns if c["id"] in allowed]
        ut, ud = users.usage(conn, user["id"])
        quota = {"total_used": ut, "total_limit": user["lead_limit_total"],
                 "today_used": ud, "daily_limit": user["lead_limit_daily"]}
    sel_id = request.args.get("campaign_id") or (batch["campaign_id"] if batch else None)
    # A restricted user has no ad-hoc option, so default to their first campaign —
    # otherwise the dropdown shows a campaign that isn't actually "selected" and the
    # pull is rejected as having no campaign.
    if not sel_id and restrict_campaigns and campaigns:
        sel_id = campaigns[0]["id"]
    selected_campaign = None
    if sel_id:
        selected_campaign = conn.execute(
            "SELECT cp.*, cl.name AS client_name FROM campaigns cp "
            "LEFT JOIN clients cl ON cl.id = cp.client_id WHERE cp.id = ?", (sel_id,)
        ).fetchone()
    # The offer drives goal/audience-dependent UI (appointment button, B2C labels);
    # it's the selected campaign's offer, or the default offer when none is chosen.
    active_campaign = db.offer_for_campaign(conn, selected_campaign)
    # Computed once — this runs COUNT queries, so calling it per template
    # argument doubled the work on every dashboard load.
    _discard_state = pull_run_discardable(conn, run_id)
    return render_template(
        "dashboard.html", leads=leads, batch=batch, batch_user=batch_user, run_id=run_id,
        out_of_hours=(out_of_hours_count(conn, {"run_id": run_id}) if run_id else 0),
        industries=industries, statuses=db.LEAD_STATUSES, counts=counts,
        current_status=request.args.get("status", ""),
        total_leads=total_leads,
        not_interested_reasons=db.NOT_INTERESTED_REASONS,
        campaigns=campaigns, active_campaign=active_campaign,
        selected_campaign=selected_campaign,
        quota=quota, restrict_campaigns=restrict_campaigns,
        can_discard=_discard_state[0], discard_reason=_discard_state[1],
        default_industry=db.get_setting(conn, "default_industry", "hvac"),
        default_target=db.get_setting(conn, "target_leads_per_day", "100"),
        countries=COUNTRIES, states_by_country=STATES_BY_COUNTRY,
        cities_by_state=geo_data.CITIES_BY_STATE,
        known_by_state=_known_cities_by_state(conn),
        last_city=db.get_setting(conn, "last_city", ""),
        last_state=db.get_setting(conn, "last_state", ""),
        last_country=db.get_setting(conn, "last_country", "United States"),
        api_key_set=bool(pipeline.get_api_key()),
        apollo_enabled=contacts.enabled(),
        nppes_industries=nppes.supported_industries(),
        # Drive the enrichment confirmation modal's cost estimate.
        enrich_reveal_email=db.get_setting(conn, "enrich_reveal_email", "1") == "1",
        enrich_reveal_phone=db.get_setting(conn, "enrich_reveal_phone", "0") == "1",
    )


@app.route("/history")
def history():
    conn = get_db()
    leads = fetch_leads(conn, request.args)
    distinct = lambda col: [r[col] for r in conn.execute(
        f"SELECT DISTINCT {col} FROM leads WHERE {col} != '' ORDER BY {col}")]
    industries = conn.execute("SELECT * FROM industries ORDER BY label").fetchall()
    # Admins can filter by the user who generated the leads.
    is_admin = getattr(g, "user", None) and g.user["role"] == "admin"
    user_list = users.list_users(conn) if is_admin else []
    return render_template(
        "history.html", leads=leads,
        countries=distinct("country"), states=distinct("state"), cities=distinct("city"),
        industries=industries, statuses=db.LEAD_STATUSES, f=request.args,
        not_interested_reasons=db.NOT_INTERESTED_REASONS,
        user_list=user_list,
        out_of_hours=out_of_hours_count(conn, request.args),
    )


@app.route("/settings")
def settings():
    conn = get_db()
    cities = conn.execute("SELECT * FROM cities ORDER BY state, name").fetchall()
    industries = conn.execute("SELECT * FROM industries ORDER BY label").fetchall()
    chains = {}
    for row in conn.execute("SELECT * FROM chains ORDER BY name"):
        chains.setdefault(row["industry_id"], []).append(row)
    offers = conn.execute("SELECT * FROM offers ORDER BY is_preset DESC, name").fetchall()
    return render_template(
        "settings.html", cities=cities, industries=industries, chains=chains,
        offers=offers, market_types=db.MARKET_TYPES,
        active_offer=db.get_setting(conn, "active_campaign", db.DEFAULT_OFFER),
        target=db.get_setting(conn, "target_leads_per_day", "100"),
        default_industry=db.get_setting(conn, "default_industry", "hvac"),
        contact_enrichment=db.get_setting(conn, "contact_enrichment", "1") == "1",
        phone_validation=db.get_setting(conn, "phone_validation", "0") == "1",
        review_signals=db.get_setting(conn, "review_signals", "0") == "1",
        buffer_multiplier=db.get_setting(conn, "buffer_multiplier", "1.4"),
        drop_voip_export=db.get_setting(conn, "drop_voip_export", "0") == "1",
        apollo_enabled=contacts.enabled(),
        enrich_reveal_email=db.get_setting(conn, "enrich_reveal_email", "1") == "1",
        enrich_reveal_phone=db.get_setting(conn, "enrich_reveal_phone", "0") == "1",
        api_key_set=bool(pipeline.get_api_key()),
        storage=storage_status(conn),
    )


def storage_status(conn):
    """Where the database lives and whether it survives a redeploy.

    Without DATA_DIR pointing at a mounted volume the file sits in the container
    filesystem, so every deploy silently starts from an empty database. That
    looks identical to "someone deleted the leads", which is exactly the
    ambiguity this panel exists to remove.
    """
    persistent = bool(os.environ.get("DATA_DIR"))
    counts = {}
    for table in ("leads", "pull_runs", "clients", "campaigns", "users"):
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        except sqlite3.Error:
            counts[table] = "?"
    return {"path": str(db.DB_FILE), "persistent": persistent, "counts": counts,
            "size_kb": round(db.DB_FILE.stat().st_size / 1024) if db.DB_FILE.exists() else 0}


def export_filename(conn, args, leads, prefix=""):
    """Descriptive export name: {campaign|industry}_{city|multi}_{YYYYMMDD}_{HHMM}.csv

    Shared by BOTH export routes. The campaign comes from an explicit campaign_id
    or the exported run's campaign; with no campaign (a quick one-off pull) the
    industry is used instead. The city is read off the exported leads themselves —
    a single city by name, several as "multi" — so the name reflects what's
    actually in the file. Timestamped at export time, not pull time."""
    def slug(text):
        return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")

    name, row = "", None
    if args.get("campaign_id"):
        row = conn.execute("SELECT name FROM campaigns WHERE id = ?",
                           (args.get("campaign_id"),)).fetchone()
    elif args.get("run_id"):
        row = conn.execute(
            "SELECT cp.name AS name FROM pull_runs r "
            "LEFT JOIN campaigns cp ON cp.id = r.campaign_id WHERE r.id = ?",
            (args.get("run_id"),)).fetchone()
    if row and row["name"]:
        name = row["name"]
    if not name:      # no campaign -> fall back to the industry
        inds = {(l["industry"] or "").strip() for l in leads if (l["industry"] or "").strip()}
        name = inds.pop() if len(inds) == 1 else (args.get("industry") or "leads")

    cities = {(l["city"] or "").strip() for l in leads if (l["city"] or "").strip()}
    city = cities.pop() if len(cities) == 1 else ("multi" if cities else "")

    parts = [slug(p) for p in (prefix, name, city) if p]
    parts.append(datetime.now().strftime("%Y%m%d_%H%M"))
    return "_".join(p for p in parts if p) + ".csv"


@app.route("/export.csv")
def export_csv():
    conn = get_db()
    leads = fetch_leads(conn, request.args)
    # DNC numbers never go on a dial sheet (unless explicitly exporting the dnc list)
    if request.args.get("status") != "dnc":
        leads = [lead for lead in leads if lead["status"] != "dnc"]
    fields = ["business_name", "phone", "address", "city", "state",
              "website", "category", "score", "call_hook", "status",
              "not_interested_reason", "notes", "pulled_date"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows([dict(lead) for lead in leads])
    name = export_filename(conn, request.args, leads)
    return app.response_class(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={name}"},
    )


STATE_ABBREV = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY", "District Of Columbia": "DC",
}


def abbrev_state(state):
    state = (state or "").strip()
    if len(state) == 2:
        return state.upper()
    return STATE_ABBREV.get(state.title(), state)


# Geo dropdown data. State list cascades from the chosen country; countries
# without a predefined list fall back to the "City" box only.
STATES_BY_COUNTRY = {
    "United States": sorted(STATE_ABBREV.keys()),
    "Canada": ["Alberta", "British Columbia", "Manitoba", "New Brunswick",
               "Newfoundland and Labrador", "Northwest Territories", "Nova Scotia",
               "Nunavut", "Ontario", "Prince Edward Island", "Quebec",
               "Saskatchewan", "Yukon"],
    "United Kingdom": ["England", "Scotland", "Wales", "Northern Ireland"],
    "Australia": ["New South Wales", "Victoria", "Queensland", "Western Australia",
                  "South Australia", "Tasmania", "Australian Capital Territory",
                  "Northern Territory"],
}
COUNTRIES = ["United States", "Canada", "United Kingdom", "Australia", "Ireland",
             "New Zealand", "India", "Philippines", "Mexico", "Germany", "France",
             "Spain", "Italy", "Netherlands", "Brazil", "South Africa",
             "United Arab Emirates", "Singapore"]


def vici_phone(phone):
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return phone


TOLL_FREE = {"800", "888", "877", "866", "855", "844", "833", "822"}


def _is_toll_free(phone):
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return len(digits) == 10 and digits[:3] in TOLL_FREE


def dialable_leads(conn, args):
    """Leads fit to hand to VICIdial: not DNC, phone not flagged invalid, business
    not closed, not requeue-exhausted/excluded, and not within a YPNI cooldown —
    plus VOIP/toll-free dropped when the setting is on. One place so every dial
    export stays consistent."""
    drop_voip = db.get_setting(conn, "drop_voip_export", "0") == "1"
    # Calling-hours: OFF by default (a batch is dialed over time, so VICIdial's
    # per-timezone call-time settings are the real gate). enforce_hours=1 drops
    # leads outside their legal window right now — for dialing the file immediately.
    enforce_hours = args.get("enforce_hours") == "1"
    blocked = requeue.blocked_lead_ids(conn)
    suppressed = requeue.suppressed_phones(conn)
    out = []
    for lead in fetch_leads(conn, args):
        if lead["status"] == "dnc" or not lead["phone_valid"]:
            continue
        if "CLOSED" in (lead["business_status"] or "").upper():
            continue  # permanently-closed skipped at pull; this drops temp-closed too
        if lead["id"] in blocked:
            continue  # requeue attempts exhausted, or manually excluded
        if normalize_phone(lead["phone"]) in suppressed:
            continue  # YPNI cooldown
        if drop_voip and ("voip" in (lead["phone_type"] or "").lower()
                          or _is_toll_free(lead["phone"])):
            continue
        if enforce_hours and not compliance.within_calling_hours(
                lead["country"], lead["state"], lead["city"])[0]:
            continue  # outside the recipient's legal calling window (CRTC/TSR)
        out.append(lead)
    return out


def out_of_hours_count(conn, args):
    """How many export-eligible leads are outside their legal calling window right
    now — for the export-time compliance warning (does not exclude anything)."""
    base = {k: v for k, v in dict(args).items() if k != "enforce_hours"}
    return sum(1 for l in dialable_leads(conn, base)
               if not compliance.within_calling_hours(l["country"], l["state"], l["city"])[0])


def _street_of(lead):
    """Street-only line: the stored street, else the part of the full address
    before the first comma (legacy leads that predate the street column)."""
    if lead["street_address"]:
        return lead["street_address"]
    addr = lead["address"] or ""
    return addr.split(",")[0].strip() if addr else ""


@app.route("/export/vicidial.csv")
def export_vicidial():
    """Dial-ready export matching the client's VICIdial upload (the red-tagged
    columns of their raw file):
    Name, Full_Address, Street_Address, City, State, Zip, Website, Phone, Email, Category, URL
    DNC, invalid-phone, and closed-business leads are never included."""
    conn = get_db()
    leads = dialable_leads(conn, request.args)

    # For a requeue export, surface VICIdial's callback_time (display only) so the
    # redial list can be sorted by it in the file too. Only when callbacks exist.
    cb_by_phone = {}
    if request.args.get("requeue") == "active" and ops_dispositions.enabled():
        try:
            for c in ops_dispositions.fetch_callbacks():
                p = normalize_phone(c.get("phone"))
                if p and c.get("callback_time"):
                    cb_by_phone[p] = c["callback_time"]
        except Exception:
            cb_by_phone = {}
    include_cbt = any(normalize_phone(l["phone"]) in cb_by_phone for l in leads)

    fields = ["Name", "Full_Address", "Street_Address", "City", "State", "Zip",
              "Website", "Phone", "Email", "Category", "URL"]
    if include_cbt:
        fields = fields + ["Callback_Time"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for lead in leads:
        row = {
            "Name": lead["business_name"],
            "Full_Address": lead["address"],
            "Street_Address": _street_of(lead),
            "City": lead["city"],
            "State": abbrev_state(lead["state"]),
            "Zip": lead["postcode"],
            "Website": lead["website"],
            "Phone": vici_phone(lead["phone"]),
            "Email": lead["email"],
            "Category": lead["category"],
            "URL": lead["maps_url"],
        }
        if include_cbt:
            cbt = cb_by_phone.get(normalize_phone(lead["phone"]))
            row["Callback_Time"] = str(cbt)[:16] if cbt else ""
        writer.writerow(row)
    prefix = "requeue" if request.args.get("requeue") == "active" else "vicidial"
    name = export_filename(conn, request.args, leads, prefix=prefix)
    return app.response_class(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={name}"},
    )


def pull_run_discardable(conn, run_id):
    """(can_discard, reason). A pull can only be discarded while nobody has acted
    on its leads — any non-'new' status or a requeue entry means someone has
    already worked them, so deleting would destroy real work."""
    if not run_id:
        return False, "No pull selected."
    n = conn.execute("SELECT COUNT(*) AS n FROM leads WHERE run_id = ?", (run_id,)).fetchone()["n"]
    if not n:
        return False, "This pull has no leads to discard."
    acted = conn.execute(
        "SELECT COUNT(*) AS n FROM leads WHERE run_id = ? AND status != 'new'", (run_id,)
    ).fetchone()["n"]
    if acted:
        return False, (f"{acted} lead(s) in this pull already have a call status — "
                       "discarding would delete work that's already been done.")
    queued = conn.execute(
        "SELECT COUNT(*) AS n FROM requeue_leads r JOIN leads l ON l.id = r.lead_id "
        "WHERE l.run_id = ?", (run_id,)).fetchone()["n"]
    if queued:
        return False, (f"{queued} lead(s) from this pull are in the redial queue — "
                       "discarding would remove leads already in the dialing cycle.")
    return True, ""


@app.route("/pull_runs/<int:run_id>/discard", methods=["POST"])
def pull_run_discard(run_id):
    """Permanently delete this pull's leads. The pull_runs row is KEPT and marked
    'discarded' so Activity still shows the pull happened (with 0 surviving leads)."""
    conn = get_db()
    ok, reason = pull_run_discardable(conn, run_id)
    if not ok:
        return redirect(url_for("dashboard", run_id=run_id, error=reason))
    conn.execute("DELETE FROM leads WHERE run_id = ?", (run_id,))
    conn.execute("UPDATE pull_runs SET status = 'discarded', message = ? WHERE id = ?",
                 ("Discarded by " + (g.user["username"] if getattr(g, "user", None) else "user"),
                  run_id))
    conn.commit()
    return redirect(url_for("dashboard", discarded=1))


@app.route("/pull_runs/<int:run_id>/rate", methods=["POST"])
def pull_run_rate(run_id):
    """Optional batch-level quality review (pre-dial list quality — separate from
    per-lead call-outcome notes). Any signed-in user; never required."""
    conn = get_db()
    rating = request.form.get("rating")
    if rating in ("good", "bad"):
        conn.execute("UPDATE pull_runs SET user_rating = ? WHERE id = ?", (rating, run_id))
    if "comment" in request.form:
        conn.execute("UPDATE pull_runs SET user_comment = ? WHERE id = ?",
                     (request.form.get("comment", "").strip(), run_id))
    conn.commit()
    return redirect(url_for("dashboard", run_id=run_id))


# ---------------------------------------------------------------- requeue

def run_requeue_now(day=None, campaign=None, dry_run=False):
    """Pull a day's VICIdial dispositions and requeue/suppress. Returns a summary.
    Safe to call from the scheduler or the 'Run now' button."""
    conn = db.connect()
    try:
        summary = requeue.run(conn, day=day, campaign=campaign, dry_run=dry_run)
        if not dry_run and "error" not in summary:
            db.set_setting(conn, "requeue_last_run", db.now_iso())
            db.set_setting(conn, "requeue_last_summary", json.dumps(summary))
            conn.commit()
        return summary
    finally:
        conn.close()


# How far back the nightly job will reach to recover missed days. Bounds the work
# so a long outage (or a fresh deploy) can't trigger a huge historical sweep.
BACKFILL_MAX_DAYS = 14


def _days_to_backfill(last_day, today, cap=BACKFILL_MAX_DAYS):
    """Days (oldest→newest, 'YYYY-MM-DD') to process this run: from the day after
    the last successful run through today, clamped to at most `cap` days back. On
    the very first run (no last_day) this is just [today] — no historical sweep."""
    fmt = "%Y-%m-%d"
    today_d = datetime.strptime(today, fmt).date()
    start = today_d
    if last_day:
        try:
            start = datetime.strptime(last_day, fmt).date() + timedelta(days=1)
        except ValueError:
            start = today_d
    earliest = today_d - timedelta(days=cap)
    start = min(max(start, earliest), today_d)
    days, d = [], start
    while d <= today_d:
        days.append(d.strftime(fmt))
        d += timedelta(days=1)
    return days


def run_requeue_backfill():
    """Scheduled entry point. Processes today PLUS any days missed since the last
    successful run, so a night the job didn't fire (deploy, container asleep,
    VICIdial briefly unreachable) never costs us paid-for leads. process() is
    idempotent, so re-touching an already-done day is a no-op."""
    today = requeue.today_est()
    conn = db.connect()
    try:
        last = db.get_setting(conn, "requeue_last_success_day", "")
    finally:
        conn.close()
    days = _days_to_backfill(last, today)

    results = [run_requeue_now(day=d, dry_run=False) for d in days]
    ok = [r for r in results if "error" not in r]
    if not ok:
        return results  # VICIdial unreachable — don't advance the marker; retry next run

    conn = db.connect()
    try:
        db.set_setting(conn, "requeue_last_success_day", today)
        missed = [d for d in days if d != today]
        if missed:
            recovered = sum(r.get("requeued", 0) for r in ok if r.get("day") in missed)
            db.add_alert(
                conn,
                f"Backfill recovered {len(missed)} missed day(s) "
                f"({missed[0]}…{missed[-1]}) — {recovered} not-reached leads re-served.",
                kind="requeue_backfill", link="/requeue",
            )
        db.set_setting(conn, "requeue_last_run", db.now_iso())
        conn.commit()
    finally:
        conn.close()
    return results


@app.route("/requeue")
def requeue_page():
    conn = get_db()
    last_summary = db.get_setting(conn, "requeue_last_summary", "")
    try:
        last_summary = json.loads(last_summary) if last_summary else None
    except ValueError:
        last_summary = None
    active = conn.execute(
        "SELECT COUNT(*) AS n FROM requeue_leads WHERE state='active'").fetchone()["n"]
    filter_campaign = request.args.get("campaign", "").strip()
    # Campaign choices: live VICIdial campaigns if reachable, else whatever we've seen.
    vici_campaigns = ops_dispositions.list_campaigns() if ops_dispositions.enabled() else []
    campaign_ids = [c["campaign_id"] for c in vici_campaigns] or requeue.campaigns_in_requeue(conn)
    return render_template(
        "requeue.html", rows=requeue.dashboard_rows(conn, campaign=filter_campaign or None),
        active_count=active,
        suppressed_count=conn.execute("SELECT COUNT(*) AS n FROM suppressed_leads").fetchone()["n"],
        last_run=db.get_setting(conn, "requeue_last_run", ""),
        last_summary=last_summary,
        run_time=db.get_setting(conn, "requeue_run_time", "23:30"),
        ops_connected=ops_dispositions.enabled(),
        retry_codes=", ".join(ops_dispositions.RETRY_CODES),
        campaigns=campaign_ids, filter_campaign=filter_campaign,
        today=requeue.today_est(),
        segments=requeue.segments(conn),
        alerts=db.unseen_alerts(conn),
    )


@app.route("/alerts/<int:alert_id>/dismiss", methods=["POST"])
def alert_dismiss(alert_id):
    conn = get_db()
    db.mark_alert_seen(conn, alert_id)
    conn.commit()
    return redirect(request.form.get("next") or url_for("requeue_page"))


@app.route("/requeue/run", methods=["POST"])
def requeue_run():
    guard = _require_admin()
    if guard:
        return guard
    dry = request.form.get("dry_run") == "1"
    day = request.form.get("day", "").strip() or None
    campaign = request.form.get("campaign", "").strip() or None
    summary = run_requeue_now(day=day, campaign=campaign, dry_run=dry)
    # A real run just SAVES a dated "regenerated" list into the system and raises
    # an alert — nothing is sent to VICIdial. The admin downloads it when ready.
    if not dry and "error" not in summary and summary.get("requeued", 0):
        conn = get_db()
        db.add_alert(
            conn,
            f"New regenerated list ready to upload: {summary['requeued']} leads "
            f"from {summary.get('day','')}"
            + (f" ({campaign})" if campaign else "")
            + f" — suppressed {summary.get('suppressed',0)}, DNC {summary.get('dnc',0)}.",
            kind="regen_list", link=url_for("requeue_page"),
        )
        conn.commit()
    return render_template("requeue_result.html", summary=summary, dry_run=dry,
                           day=day or "", campaign=campaign or "")


@app.route("/requeue/<int:requeue_id>/exclude", methods=["POST"])
def requeue_exclude(requeue_id):
    conn = get_db()
    conn.execute("UPDATE requeue_leads SET state='excluded', updated_at=? WHERE id=?",
                 (db.now_iso(), requeue_id))
    conn.commit()
    return redirect(url_for("requeue_page"))


@app.route("/requeue/<int:requeue_id>/reactivate", methods=["POST"])
def requeue_reactivate(requeue_id):
    conn = get_db()
    # Only re-activate if still under the cap (callbacks get the higher cap).
    row = conn.execute(
        "SELECT attempt_count, last_disposition FROM requeue_leads WHERE id=?",
        (requeue_id,)).fetchone()
    if row and row["attempt_count"] < requeue._cap_for((row["last_disposition"] or "").strip().upper()):
        conn.execute("UPDATE requeue_leads SET state='active', updated_at=? WHERE id=?",
                     (db.now_iso(), requeue_id))
        conn.commit()
    return redirect(url_for("requeue_page"))


@app.route("/requeue/inspect")
def requeue_inspect():
    """Read-only look at what a client's VICIdial list actually stores for these
    numbers — so we know which fields to map into regenerated leads (and what's
    simply missing and would need external enrichment)."""
    day = request.args.get("day", "").strip() or None
    campaign = request.args.get("campaign", "").strip() or None
    rows, error, fill = [], None, {}
    if ops_dispositions.enabled():
        try:
            rows = [dict(r) for r in ops_dispositions.sample_records(day=day, campaign=campaign)]
        except Exception as e:
            error = str(e)
        # For each column, what fraction of the sample is non-empty?
        if rows:
            for col in rows[0].keys():
                n = sum(1 for r in rows if str(r.get(col) or "").strip())
                fill[col] = f"{n}/{len(rows)}"
    vici_campaigns = ops_dispositions.list_campaigns() if ops_dispositions.enabled() else []
    return render_template(
        "requeue_inspect.html", rows=rows, error=error, fill=fill,
        ops_connected=ops_dispositions.enabled(),
        campaigns=[c["campaign_id"] for c in vici_campaigns],
        day=day or requeue.today_est(), campaign=campaign or "",
    )


@app.route("/requeue/callbacks")
def requeue_callbacks():
    """Read-only view of VICIdial's own scheduled callbacks (vicidial_callback).
    Informational: the dialer re-serves these at callback_time — we don't. Shows
    who's owed a call and when, matched to our business names where possible."""
    conn = get_db()
    filter_campaign = request.args.get("campaign", "").strip()
    rows, error = [], None
    if ops_dispositions.enabled():
        try:
            raw = ops_dispositions.fetch_callbacks(campaign=filter_campaign or None)
        except Exception as e:
            raw, error = [], str(e)
        biz = {}
        for r in conn.execute("SELECT business_name, phone FROM leads"):
            p = normalize_phone(r["phone"])
            if p:
                biz[p] = r["business_name"]
        now_est = datetime.now(requeue.EST).replace(tzinfo=None)
        for c in raw:
            d = dict(c)
            d["business_name"] = biz.get(normalize_phone(c.get("phone")), "")
            cbt = c.get("callback_time")
            d["due"] = bool(cbt and not isinstance(cbt, str) and cbt <= now_est)
            rows.append(d)
    vici_campaigns = ops_dispositions.list_campaigns() if ops_dispositions.enabled() else []
    return render_template(
        "callbacks.html", rows=rows, error=error,
        ops_connected=ops_dispositions.enabled(),
        campaigns=[c["campaign_id"] for c in vici_campaigns],
        filter_campaign=filter_campaign,
    )


# ---------------------------------------------------------------- DNC suppression

@app.route("/dnc")
def dnc_page():
    conn = get_db()
    recent = conn.execute(
        "SELECT phone, source, reason, added_at FROM dnc_numbers ORDER BY added_at DESC LIMIT 20"
    ).fetchall()
    by_source = conn.execute(
        "SELECT source, COUNT(*) AS n FROM dnc_numbers GROUP BY source ORDER BY n DESC"
    ).fetchall()
    return render_template(
        "dnc.html", total=dnc.count(conn), recent=recent, by_source=by_source,
        blocked_leads=conn.execute("SELECT COUNT(*) AS n FROM leads WHERE status='dnc'").fetchone()["n"],
        provider_configured=bool(os.environ.get("DNC_API_KEY")),
    )


@app.route("/dnc/upload", methods=["POST"])
def dnc_upload():
    f = request.files.get("file")
    conn = get_db()
    result = {"added": 0, "rows": 0}
    if f and f.filename:
        text = f.read().decode("utf-8-sig", errors="replace")
        result = dnc.import_csv(conn, text, source="upload")
    blocked = dnc.scrub_leads(conn)  # apply the new list to existing leads
    return render_template("dnc_result.html", added=result["added"], rows=result["rows"],
                           blocked=blocked)


@app.route("/dnc/add", methods=["POST"])
def dnc_add():
    number = request.form.get("phone", "").strip()
    conn = get_db()
    dnc.add_numbers(conn, [number], source="manual", reason="added by hand")
    conn.commit()
    dnc.scrub_leads(conn)
    return redirect(url_for("dnc_page"))


@app.route("/dnc/scrub", methods=["POST"])
def dnc_scrub():
    conn = get_db()
    blocked = dnc.scrub_leads(conn)
    return render_template("dnc_result.html", added=0, rows=0, blocked=blocked, scrub_only=True)


# ---------------------------------------------------------------- B2C lead intake

@app.route("/api/intake", methods=["POST"])
def api_intake():
    """Vendor/ad-funnel webhook: POST a single consumer lead as JSON.
    Auth: header 'X-Intake-Key' must equal the INTAKE_API_KEY env var.
    Exempt from the browser password gate (see require_password)."""
    expected = os.environ.get("INTAKE_API_KEY", "")
    if not expected:
        return jsonify({"error": "Lead intake is not enabled (INTAKE_API_KEY unset)"}), 503
    if request.headers.get("X-Intake-Key", "") != expected:
        return jsonify({"error": "Invalid intake key"}), 401

    data = request.get_json(silent=True) or {}
    # accept first/last split or a combined name
    payload = {
        "name": data.get("name") or "",
        "first_name": data.get("first_name") or "",
        "last_name": data.get("last_name") or "",
        "phone": data.get("phone") or data.get("mobile") or "",
        "email": data.get("email") or "",
        "city": data.get("city") or "",
        "state": data.get("state") or "",
        "postcode": data.get("zip") or data.get("postcode") or "",
        "product_interest": data.get("product_interest") or data.get("interest") or "",
        "preferred_contact_time": data.get("preferred_contact_time") or "",
        "consent_status": data.get("consent") if data.get("consent") is not None
                          else data.get("consent_status", ""),
        "consent_at": data.get("consent_at") or "",
        "notes": data.get("notes") or "",
    }
    source = "api:" + (data.get("source") or "vendor")
    conn = get_db()
    campaign = None
    if data.get("campaign"):
        # 'campaign' here names an OFFER slug (scoring profile) for this lead.
        campaign = conn.execute(
            "SELECT * FROM offers WHERE slug = ?", (data["campaign"],)
        ).fetchone()
    result = leads_intake.intake_one(conn, payload, source, campaign=campaign)
    conn.commit()
    status = 201 if result == "added" else 200
    return jsonify({"ok": result != "invalid", "result": result}), (
        400 if result == "invalid" else status)


@app.route("/import/leads", methods=["GET", "POST"])
def import_leads():
    """Upload a CSV list of consumer (B2C) leads."""
    summary = error = None
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            error = "Choose a CSV file first."
        else:
            try:
                text = f.read().decode("utf-8-sig", errors="replace")
                summary = leads_intake.import_csv(get_db(), text, f"csv:{f.filename}")
                summary["filename"] = f.filename
            except ValueError as e:
                error = str(e)
    return render_template("import_leads.html", summary=summary, error=error,
                           intake_enabled=bool(os.environ.get("INTAKE_API_KEY")))


# ------------------------------------------------ B2B intent signals (company-level)

@app.route("/api/signal", methods=["POST"])
def api_signal():
    """Webhook for a B2B intent signal (site-visitor or research-intent), matched
    onto an existing lead by domain/company name. Auth: header 'X-Signal-Key' must
    equal SIGNAL_API_KEY. Public (like api_intake). No lead is auto-created — an
    unmatched signal is stored for review/backfill."""
    expected = os.environ.get("SIGNAL_API_KEY", "")
    if not expected:
        return jsonify({"error": "Signals not enabled (SIGNAL_API_KEY unset)"}), 503
    if request.headers.get("X-Signal-Key", "") != expected:
        return jsonify({"error": "Invalid signal key"}), 401
    data = request.get_json(silent=True) or {}
    kind = (data.get("kind") or ("intent" if data.get("topic") else "site_visitor")).strip()
    if kind not in intent_signals.KINDS:
        return jsonify({"error": f"kind must be one of {intent_signals.KINDS}"}), 400
    payload = {
        "company_name": data.get("company_name") or data.get("company") or "",
        "domain": data.get("domain") or data.get("website") or "",
        "signal_strength": data.get("signal_strength") or data.get("visits")
                           or data.get("intent_score") or "",
        "last_seen_date": data.get("last_seen_date") or data.get("last_seen") or "",
        "topic": data.get("topic") or "",
    }
    conn = get_db()
    affected = []
    result = intent_signals.apply_one(
        conn, kind, payload, intent_signals.build_index(conn),
        "api:" + (data.get("source") or "vendor"), affected)
    conn.commit()
    scoring.rescore_leads(conn, affected)
    return jsonify({"ok": result != "invalid", "result": result}), (
        400 if result == "invalid" else 200)


@app.route("/import/signals", methods=["GET", "POST"])
def import_signals():
    """Upload a CSV of B2B intent signals (site-visitor or intent)."""
    guard = _require_admin()
    if guard:
        return guard
    summary = error = None
    if request.method == "POST":
        kind = request.form.get("kind", "site_visitor")
        f = request.files.get("file")
        if not f or not f.filename:
            error = "Choose a CSV file first."
        elif kind not in intent_signals.KINDS:
            error = "Pick a signal type."
        else:
            try:
                text = f.read().decode("utf-8-sig", errors="replace")
                summary = intent_signals.import_csv(get_db(), text, kind, f"csv:{f.filename}")
                summary["filename"] = f.filename
                summary["kind"] = kind
            except ValueError as e:
                error = str(e)
    conn = get_db()
    return render_template("import_signals.html", summary=summary, error=error,
                           kinds=intent_signals.KINDS,
                           unmatched=intent_signals.unmatched_count(conn),
                           signal_enabled=bool(os.environ.get("SIGNAL_API_KEY")))


@app.route("/signals/unmatched")
def signals_unmatched():
    guard = _require_admin()
    if guard:
        return guard
    conn = get_db()
    return render_template("signals_unmatched.html",
                           rows=intent_signals.unmatched_list(conn),
                           total=intent_signals.unmatched_count(conn))


@app.route("/signals/rematch", methods=["POST"])
def signals_rematch():
    guard = _require_admin()
    if guard:
        return guard
    n = intent_signals.rematch_unmatched(get_db())
    return redirect(url_for("signals_unmatched", matched=n))


# ---------------------------------------------------------------- call log import

@app.route("/import", methods=["GET", "POST"])
def import_call_log():
    summary = error = None
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            error = "Choose a CSV file first."
        else:
            try:
                text = f.read().decode("utf-8-sig", errors="replace")
                outcomes, skipped = dialer_import.parse_log(text)
                summary = dialer_import.apply_outcomes(get_db(), outcomes)
                summary["parsed"] = len(outcomes)
                summary["skipped_rows"] = skipped
                summary["filename"] = f.filename
            except ValueError as e:
                error = str(e)
    return render_template("import.html", summary=summary, error=error)


# ---------------------------------------------------------------- lead updates

@app.route("/api/leads/<int:lead_id>/status", methods=["POST"])
def set_lead_status(lead_id):
    status = (request.json or {}).get("status", "")
    if status not in db.LEAD_STATUSES:
        return jsonify({"error": f"Invalid status '{status}'"}), 400
    # Sub-reason only makes sense for a 'no'. Any other status clears it, so a
    # stale reason can never linger on a lead that later became interested.
    reason = (request.json or {}).get("not_interested_reason") or ""
    if status != "not_interested":
        reason = ""
    elif reason and reason not in db.NOT_INTERESTED_REASONS:
        return jsonify({"error": f"Invalid reason '{reason}'"}), 400
    conn = get_db()
    conn.execute(
        "UPDATE leads SET status = ?, status_updated_at = ?, not_interested_reason = ? "
        "WHERE id = ?",
        (status, db.now_iso(), reason, lead_id),
    )
    conn.commit()
    return jsonify({"ok": True, "status": status, "not_interested_reason": reason})


@app.route("/api/leads/<int:lead_id>/notes", methods=["POST"])
def set_lead_notes(lead_id):
    notes = (request.json or {}).get("notes", "")
    conn = get_db()
    conn.execute("UPDATE leads SET notes = ? WHERE id = ?", (notes, lead_id))
    conn.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- pull control

def _pull_worker(run_id, industries, target, api_key, location, campaign_id=None,
                 locations=None, source="maps"):
    try:
        pipeline.run_pull(industries, target, api_key, location=location,
                          locations=locations, run_id=run_id,
                          campaign_id=campaign_id, source=source, log=lambda *_: None)
    except Exception:
        pass  # error is already recorded on the pull_runs row
    finally:
        _pull_lock.release()


@app.route("/api/pull", methods=["POST"])
def start_pull():
    payload = request.json or {}
    api_key = pipeline.get_api_key()
    if not api_key:
        return jsonify({"error": "OUTSCRAPER_API_KEY is not set (env var or .env file)"}), 400

    conn = get_db()

    # A pull can be tagged to a campaign (client engagement) so its leads attribute
    # to that client and share its VICIdial redial bridge. Industry + geography are
    # chosen on the dashboard at pull time (the campaign only PRE-FILLS them), so one
    # campaign can sweep many cities/industries over time.
    campaign_id = payload.get("campaign_id")
    camp = None
    if campaign_id:
        camp = conn.execute(
            "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if not camp:
            return jsonify({"error": "Unknown campaign"}), 400

    source = (payload.get("source") or "maps").strip()
    if source not in ("maps", "nppes", "both"):
        return jsonify({"error": "Unknown source"}), 400
    if source in ("nppes", "both") and not any(nppes.supports(i) for i in
                                               (payload.get("industries") or [])):
        if source == "nppes":
            return jsonify({"error": "NPPES only covers licensed US healthcare "
                            "provider types — pick one of: "
                            + ", ".join(nppes.supported_industries())}), 400
    if payload.get("all_industries"):
        # Sweep the whole catalog — the pull loops every industry until the target
        # is hit (raise the target to reach more of them).
        industries = [r["slug"] for r in conn.execute(
            "SELECT slug FROM industries WHERE enabled = 1 ORDER BY label")]
        if not industries:
            return jsonify({"error": "No industries are configured."}), 400
    else:
        industries = payload.get("industries") or [
            payload.get("industry") or db.get_setting(conn, "default_industry", "hvac")
        ]
        if not isinstance(industries, list) or not all(isinstance(s, str) for s in industries):
            return jsonify({"error": "industries must be a list of slugs"}), 400
        industries = [s.strip() for s in industries if s.strip()]
        if not industries:
            return jsonify({"error": "Choose at least one industry to pull."}), 400
    try:
        target = int(payload.get("target") or db.get_setting(conn, "target_leads_per_day", "100"))
    except ValueError:
        return jsonify({"error": "Target must be a number"}), 400
    if target < 1:
        return jsonify({"error": "Target must be at least 1"}), 400

    # Per-user limits (admins are unlimited). A non-admin can only pull for their
    # assigned campaign(s) and up to their remaining daily/total lead quota; the
    # target is capped to whatever quota is left so they can't overshoot.
    user = getattr(g, "user", None)
    if user and user["role"] != "admin":
        allowed = users.allowed_campaign_ids(user)
        if allowed and (not campaign_id or int(campaign_id) not in allowed):
            return jsonify({"error": "You can only pull for your assigned campaign(s). "
                            "Pick one of your campaigns from the dropdown."}), 403
        used_total, used_today = users.usage(conn, user["id"])
        caps = []
        if user["lead_limit_total"]:
            caps.append(user["lead_limit_total"] - used_total)
        if user["lead_limit_daily"]:
            caps.append(user["lead_limit_daily"] - used_today)
        if caps:
            remaining = min(caps)
            if remaining <= 0:
                return jsonify({"error": "You've reached your lead limit. Ask an admin "
                                "to raise it or reset your usage."}), 403
            target = min(target, remaining)

    # Location: a multi-select list from the dashboard, or a single typed location,
    # else fall back to saved cities. All entered on the dashboard now (never locked
    # to the campaign), so one campaign can sweep several cities in one run.
    def _clean_loc(l):
        return {"city": (l.get("city") or "").strip(),
                "state": (l.get("state") or "").strip(),
                "country": (l.get("country") or "").strip()}

    raw_locs = payload.get("locations")
    locations = None
    if isinstance(raw_locs, list):
        locations = [_clean_loc(l) for l in raw_locs
                     if isinstance(l, dict) and (l.get("city") or l.get("state"))]
    single = _clean_loc(payload.get("location") or {})
    location = single if (single["city"] or single["state"]) else None

    if not locations and not location:
        # No explicit location: only allowed if there are saved cities to fall back on.
        if not conn.execute("SELECT 1 FROM cities WHERE enabled = 1 LIMIT 1").fetchone():
            return jsonify({"error": "Enter a City and State to pull from "
                            "(or add several under Multiple locations)."}), 400
    if location:
        # Remember the single location so the inputs pre-fill next time.
        for key, setting in (("city", "last_city"), ("state", "last_state"),
                             ("country", "last_country")):
            db.set_setting(conn, setting, location[key])
        conn.commit()

    if not _pull_lock.acquire(blocking=False):
        return jsonify({"error": "A pull is already running"}), 409

    cur = conn.execute(
        "INSERT INTO pull_runs (started_at, industry, target, campaign_id, user_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (db.now_iso(), ",".join(industries), target, camp["id"] if camp else None,
         user["id"] if user else None),
    )
    conn.commit()
    run_id = cur.lastrowid

    threading.Thread(
        target=_pull_worker,
        args=(run_id, industries, target, api_key, location, camp["id"] if camp else None),
        kwargs={"locations": locations, "source": source},
        daemon=True,
    ).start()
    return jsonify({"ok": True, "run_id": run_id})


@app.route("/api/pull/status")
def pull_status():
    conn = get_db()
    run = conn.execute("SELECT * FROM pull_runs ORDER BY id DESC LIMIT 1").fetchone()
    return jsonify(dict(run) if run else {"status": "none"})


@app.route("/api/pull/cancel", methods=["POST"])
def cancel_pull():
    """Ask the running pull to stop. The worker checks this flag between queries
    and finishes gracefully, keeping whatever leads it already gathered."""
    conn = get_db()
    run = conn.execute(
        "SELECT id, status FROM pull_runs WHERE status = 'running' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not run:
        return jsonify({"error": "No pull is running"}), 409
    conn.execute("UPDATE pull_runs SET cancel = 1 WHERE id = ?", (run["id"],))
    conn.commit()
    return jsonify({"ok": True})


# --- on-demand phone verification (independent of a pull) ---

_verify_lock = threading.Lock()


def _verify_worker(lead_ids, api_key):
    try:
        conn = db.connect()
        rows = conn.execute(
            f"SELECT id, phone FROM leads WHERE id IN ({','.join('?' * len(lead_ids))})",
            lead_ids,
        ).fetchall()
        pipeline.verify_lead_phones(conn, api_key, rows, log=lambda *_: None)
        conn.close()
    except Exception:
        pass
    finally:
        _verify_lock.release()


@app.route("/api/verify_phones", methods=["POST"])
def verify_phones():
    """Validate phone numbers for the currently filtered leads (default: not yet
    validated). Runs in the background; poll /api/verify_phones/status."""
    api_key = pipeline.get_api_key()
    if not api_key:
        return jsonify({"error": "OUTSCRAPER_API_KEY is not set"}), 400

    conn = get_db()
    args = (request.json or {})
    clause, params = lead_filters(args)
    only_new = args.get("only_unvalidated", True)
    extra = " AND validated_at IS NULL" if only_new else ""
    sql = f"SELECT id FROM leads {clause or 'WHERE 1=1'}{extra}"
    lead_ids = [r["id"] for r in conn.execute(sql, params)]
    if not lead_ids:
        return jsonify({"error": "No unvalidated leads match the current view"}), 400

    if not _verify_lock.acquire(blocking=False):
        return jsonify({"error": "A verification run is already in progress"}), 409

    threading.Thread(
        target=_verify_worker, args=(lead_ids, api_key), daemon=True
    ).start()
    return jsonify({"ok": True, "count": len(lead_ids)})


@app.route("/api/verify_phones/status")
def verify_phones_status():
    running = _verify_lock.locked()
    return jsonify({"running": running})


# --- on-demand contact enrichment (Apollo) — fills decision-maker name/title ---

_enrich_lock = threading.Lock()


def _enrich_worker(lead_ids, reveal_email, reveal_phone):
    try:
        conn = db.connect()
        rows = conn.execute(
            f"SELECT id, website, contact, email, employee_count FROM leads "
            f"WHERE id IN ({','.join('?' * len(lead_ids))})",
            lead_ids,
        ).fetchall()
        result = contacts.enrich_leads(conn, rows, reveal_email=reveal_email,
                                       reveal_phone=reveal_phone, log=lambda *_: None)
        db.set_setting(conn, "enrich_last_result", json.dumps(result))
        conn.commit()
        conn.close()
    except Exception as e:
        try:
            conn = db.connect()
            db.set_setting(conn, "enrich_last_result", json.dumps({"error": str(e)}))
            conn.commit()
            conn.close()
        except Exception:
            pass
    finally:
        _enrich_lock.release()


@app.route("/api/enrich_contacts", methods=["POST"])
def enrich_contacts():
    """Enrich the currently filtered leads that have a website with a decision-maker
    name/title (+ email/direct-dial per the reveal settings). Background; poll
    /api/enrich_contacts/status. Free for names/titles; reveal spends Apollo credits."""
    if not contacts.enabled():
        return jsonify({"error": "APOLLO_API_KEY is not set — add it in Railway → "
                        "Variables to enable contact enrichment."}), 400
    conn = get_db()
    args = request.json or {}
    picked = args.get("lead_ids")
    if isinstance(picked, list) and picked:
        # Explicit row selection from the dashboard: use exactly these leads
        # (still only the ones that have a website to look up).
        ids = [int(i) for i in picked if str(i).strip().isdigit()]
        ph = ",".join("?" * len(ids)) or "NULL"
        lead_ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM leads WHERE id IN ({ph}) AND website != ''", ids)]
    else:
        clause, params = lead_filters(args)
        # All website leads in the view — the waterfall in enrich_leads() skips (for
        # free) any that Outscraper already fully enriched, and only spends Apollo on
        # the gaps. Selecting them all is what makes the "skipped" savings visible.
        extra = " AND website != ''"
        sql = f"SELECT id FROM leads {clause or 'WHERE 1=1'}{extra}"
        lead_ids = [r["id"] for r in conn.execute(sql, params)]
    if not lead_ids:
        return jsonify({"error": "No leads with a website in this view to enrich"}), 400

    reveal_email = db.get_setting(conn, "enrich_reveal_email", "1") == "1"
    reveal_phone = db.get_setting(conn, "enrich_reveal_phone", "0") == "1"
    if not _enrich_lock.acquire(blocking=False):
        return jsonify({"error": "An enrichment run is already in progress"}), 409
    threading.Thread(
        target=_enrich_worker, args=(lead_ids, reveal_email, reveal_phone), daemon=True
    ).start()
    return jsonify({"ok": True, "count": len(lead_ids)})


@app.route("/api/enrich_contacts/status")
def enrich_contacts_status():
    conn = get_db()
    last = db.get_setting(conn, "enrich_last_result", "")
    try:
        last = json.loads(last) if last else None
    except ValueError:
        last = None
    return jsonify({"running": _enrich_lock.locked(), "last": last})


# ---------------------------------------------------------------- settings CRUD

@app.route("/settings/cities/add", methods=["POST"])
def add_city():
    name = request.form.get("name", "").strip()
    state = request.form.get("state", "").strip().upper()
    if name and state:
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO cities (name, state) VALUES (?, ?)", (name, state))
        conn.commit()
    return redirect(url_for("settings"))


@app.route("/settings/cities/<int:city_id>/toggle", methods=["POST"])
def toggle_city(city_id):
    conn = get_db()
    conn.execute("UPDATE cities SET enabled = 1 - enabled WHERE id = ?", (city_id,))
    conn.commit()
    return redirect(url_for("settings"))


@app.route("/settings/cities/<int:city_id>/delete", methods=["POST"])
def delete_city(city_id):
    conn = get_db()
    conn.execute("DELETE FROM cities WHERE id = ?", (city_id,))
    conn.commit()
    return redirect(url_for("settings"))


def create_industry(conn, label, query="", chains=None, slug=None):
    """Add one industry to the catalog (+ its chain exclusions). The single path
    used by BOTH the manual '+ add industry' form and AI-suggested approvals.
    Returns the slug, or '' when the label is empty."""
    label = (label or "").strip()
    if not label:
        return ""
    slug = (slug or re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")) or "industry"
    query = (query or "").strip()
    if "{city}" not in query:
        # Sensible default search phrase for a custom industry.
        query = f"{label.lower()} in {{city}}"
    cur = conn.execute(
        "INSERT OR IGNORE INTO industries (slug, label, query_template) VALUES (?, ?, ?)",
        (slug, label, query),
    )
    if cur.rowcount and chains:      # newly added -> seed its chain exclusions
        for chain in chains:
            chain = str(chain).strip()
            if chain:
                conn.execute(
                    "INSERT OR IGNORE INTO chains (industry_id, name) VALUES (?, ?)",
                    (cur.lastrowid, chain))
    return slug


@app.route("/settings/industries/add", methods=["POST"])
def add_industry():
    label = request.form.get("label", "").strip()
    if label:
        conn = get_db()
        slug = create_industry(conn, label, request.form.get("query", ""))
        if request.form.get("next") == "dashboard":
            # quick-add from the dashboard: preselect it for the next pull
            db.set_setting(conn, "default_industry", slug)
        conn.commit()
    if request.form.get("next") == "dashboard":
        return redirect(url_for("dashboard"))
    return redirect(url_for("settings"))


@app.route("/settings/industries/<int:industry_id>/query", methods=["POST"])
def edit_industry_query(industry_id):
    query = request.form.get("query", "").strip()
    if "{city}" in query:
        conn = get_db()
        conn.execute(
            "UPDATE industries SET query_template = ? WHERE id = ?", (query, industry_id)
        )
        conn.commit()
    return redirect(url_for("settings"))


@app.route("/settings/industries/<int:industry_id>/toggle", methods=["POST"])
def toggle_industry(industry_id):
    conn = get_db()
    conn.execute("UPDATE industries SET enabled = 1 - enabled WHERE id = ?", (industry_id,))
    conn.commit()
    return redirect(url_for("settings"))


@app.route("/settings/industries/<int:industry_id>/delete", methods=["POST"])
def delete_industry(industry_id):
    conn = get_db()
    conn.execute("DELETE FROM industries WHERE id = ?", (industry_id,))
    conn.commit()
    return redirect(url_for("settings"))


@app.route("/settings/industries/<int:industry_id>/chains/add", methods=["POST"])
def add_chain(industry_id):
    name = request.form.get("name", "").strip()
    if name:
        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO chains (industry_id, name) VALUES (?, ?)",
            (industry_id, name),
        )
        conn.commit()
    return redirect(url_for("settings"))


@app.route("/settings/chains/<int:chain_id>/delete", methods=["POST"])
def delete_chain(chain_id):
    conn = get_db()
    conn.execute("DELETE FROM chains WHERE id = ?", (chain_id,))
    conn.commit()
    return redirect(url_for("settings"))


@app.route("/settings/campaign/activate", methods=["POST"])
def activate_campaign():
    """Set the DEFAULT offer (used to score leads with no campaign) and re-rank.
    Per-campaign leads keep their own campaign's offer; only unassigned leads
    follow this default."""
    slug = request.form.get("slug", "").strip()
    conn = get_db()
    offer = conn.execute("SELECT * FROM offers WHERE slug = ?", (slug,)).fetchone()
    if offer:
        db.set_setting(conn, "active_campaign", slug)
        conn.commit()
        scoring.rescore_everything(conn)
    next_page = request.form.get("next", "settings")
    return redirect(url_for("dashboard" if next_page == "dashboard" else "settings"))


@app.route("/settings/campaigns/add", methods=["POST"])
def add_campaign():
    """Create a custom campaign by cloning a preset's scoring rules, then
    overriding name / audience / goal."""
    name = request.form.get("name", "").strip()
    base = request.form.get("base", "seo").strip()
    audience = request.form.get("audience", "b2b").strip()
    goal = request.form.get("goal", "close").strip()
    if name:
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "campaign"
        conn = get_db()
        base_row = conn.execute(
            "SELECT rules, site_check FROM offers WHERE slug = ?", (base,)
        ).fetchone()
        rules = base_row["rules"] if base_row else "{}"
        site_check = base_row["site_check"] if base_row else 0
        conn.execute(
            "INSERT OR IGNORE INTO offers (slug, name, audience, goal, rules, is_preset, site_check) "
            "VALUES (?, ?, ?, ?, ?, 0, ?)",
            (slug, name, audience if audience in db.MARKET_TYPES else "b2b",
             goal if goal in ("close", "appointment") else "close", rules, site_check),
        )
        conn.commit()
    return redirect(url_for("settings"))


@app.route("/settings/campaigns/<int:campaign_id>/edit", methods=["POST"])
def edit_campaign(campaign_id):
    """Edit a campaign's name / market type / goal in the UI (no code changes)."""
    name = request.form.get("name", "").strip()
    audience = request.form.get("audience", "").strip()
    goal = request.form.get("goal", "").strip()
    conn = get_db()
    if name:
        conn.execute("UPDATE offers SET name = ? WHERE id = ?", (name, campaign_id))
    if audience in db.MARKET_TYPES:
        conn.execute("UPDATE offers SET audience = ? WHERE id = ?", (audience, campaign_id))
    if goal in ("close", "appointment"):
        conn.execute("UPDATE offers SET goal = ? WHERE id = ?", (goal, campaign_id))
    if "pain_keywords" in request.form:
        # Comma-separated in the UI, stored as a JSON list.
        kws = [k.strip() for k in request.form.get("pain_keywords", "").split(",") if k.strip()]
        conn.execute("UPDATE offers SET pain_keywords = ? WHERE id = ?",
                     (json.dumps(kws), campaign_id))
    conn.commit()
    return redirect(url_for("settings"))


@app.route("/settings/campaigns/<int:campaign_id>/toggle", methods=["POST"])
def toggle_campaign(campaign_id):
    """Enable/disable an offer (hides it from the offer pickers)."""
    conn = get_db()
    conn.execute("UPDATE offers SET enabled = 1 - enabled WHERE id = ?", (campaign_id,))
    conn.commit()
    return redirect(url_for("settings"))


@app.route("/settings/campaigns/<int:campaign_id>/delete", methods=["POST"])
def delete_campaign(campaign_id):
    conn = get_db()
    row = conn.execute("SELECT slug, is_preset FROM offers WHERE id = ?", (campaign_id,)).fetchone()
    if row and not row["is_preset"]:
        # Don't leave the default-offer pointer dangling.
        if db.get_setting(conn, "active_campaign") == row["slug"]:
            db.set_setting(conn, "active_campaign", db.DEFAULT_OFFER)
        conn.execute("DELETE FROM offers WHERE id = ?", (campaign_id,))
        conn.commit()
    return redirect(url_for("settings"))


@app.route("/settings/general", methods=["POST"])
def save_general_settings():
    conn = get_db()
    target = request.form.get("target", "").strip()
    if target.isdigit() and int(target) > 0:
        db.set_setting(conn, "target_leads_per_day", target)
    industry = request.form.get("default_industry", "").strip()
    if industry:
        db.set_setting(conn, "default_industry", industry)
    db.set_setting(conn, "contact_enrichment",
                   "1" if request.form.get("contact_enrichment") else "0")
    db.set_setting(conn, "phone_validation",
                   "1" if request.form.get("phone_validation") else "0")
    db.set_setting(conn, "review_signals",
                   "1" if request.form.get("review_signals") else "0")
    try:
        buf = float(request.form.get("buffer_multiplier", "1.4"))
        db.set_setting(conn, "buffer_multiplier", str(max(1.0, min(3.0, buf))))
    except ValueError:
        pass
    db.set_setting(conn, "drop_voip_export",
                   "1" if request.form.get("drop_voip_export") else "0")
    db.set_setting(conn, "enrich_reveal_email",
                   "1" if request.form.get("enrich_reveal_email") else "0")
    db.set_setting(conn, "enrich_reveal_phone",
                   "1" if request.form.get("enrich_reveal_phone") else "0")
    conn.commit()
    return redirect(url_for("settings"))


_scheduler = None


def _start_scheduler():
    """Start the daily requeue job (EST) in-process. One gunicorn worker => one
    scheduler, so it fires once. Off in tests via ENABLE_SCHEDULER=0."""
    global _scheduler
    if _scheduler is not None or os.environ.get("ENABLE_SCHEDULER", "1") != "1":
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from zoneinfo import ZoneInfo
        conn = db.connect()
        run_time = db.get_setting(conn, "requeue_run_time", "23:30")
        conn.close()
        hour, _, minute = run_time.partition(":")
        sched = BackgroundScheduler(timezone=ZoneInfo("America/New_York"))
        sched.add_job(run_requeue_backfill,
                      CronTrigger(hour=int(hour or 23), minute=int(minute or 30)),
                      id="requeue_daily", replace_existing=True)
        sched.start()
        _scheduler = sched
    except Exception as e:  # never let scheduling break app boot
        print(f"[scheduler] not started: {e}")


_start_scheduler()


def _port_in_use(host, port):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _open_browser(url):
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:
        pass  # never let a missing browser stop the server


if __name__ == "__main__":
    import threading

    HOST, PORT = "127.0.0.1", 5000
    URL = f"http://localhost:{PORT}"

    # Double-clicked while already running? Just open the browser and exit.
    if _port_in_use(HOST, PORT):
        print(f"SEO Leads is already running. Opening {URL} ...")
        _open_browser(URL)
    else:
        # DB already initialized at import time. Open the browser once the
        # server is up.
        threading.Timer(1.5, lambda: _open_browser(URL)).start()
        print("\n" + "=" * 52)
        print("  SEO Leads is running.")
        print(f"  Your browser should open at {URL}")
        print("  If it doesn't, type that address into your browser.")
        print("  Keep this window open. Close it to stop the app.")
        print("=" * 52 + "\n")
        app.run(host=HOST, port=PORT, debug=False)
