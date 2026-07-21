"""
Import dialer call-log CSVs and update lead outcomes by phone number.

Expected shape (column order doesn't matter, extra columns are ignored):
    - a phone column: any header containing "phone" (e.g. phone_number_dialed)
    - an outcome column: header "status_name" if present, otherwise the last column

Outcome handling per phone number:
    - "DO NOT CALL" anywhere in the file wins -> lead becomes dnc and the number
      is blocked from all future pulls (inserted as a blocked row if unknown)
    - otherwise the last mappable row wins (VOICEMAIL/NO ANSWER -> called, etc.)
    - manual statuses you set in the UI (interested / callback / not_interested)
      are never downgraded by an import; only dnc overrides them
"""

import csv
import io
import re
from datetime import date

import db

DNC_VALUES = {"DO NOT CALL", "DNC"}

OUTCOME_MAP = {
    "VOICEMAIL": "called",
    "NO ANSWER": "called",
    "ANSWERED": "called",
    "BUSY": "called",
    "WRONG DEPARTMENT": "called",
    "WRONG NUMBER": "called",
    "NOT INTERESTED": "not_interested",
    "INTERESTED": "interested",
    "SALE": "interested",
    "CALL BACK": "callback",
    "CALLBACK": "callback",
}


def normalize_phone(raw):
    """Reduce any phone representation to 10 digits, or None if impossible
    (handles Excel-mangled values like '4.42035E+11' by rejecting them)."""
    raw = str(raw or "").strip()
    if "e+" in raw.lower() or "e-" in raw.lower():
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def format_phone(d10):
    """Match the +1 aaa-bbb-cccc style Outscraper leads are stored with."""
    return f"+1 {d10[:3]}-{d10[3:6]}-{d10[6:]}"


def parse_log(text):
    """Return ({phone10: status}, skipped_row_count)."""
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    phone_col = next((h for h in headers if h and "phone" in h.lower()), None)
    outcome_col = next((h for h in headers if h and h.lower() == "status_name"),
                       headers[-1] if headers else None)
    if not phone_col or not outcome_col:
        raise ValueError(
            "Could not find a phone column and an outcome column. "
            f"Headers found: {', '.join(h or '?' for h in headers)}"
        )

    outcomes = {}
    dnc_phones = set()
    skipped = 0
    for row in reader:
        phone = normalize_phone(row.get(phone_col))
        if not phone:
            skipped += 1
            continue
        value = (row.get(outcome_col) or "").strip().upper()
        if value in DNC_VALUES:
            dnc_phones.add(phone)
        elif value in OUTCOME_MAP:
            outcomes[phone] = OUTCOME_MAP[value]  # last mappable row wins
        else:
            skipped += 1  # AFTRS / DROPPED / inbound rows etc.

    for phone in dnc_phones:
        outcomes[phone] = "dnc"
    return outcomes, skipped


def apply_outcomes(conn, outcomes):
    """Write parsed outcomes to the leads table. Returns a summary dict."""
    # A phone can now map to several leads (one per campaign, since dedupe is
    # per-campaign). The call log carries no campaign, so an outcome applies to
    # every lead row sharing the number.
    existing = {}
    for row in conn.execute("SELECT id, phone, status FROM leads"):
        norm = normalize_phone(row["phone"])
        if norm:
            existing.setdefault(norm, []).append((row["id"], row["status"]))

    summary = {"updated": 0, "kept": 0, "dnc_blocked": 0, "unmatched": 0}
    now = db.now_iso()

    # Every DO-NOT-CALL number from the log goes onto the permanent suppression
    # list, so it stays blocked even if it turns up in a future pull/import.
    import dnc
    dnc.add_numbers(conn, [p for p, s in outcomes.items() if s == "dnc"],
                    source="call_log", reason="DO NOT CALL from dialer log")

    for phone, status in outcomes.items():
        if phone in existing:
            touched = False
            for lead_id, current in existing[phone]:
                # dnc always wins; other outcomes only fill in new/called leads
                if status == "dnc":
                    overwrite = current != "dnc"
                else:
                    overwrite = current in ("new", "called") and current != status
                if overwrite:
                    conn.execute(
                        "UPDATE leads SET status = ?, status_updated_at = ? WHERE id = ?",
                        (status, now, lead_id),
                    )
                    touched = True
            summary["updated" if touched else "kept"] += 1
        elif status == "dnc":
            # Unknown number that asked not to be called: block it forever.
            conn.execute(
                "INSERT OR IGNORE INTO leads (phone, business_name, pulled_date, "
                "status, status_updated_at) VALUES (?, ?, ?, 'dnc', ?)",
                (format_phone(phone), "(DNC — imported from call log)",
                 str(date.today()), now),
            )
            summary["dnc_blocked"] += 1
        else:
            summary["unmatched"] += 1

    conn.commit()
    return summary
