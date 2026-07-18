"""
Read-only reader for the VICIdial MySQL database (the ops/dialer system).

Pulls each day's outbound dispositions from `vicidial_list` — phone_number,
status (disposition code), and called_count (total dials, VICIdial's own count).
Only SELECTs; this module never writes to the dialer DB.

Connection comes from env vars set in Railway (never in code/DB/chat):
    OPS_DB_HOST, OPS_DB_PORT, OPS_DB_NAME, OPS_DB_USER, OPS_DB_PASSWORD
"""

import os

# Not-reached -> requeue for redial (capped by called_count).
RETRY_CODES = ("YPVM", "YPCBCK", "YPNA", "INCALL", "DROP")
# Reached and declined -> time-boxed suppression, not requeue.
SUPPRESS_CODES = ("YPNI",)
ALL_CODES = RETRY_CODES + SUPPRESS_CODES


def _env(name, default=""):
    """Read OPS_DB_* first, then plain DB_* — so the ops service's variables can
    be copied in verbatim without renaming."""
    return os.environ.get("OPS_" + name) or os.environ.get(name) or default


def enabled():
    """True when the VICIdial DB connection is configured."""
    return bool(_env("DB_HOST") and _env("DB_USER"))


def _connect():
    import pymysql
    from pymysql.cursors import DictCursor
    return pymysql.connect(
        host=_env("DB_HOST"),
        port=int(_env("DB_PORT", "3306")),
        user=_env("DB_USER"),
        password=_env("DB_PASSWORD"),
        database=_env("DB_NAME"),
        cursorclass=DictCursor,
        connect_timeout=15,
        read_timeout=45,
    )


def fetch_dispositions(day, campaign=None):
    """day: 'YYYY-MM-DD' (EST). Optional campaign = VICIdial campaign_id.
    Returns [{phone, status, called_count, campaign}, ...] for leads whose latest
    disposition that day is a retry/suppress code. Read-only."""
    placeholders = ",".join(["%s"] * len(ALL_CODES))
    sql = (
        "SELECT vl.phone_number AS phone, vl.status, vl.called_count, "
        "       vls.campaign_id AS campaign "
        "FROM vicidial_list vl "
        "JOIN vicidial_lists vls ON vl.list_id = vls.list_id "
        f"WHERE vl.status IN ({placeholders}) AND DATE(vl.last_local_call_time) = %s"
    )
    params = [*ALL_CODES, day]
    if campaign:
        sql += " AND vls.campaign_id = %s"
        params.append(campaign)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())
    finally:
        conn.close()


def list_campaigns():
    """Active VICIdial campaigns for the filter dropdown. Best-effort; returns []
    if unreachable so the page still loads."""
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT campaign_id, campaign_name FROM vicidial_campaigns "
                    "WHERE active = 'Y' ORDER BY campaign_id"
                )
                return list(cur.fetchall())
        finally:
            conn.close()
    except Exception:
        return []
