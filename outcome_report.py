"""Learn from call outcomes — ANALYSIS ONLY.

For every campaign (and the unassigned pool, scored under the default offer),
this asks of each scoring signal: among the leads that have actually been
contacted, how did the ones the signal fired on fare against the ones it did
not fire on? Interested, appointment and not-interested rates are given for
both groups with their sample sizes, and a signal that fired on fewer than
MIN_SAMPLE contacted leads is flagged "not enough data" rather than rated.

It suggests new point values, but it never writes them: nothing here changes
an offer's rules or rescores a lead. An admin reads the report and edits the
offer in Settings if they agree.

Outcomes come from leads.status, which the app sets from the dashboard and
from imported VICIdial dispositions (YPDNC -> dnc, YPNI -> suppressed). Only
contacted leads count: 'new' leads have not had the chance to say no, and 'dnc'
is a compliance removal, not a sales outcome — the same reading as the
score-vs-rejection analysis on the campaign page.

CLI:  python outcome_report.py [--campaign ID] [--db PATH]
"""
import argparse
import sys

import db
import scoring

MIN_SAMPLE = 30
CONTACTED = ("called", "callback", "interested", "appointment", "not_interested")
POSITIVE = ("interested", "appointment")


def fired_signals(lead, rules):
    """Names of the signals in `rules` that fire for this lead — the same
    decision evaluate() makes, exposed per signal instead of summed."""
    out = set()
    market = scoring._mv(lead, "market_type", "b2b")
    for signal, cfg in rules.items():
        if signal in scoring.B2B_ONLY_SIGNALS and market != "b2b":
            continue
        check = scoring.SIGNALS.get(signal)
        if check is None:
            continue
        try:
            fired = (check(lead, cfg) if signal in scoring.CONFIGURABLE_SIGNALS
                     else check(lead))
        except (KeyError, TypeError, IndexError):
            fired = False
        if fired:
            out.add(signal)
    return out


def _rates(leads):
    """Outcome rates for a group of contacted leads, as fractions."""
    n = len(leads)
    count = lambda *statuses: sum(1 for l in leads if l["status"] in statuses)
    if not n:
        return {"n": 0, "interested": None, "appointment": None,
                "not_interested": None, "positive": None}
    return {"n": n,
            "interested": count("interested") / n,
            "appointment": count("appointment") / n,
            "not_interested": count("not_interested") / n,
            "positive": count(*POSITIVE) / n}


def _round5(x):
    return int(5 * round(x / 5.0))


def suggest_points(points, with_rates, without_rates):
    """A suggested point value, or (None, note) when the data can't support one.

    The rule is deliberately simple and visible: scale the current points by
    how much likelier a positive outcome is WITH the signal than without it,
    rounded to 5 and capped at double the current value (or at 20 for a signal
    currently worth 0), so one lucky week cannot swing a rule wildly. Both
    groups need MIN_SAMPLE leads — a comparison against five leads is noise."""
    if with_rates["n"] < MIN_SAMPLE:
        return None, f"not enough data ({with_rates['n']} of {MIN_SAMPLE} contacted leads)"
    if without_rates["n"] < MIN_SAMPLE:
        return None, (f"not enough data without the signal "
                      f"({without_rates['n']} of {MIN_SAMPLE})")
    pw, po = with_rates["positive"], without_rates["positive"]
    if po == 0 and pw == 0:
        return None, "no positive outcomes in either group yet"
    ratio = (pw / po) if po else 2.0
    if points <= 0:
        suggested = 20 if ratio >= 1.5 else 0
    else:
        suggested = max(0, min(2 * points, _round5(points * ratio)))
    if suggested == points:
        return suggested, "keep"
    direction = "raise" if suggested > points else "lower"
    return suggested, (f"{direction}: positive rate {pw:.0%} with vs {po:.0%} without")


def signal_table(leads, rules):
    """One row per rule signal: counts and rates with/without, lift, suggestion."""
    fired = [(l, fired_signals(l, rules)) for l in leads]
    rows = []
    for signal, cfg in rules.items():
        with_ = [l for l, f in fired if signal in f]
        without = [l for l, f in fired if signal not in f]
        w, wo = _rates(with_), _rates(without)
        points = int(cfg.get("points", 0) or 0)
        suggested, note = suggest_points(points, w, wo)
        lift = (None if w["positive"] is None or wo["positive"] is None
                else w["positive"] - wo["positive"])
        rows.append({"signal": signal, "points": points, "with": w, "without": wo,
                     "lift": lift, "enough_data": w["n"] >= MIN_SAMPLE,
                     "suggested": suggested, "note": note})
    # Signals with data first, biggest lift first; the rest alphabetical.
    rows.sort(key=lambda r: (not r["enough_data"], -(r["lift"] or 0), r["signal"]))
    return rows


def _contacted(conn, campaign_id):
    ph = ",".join("?" * len(CONTACTED))
    if campaign_id is None:
        return conn.execute(
            f"SELECT * FROM leads WHERE campaign_id IS NULL AND status IN ({ph})",
            list(CONTACTED)).fetchall()
    return conn.execute(
        f"SELECT * FROM leads WHERE campaign_id = ? AND status IN ({ph})",
        [campaign_id, *CONTACTED]).fetchall()


def campaign_report(conn, campaign):
    """The report for one campaign row, or for the unassigned pool when None."""
    offer = db.offer_for_campaign(conn, campaign)
    rules = scoring.load_rules(offer)
    leads = _contacted(conn, campaign["id"] if campaign is not None else None)
    totals = _rates(leads)
    return {
        "campaign_id": campaign["id"] if campaign is not None else None,
        "campaign": campaign["name"] if campaign is not None else "Unassigned leads",
        "offer": offer["name"] if offer is not None else "(no offer)",
        "offer_slug": offer["slug"] if offer is not None else "",
        "contacted": totals["n"],
        "totals": totals,
        "signals": signal_table(leads, rules),
    }


def report(conn, campaign_id=None):
    """Reports for every campaign that has leads (plus the unassigned pool), or
    for one campaign when campaign_id is given. Read-only."""
    if campaign_id is not None:
        camp = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        return [campaign_report(conn, camp)] if camp else []
    out = []
    for camp in conn.execute("SELECT * FROM campaigns ORDER BY status = 'active' DESC, name"):
        if conn.execute("SELECT 1 FROM leads WHERE campaign_id = ? LIMIT 1",
                        (camp["id"],)).fetchone():
            out.append(campaign_report(conn, camp))
    if conn.execute("SELECT 1 FROM leads WHERE campaign_id IS NULL LIMIT 1").fetchone():
        out.append(campaign_report(conn, None))
    return out


def _pct(x):
    return "-" if x is None else f"{100 * x:.0f}%"


def format_text(reports):
    lines = ["Call-outcome report (analysis only — no rules were changed)", ""]
    if not reports:
        lines.append("No leads yet.")
    for r in reports:
        t = r["totals"]
        lines.append(f"== {r['campaign']}  [offer: {r['offer']}]")
        lines.append(f"   contacted {t['n']}: interested {_pct(t['interested'])}, "
                     f"appointment {_pct(t['appointment'])}, "
                     f"not interested {_pct(t['not_interested'])}")
        if not r["signals"]:
            lines.append("   (offer has no scoring rules)")
        for s in r["signals"]:
            w, wo = s["with"], s["without"]
            sug = "" if s["suggested"] is None else f" -> suggest {s['suggested']}"
            lines.append(
                f"   {s['signal']:<22} pts {s['points']:>3}{sug:<16} "
                f"with n={w['n']:<4} int {_pct(w['interested']):>4} appt {_pct(w['appointment']):>4} "
                f"no {_pct(w['not_interested']):>4} | "
                f"without n={wo['n']:<4} int {_pct(wo['interested']):>4} "
                f"appt {_pct(wo['appointment']):>4} no {_pct(wo['not_interested']):>4}"
                f"  [{s['note']}]")
        lines.append("")
    lines.append(f"Signals need {MIN_SAMPLE}+ contacted leads before they are rated. "
                 "To act on a suggestion, edit the offer's rules in Settings.")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Per-campaign signal vs call-outcome report. Reads only; "
                    "never changes scoring rules or rescores leads.")
    parser.add_argument("--campaign", type=int, default=None, help="one campaign id")
    parser.add_argument("--db", default=None, help="SQLite path (default: the app's)")
    args = parser.parse_args(argv)
    conn = db.connect(args.db) if args.db else db.connect()
    try:
        print(format_text(report(conn, args.campaign)))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
