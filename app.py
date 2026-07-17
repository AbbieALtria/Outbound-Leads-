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
import threading
from datetime import date

from flask import (Flask, g, jsonify, redirect, render_template,
                   request, session, url_for)

import db
import dialer_import
import dnc
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
# once on the next start instead of showing stale hooks.
SCORING_VERSION = "2"
if db.get_setting(_seed_conn, "scoring_version") != SCORING_VERSION:
    _active = db.get_setting(_seed_conn, "active_campaign", db.DEFAULT_ACTIVE_CAMPAIGN)
    _camp = _seed_conn.execute(
        "SELECT * FROM campaigns WHERE slug = ?", (_active,)).fetchone()
    if _camp:
        scoring.rescore_all(_seed_conn, _camp)
    db.set_setting(_seed_conn, "scoring_version", SCORING_VERSION)
    _seed_conn.commit()
_seed_conn.close()

_pull_lock = threading.Lock()

# Paths reachable without a login.
_PUBLIC_ENDPOINTS = {"login", "logout", "static", "api_intake"}


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
            return None
        session.clear()
    return redirect(url_for("login", next=request.path))


@app.context_processor
def inject_user():
    return {"current_user": getattr(g, "user", None)}


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


def _require_admin():
    """Return None if the current user is an admin, else a redirect response."""
    user = getattr(g, "user", None)
    if user and user["role"] == "admin":
        return None
    return redirect(url_for("dashboard"))


@app.route("/users")
def users_page():
    guard = _require_admin()
    if guard:
        return guard
    conn = get_db()
    return render_template("users.html", users=users.list_users(conn),
                           roles=users.ROLES, error=request.args.get("error"))


@app.route("/users/create", methods=["POST"])
def users_create():
    guard = _require_admin()
    if guard:
        return guard
    conn = get_db()
    ok, err = users.create_user(
        conn, request.form.get("username", ""), request.form.get("password", ""),
        request.form.get("role", "agent"), created_by=g.user["username"],
    )
    return redirect(url_for("users_page", error=None if ok else err))


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
        where.append("id IN (SELECT lead_id FROM requeue_leads WHERE state = 'active')")
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
    if run_id:
        batch = conn.execute("SELECT * FROM pull_runs WHERE id = ?", (run_id,)).fetchone()

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
    campaigns = conn.execute(
        "SELECT * FROM campaigns WHERE enabled = 1 ORDER BY audience, is_preset DESC, name"
    ).fetchall()
    active_slug = db.get_setting(conn, "active_campaign", db.DEFAULT_ACTIVE_CAMPAIGN)
    active_campaign = conn.execute(
        "SELECT * FROM campaigns WHERE slug = ?", (active_slug,)
    ).fetchone()
    return render_template(
        "dashboard.html", leads=leads, batch=batch, run_id=run_id,
        industries=industries, statuses=db.LEAD_STATUSES, counts=counts,
        current_status=request.args.get("status", ""),
        total_leads=total_leads,
        campaigns=campaigns, active_campaign=active_campaign,
        default_industry=db.get_setting(conn, "default_industry", "hvac"),
        default_target=db.get_setting(conn, "target_leads_per_day", "100"),
        countries=COUNTRIES, states_by_country=STATES_BY_COUNTRY,
        cities_by_state=geo_data.CITIES_BY_STATE,
        known_by_state=_known_cities_by_state(conn),
        last_city=db.get_setting(conn, "last_city", ""),
        last_state=db.get_setting(conn, "last_state", ""),
        last_country=db.get_setting(conn, "last_country", "United States"),
        api_key_set=bool(pipeline.get_api_key()),
    )


@app.route("/history")
def history():
    conn = get_db()
    leads = fetch_leads(conn, request.args)
    distinct = lambda col: [r[col] for r in conn.execute(
        f"SELECT DISTINCT {col} FROM leads WHERE {col} != '' ORDER BY {col}")]
    industries = conn.execute("SELECT * FROM industries ORDER BY label").fetchall()
    return render_template(
        "history.html", leads=leads,
        countries=distinct("country"), states=distinct("state"), cities=distinct("city"),
        industries=industries, statuses=db.LEAD_STATUSES, f=request.args,
    )


@app.route("/settings")
def settings():
    conn = get_db()
    cities = conn.execute("SELECT * FROM cities ORDER BY state, name").fetchall()
    industries = conn.execute("SELECT * FROM industries ORDER BY label").fetchall()
    chains = {}
    for row in conn.execute("SELECT * FROM chains ORDER BY name"):
        chains.setdefault(row["industry_id"], []).append(row)
    campaigns = conn.execute("SELECT * FROM campaigns ORDER BY is_preset DESC, name").fetchall()
    return render_template(
        "settings.html", cities=cities, industries=industries, chains=chains,
        campaigns=campaigns, market_types=db.MARKET_TYPES,
        active_campaign=db.get_setting(conn, "active_campaign", db.DEFAULT_ACTIVE_CAMPAIGN),
        target=db.get_setting(conn, "target_leads_per_day", "100"),
        default_industry=db.get_setting(conn, "default_industry", "hvac"),
        contact_enrichment=db.get_setting(conn, "contact_enrichment", "1") == "1",
        phone_validation=db.get_setting(conn, "phone_validation", "0") == "1",
        drop_voip_export=db.get_setting(conn, "drop_voip_export", "0") == "1",
        api_key_set=bool(pipeline.get_api_key()),
    )


@app.route("/export.csv")
def export_csv():
    conn = get_db()
    leads = fetch_leads(conn, request.args)
    # DNC numbers never go on a dial sheet (unless explicitly exporting the dnc list)
    if request.args.get("status") != "dnc":
        leads = [lead for lead in leads if lead["status"] != "dnc"]
    fields = ["business_name", "phone", "address", "city", "state",
              "website", "category", "score", "call_hook", "status", "notes",
              "pulled_date"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows([dict(lead) for lead in leads])
    name = f"leads_{request.args.get('date') or 'all'}.csv"
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
        out.append(lead)
    return out


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

    fields = ["Name", "Full_Address", "Street_Address", "City", "State", "Zip",
              "Website", "Phone", "Email", "Category", "URL"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for lead in leads:
        writer.writerow({
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
        })
    name = f"vicidial_{request.args.get('date') or 'all'}.csv"
    return app.response_class(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={name}"},
    )


# ---------------------------------------------------------------- requeue

def run_requeue_now(dry_run=False):
    """Pull today's VICIdial dispositions and requeue/suppress. Returns a summary.
    Safe to call from the scheduler or the 'Run now' button."""
    conn = db.connect()
    try:
        summary = requeue.run(conn, dry_run=dry_run)
        if not dry_run and "error" not in summary:
            db.set_setting(conn, "requeue_last_run", db.now_iso())
            db.set_setting(conn, "requeue_last_summary", json.dumps(summary))
            conn.commit()
        return summary
    finally:
        conn.close()


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
    return render_template(
        "requeue.html", rows=requeue.dashboard_rows(conn),
        active_count=active,
        suppressed_count=conn.execute("SELECT COUNT(*) AS n FROM suppressed_leads").fetchone()["n"],
        last_run=db.get_setting(conn, "requeue_last_run", ""),
        last_summary=last_summary,
        run_time=db.get_setting(conn, "requeue_run_time", "23:30"),
        ops_connected=ops_dispositions.enabled(),
        retry_codes=", ".join(ops_dispositions.RETRY_CODES),
    )


@app.route("/requeue/run", methods=["POST"])
def requeue_run():
    guard = _require_admin()
    if guard:
        return guard
    dry = request.form.get("dry_run") == "1"
    summary = run_requeue_now(dry_run=dry)
    return render_template("requeue_result.html", summary=summary, dry_run=dry)


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
    # Only re-activate if still under the cap.
    row = conn.execute("SELECT attempt_count FROM requeue_leads WHERE id=?", (requeue_id,)).fetchone()
    if row and row["attempt_count"] < requeue.CAP:
        conn.execute("UPDATE requeue_leads SET state='active', updated_at=? WHERE id=?",
                     (db.now_iso(), requeue_id))
        conn.commit()
    return redirect(url_for("requeue_page"))


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
        campaign = conn.execute(
            "SELECT * FROM campaigns WHERE slug = ?", (data["campaign"],)
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
    conn = get_db()
    conn.execute(
        "UPDATE leads SET status = ?, status_updated_at = ? WHERE id = ?",
        (status, db.now_iso(), lead_id),
    )
    conn.commit()
    return jsonify({"ok": True, "status": status})


@app.route("/api/leads/<int:lead_id>/notes", methods=["POST"])
def set_lead_notes(lead_id):
    notes = (request.json or {}).get("notes", "")
    conn = get_db()
    conn.execute("UPDATE leads SET notes = ? WHERE id = ?", (notes, lead_id))
    conn.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- pull control

def _pull_worker(run_id, industries, target, api_key, location):
    try:
        pipeline.run_pull(industries, target, api_key, location=location,
                          run_id=run_id, log=lambda *_: None)
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
    industries = payload.get("industries") or [
        payload.get("industry") or db.get_setting(conn, "default_industry", "hvac")
    ]
    if not isinstance(industries, list) or not all(isinstance(s, str) for s in industries):
        return jsonify({"error": "industries must be a list of slugs"}), 400
    try:
        target = int(payload.get("target") or db.get_setting(conn, "target_leads_per_day", "100"))
    except ValueError:
        return jsonify({"error": "Target must be a number"}), 400
    if target < 1:
        return jsonify({"error": "Target must be at least 1"}), 400

    # Location entered on the dashboard (city/state/country). Remember it so the
    # inputs pre-fill next time.
    loc = payload.get("location") or {}
    location = {"city": (loc.get("city") or "").strip(),
                "state": (loc.get("state") or "").strip(),
                "country": (loc.get("country") or "").strip()}
    if not (location["city"] or location["state"]):
        # No location typed: only allowed if there are saved cities to fall back on.
        if not conn.execute("SELECT 1 FROM cities WHERE enabled = 1 LIMIT 1").fetchone():
            return jsonify({"error": "Enter a City and State to pull from."}), 400
        location = None
    else:
        for key, setting in (("city", "last_city"), ("state", "last_state"),
                             ("country", "last_country")):
            db.set_setting(conn, setting, location[key])
        conn.commit()

    if not _pull_lock.acquire(blocking=False):
        return jsonify({"error": "A pull is already running"}), 409

    cur = conn.execute(
        "INSERT INTO pull_runs (started_at, industry, target) VALUES (?, ?, ?)",
        (db.now_iso(), ",".join(industries), target),
    )
    conn.commit()
    run_id = cur.lastrowid

    threading.Thread(
        target=_pull_worker, args=(run_id, industries, target, api_key, location),
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


@app.route("/settings/industries/add", methods=["POST"])
def add_industry():
    label = request.form.get("label", "").strip()
    if label:
        slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "industry"
        query = request.form.get("query", "").strip()
        if "{city}" not in query:
            # Sensible default search phrase for a custom industry.
            query = f"{label.lower()} in {{city}}"
        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO industries (slug, label, query_template) VALUES (?, ?, ?)",
            (slug, label, query),
        )
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
    """Switch the active campaign and re-score every lead under its rules —
    the same lead pool is instantly re-ranked for the new offer, no re-pull."""
    slug = request.form.get("slug", "").strip()
    conn = get_db()
    campaign = conn.execute("SELECT * FROM campaigns WHERE slug = ?", (slug,)).fetchone()
    if campaign:
        db.set_setting(conn, "active_campaign", slug)
        conn.commit()
        scoring.rescore_all(conn, campaign)
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
            "SELECT rules, site_check FROM campaigns WHERE slug = ?", (base,)
        ).fetchone()
        rules = base_row["rules"] if base_row else "{}"
        site_check = base_row["site_check"] if base_row else 0
        conn.execute(
            "INSERT OR IGNORE INTO campaigns (slug, name, audience, goal, rules, is_preset, site_check) "
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
        conn.execute("UPDATE campaigns SET name = ? WHERE id = ?", (name, campaign_id))
    if audience in db.MARKET_TYPES:
        conn.execute("UPDATE campaigns SET audience = ? WHERE id = ?", (audience, campaign_id))
    if goal in ("close", "appointment"):
        conn.execute("UPDATE campaigns SET goal = ? WHERE id = ?", (goal, campaign_id))
    conn.commit()
    return redirect(url_for("settings"))


@app.route("/settings/campaigns/<int:campaign_id>/toggle", methods=["POST"])
def toggle_campaign(campaign_id):
    """Activate/deactivate a campaign (hides it from the dashboard picker)."""
    conn = get_db()
    conn.execute("UPDATE campaigns SET enabled = 1 - enabled WHERE id = ?", (campaign_id,))
    conn.commit()
    return redirect(url_for("settings"))


@app.route("/settings/campaigns/<int:campaign_id>/delete", methods=["POST"])
def delete_campaign(campaign_id):
    conn = get_db()
    row = conn.execute("SELECT slug, is_preset FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if row and not row["is_preset"]:
        # Don't leave the active pointer dangling.
        if db.get_setting(conn, "active_campaign") == row["slug"]:
            db.set_setting(conn, "active_campaign", db.DEFAULT_ACTIVE_CAMPAIGN)
        conn.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
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
    db.set_setting(conn, "drop_voip_export",
                   "1" if request.form.get("drop_voip_export") else "0")
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
        sched.add_job(lambda: run_requeue_now(dry_run=False),
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
