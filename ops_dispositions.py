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


def enabled():
    """True when the VICIdial DB connection is configured."""
    return bool(os.environ.get("OPS_DB_HOST") and os.environ.get("OPS_DB_USER"))


def _connect():
    import pymysql
    from pymysql.cursors import DictCursor
    return pymysql.connect(
        host=os.environ["OPS_DB_HOST"],
        port=int(os.environ.get("OPS_DB_PORT", "3306")),
        user=os.environ["OPS_DB_USER"],
        password=os.environ.get("OPS_DB_PASSWORD", ""),
        database=os.environ.get("OPS_DB_NAME", ""),
        cursorclass=DictCursor,
        connect_timeout=15,
        read_timeout=45,
    )


def fetch_dispositions(day):
    """day: 'YYYY-MM-DD' (EST). Returns [{phone, status, called_count}, ...] for
    leads whose latest disposition that day is a retry/suppress code. Read-only."""
    placeholders = ",".join(["%s"] * len(ALL_CODES))
    sql = (
        "SELECT phone_number AS phone, status, called_count "
        "FROM vicidial_list "
        f"WHERE status IN ({placeholders}) AND DATE(last_local_call_time) = %s"
    )
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (*ALL_CODES, day))
            return list(cur.fetchall())
    finally:
        conn.close()
