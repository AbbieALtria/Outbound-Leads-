"""Postgres backend presenting the sqlite3 API the rest of the app already uses.

The app talks to its database through a small, consistent slice of the DB-API:
``conn.execute(sql, params)`` returning a cursor, rows read by column name,
``cur.rowcount``, ``cur.lastrowid`` and ``conn.commit()``. Rather than rewrite
~230 call sites, this module makes Postgres answer to that same slice and
translates the handful of statements SQLite spells differently.

Selected by DATABASE_URL. With it unset the app stays on SQLite, so local runs
and tests are unaffected and removing the variable is a complete rollback.

Why this exists at all: Railway mounts the service's volume into a namespace the
web process cannot see, so a file-backed database is discarded on every deploy.
Postgres is reached over the network and needs no volume, which sidesteps the
platform fault entirely rather than waiting on it.
"""

import re

import psycopg
from psycopg.rows import dict_row

# Statement-level rewrites. SQLite's INSERT OR IGNORE is Postgres's ON CONFLICT
# DO NOTHING; everything else the app writes is already valid in both.
_OR_IGNORE = re.compile(r"^(\s*)INSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE)

# Type spellings that differ in DDL. SQLite's "INTEGER PRIMARY KEY" is an alias
# for the auto-assigning rowid; Postgres needs an explicit sequence.
_DDL_SUBS = (
    (re.compile(r"\bINTEGER\s+PRIMARY\s+KEY\b", re.IGNORECASE), "SERIAL PRIMARY KEY"),
    (re.compile(r"\bREAL\b", re.IGNORECASE), "DOUBLE PRECISION"),
)


def _is_ddl(sql):
    return bool(re.match(r"\s*CREATE\s+TABLE\b", sql, re.IGNORECASE))


def translate(sql):
    """Rewrite one SQLite statement for Postgres.

    Placeholder conversion is quote-aware: a '?' inside a string literal is data,
    not a parameter. Every literal '%' has to be doubled either way, because
    psycopg scans the whole statement for its own %s markers and would otherwise
    read `LIKE '%reviews%'` as a malformed placeholder.
    """
    sql = _OR_IGNORE.sub(r"\1INSERT INTO", sql, count=1)
    if _is_ddl(sql):
        for pattern, repl in _DDL_SUBS:
            sql = pattern.sub(repl, sql)

    out, in_string = [], False
    for ch in sql:
        if ch == "'":
            in_string = not in_string
            out.append(ch)
        elif ch == "%":
            out.append("%%")
        elif ch == "?" and not in_string:
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)


def split_statements(script):
    """Split a SQL script on statement boundaries.

    Naive splitting on ';' breaks on the schema this app actually has: it holds
    line comments like "-- 10-digit, normalized", and a semicolon inside a
    comment or a string literal is not a boundary.
    """
    out, buf = [], []
    in_string = in_line_comment = in_block_comment = False
    i, n = 0, len(script)
    while i < n:
        ch, nxt = script[i], script[i + 1] if i + 1 < n else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                buf.append(ch)
        elif in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 1
        elif in_string:
            buf.append(ch)
            if ch == "'":
                in_string = False
        elif ch == "-" and nxt == "-":
            in_line_comment = True
            i += 1
        elif ch == "/" and nxt == "*":
            in_block_comment = True
            i += 1
        elif ch == "'":
            in_string = True
            buf.append(ch)
        elif ch == ";":
            statement = "".join(buf).strip()
            if statement:
                out.append(statement)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def prepare(sql):
    """Full statement rewrite, including the ON CONFLICT tail for INSERT OR IGNORE."""
    had_or_ignore = bool(_OR_IGNORE.match(sql))
    sql = translate(sql)
    if had_or_ignore:
        # Appended rather than spelled per-table: with no conflict target
        # Postgres applies it to whichever unique constraint is violated, which
        # is exactly what OR IGNORE meant.
        sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return sql


class Cursor:
    """The parts of a sqlite3 cursor this app uses, backed by psycopg."""

    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def description(self):
        return self._cur.description

    @property
    def lastrowid(self):
        """Id of the row just inserted.

        lastval() reads the sequence this session last advanced, which avoids
        appending RETURNING to every INSERT -- several tables here key on a text
        column and have no id at all. Callers already guard on rowcount, so it
        is never read after an insert that did nothing.
        """
        try:
            with self._cur.connection.cursor() as c:
                c.execute("SELECT lastval()")
                row = c.fetchone()
                if not row:
                    return None
                return list(row.values())[0] if isinstance(row, dict) else row[0]
        except psycopg.Error:
            return None

    def close(self):
        self._cur.close()


class Connection:
    """sqlite3-shaped wrapper over a psycopg connection."""

    def __init__(self, dsn):
        # autocommit mirrors how this app actually behaves -- it commits
        # promptly and never rolls back -- and stops read-only paths that never
        # call commit() from holding a transaction open.
        self._conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)

    def execute(self, sql, params=()):
        cur = self._conn.cursor()
        cur.execute(prepare(sql), tuple(params))
        return Cursor(cur)

    def executemany(self, sql, seq):
        cur = self._conn.cursor()
        cur.executemany(prepare(sql), [tuple(p) for p in seq])
        return Cursor(cur)

    def executescript(self, script):
        """Run a multi-statement script, as sqlite3 does.

        psycopg will not take several statements with one execute(), so the
        script is split and each statement translated on its own — which also
        gives each CREATE TABLE its own type rewriting.
        """
        for statement in split_statements(script):
            self.execute(statement)
        return self

    def commit(self):
        if not self._conn.autocommit:
            self._conn.commit()

    def rollback(self):
        if not self._conn.autocommit:
            self._conn.rollback()

    def close(self):
        try:
            self._conn.close()
        except psycopg.Error:
            pass

    @property
    def closed(self):
        return self._conn.closed


def connect(dsn):
    return Connection(dsn)


def table_columns(conn, table):
    """Column names of `table` -- the Postgres answer to PRAGMA table_info."""
    return {r["column_name"] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ?", (table,))}


def table_names(conn):
    return {r["table_name"] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema()")}


# Raised on a uniqueness violation; users.py catches the sqlite3 spelling.
IntegrityError = psycopg.errors.IntegrityError
Error = psycopg.Error


def selftest(conn):
    """Exercise the translated behaviours against the live database.

    The translation layer can be unit-tested without a server, but whether
    Postgres then *behaves* like the sqlite3 API the app expects — dedupe that
    reports rowcount 0, a usable lastrowid, rows addressed by column name — can
    only be checked against a real one. This runs at startup and prints a line
    per behaviour, so a broken assumption shows up in the deploy log instead of
    as a mangled pull hours later.

    Returns a list of (name, ok, detail).
    """
    results, t = [], "_selftest_leads"

    def record(name, fn):
        try:
            fn()
            results.append((name, True, ""))
        except Exception as e:                    # noqa: BLE001 - reported, not raised
            results.append((name, False, f"{type(e).__name__}: {e}"))

    try:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.execute(f"CREATE TABLE IF NOT EXISTS {t} ("
                     "id INTEGER PRIMARY KEY, phone TEXT UNIQUE, hook TEXT, n INTEGER)")
    except Exception as e:                        # noqa: BLE001
        # A diagnostic must never be the reason the app won't boot.
        return [("selftest setup", False, f"{type(e).__name__}: {e}")]

    state = {}

    def insert_returns_id():
        cur = conn.execute(f"INSERT OR IGNORE INTO {t} (phone, hook, n) VALUES (?, ?, ?)",
                           ("15550001", "100% uptime", 1))
        assert cur.rowcount == 1, f"rowcount {cur.rowcount}"
        state["id"] = cur.lastrowid
        assert isinstance(state["id"], int) and state["id"] > 0, f"lastrowid {state['id']!r}"

    def dedupe_is_silent():
        cur = conn.execute(f"INSERT OR IGNORE INTO {t} (phone, hook, n) VALUES (?, ?, ?)",
                           ("15550001", "dup", 2))
        assert cur.rowcount == 0, f"duplicate inserted (rowcount {cur.rowcount})"

    def rows_read_by_name():
        row = conn.execute(f"SELECT id, phone, n FROM {t} WHERE id = ?",
                           (state["id"],)).fetchone()
        assert row["phone"] == "15550001", f"phone {row['phone']!r}"
        assert "n" in row.keys(), "keys() missing column"
        assert dict(row)["id"] == state["id"], "dict(row) lost the id"

    def literal_percent_survives():
        row = conn.execute(
            f"SELECT COUNT(*) AS c FROM {t} WHERE hook LIKE '%uptime%'").fetchone()
        assert row["c"] == 1, f"LIKE matched {row['c']} rows"

    def update_reports_rowcount():
        cur = conn.execute(f"UPDATE {t} SET n = ? WHERE phone = ?", (9, "15550001"))
        assert cur.rowcount == 1, f"rowcount {cur.rowcount}"

    def executemany_works():
        conn.executemany(f"UPDATE {t} SET n = ? WHERE id = ?", [(7, state["id"])])
        assert conn.execute(f"SELECT n FROM {t} WHERE id = ?",
                            (state["id"],)).fetchone()["n"] == 7

    def missing_column_is_detectable():
        assert "hook" in table_columns(conn, t), "table_columns lost a column"
        assert t in table_names(conn), "table_names lost the table"

    for name, fn in (("insert returns an id", insert_returns_id),
                     ("duplicate is ignored", dedupe_is_silent),
                     ("rows read by column name", rows_read_by_name),
                     ("literal % in LIKE", literal_percent_survives),
                     ("update reports rowcount", update_reports_rowcount),
                     ("executemany", executemany_works),
                     ("schema introspection", missing_column_is_detectable)):
        record(name, fn)

    try:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.commit()
    except Exception as e:                        # noqa: BLE001
        results.append(("selftest cleanup", False, f"{type(e).__name__}: {e}"))
    return results
