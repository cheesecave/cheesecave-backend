"""Run one read-only query for the admin database viewer, with the limits the *database* enforces (prototype).

Layers, from the outside in; only the last two are guarantees, the first is a courtesy that gives a clear message:

1. ``guard_sql``: one statement, a SELECT, no writing node anywhere in it, no function that sleeps, reads files or
   changes state, no credential table or column, no whole-row reference of a table that holds credentials
   (``row_to_json(u)``, ``u::text``). It parses with sqlglot, so it sees the query the way a parser does, not as text.
2. a read-only transaction with a statement timeout and a lock timeout (PostgreSQL), ``query_only`` and an interrupt
   after the timeout (SQLite). The transaction is always rolled back.
3. a row cap taken from a server-side cursor: ``max_rows + 1`` rows are fetched, so ``truncated`` is true when it is.

A dedicated database role with no grant on the credential columns is the real boundary (see the issue); this module
does not need one, and does not pretend that it is one.
"""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import date, datetime, time as dtime
from decimal import Decimal

import sqlglot
from sqlglot import exp

from kohakuhub.api.admin.utils.db_schema import _is_postgres, credential_tables, current_db
from kohakuhub.api.admin.utils.validation import CREDENTIAL_NAMES

DEFAULT_TIMEOUT_MS = 10_000
DISPLAY_ROWS = 1_000
EXPORT_ROWS = 10_000

# nodes that write or change state, wherever they sit in the statement (a data-modifying CTE, a SELECT ... INTO)
_WRITING = tuple(
    getattr(exp, name)
    for name in (
        "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "AlterTable", "Command", "Into", "Lock",
        "Copy", "TruncateTable", "Grant", "Revoke", "Set", "Use", "Pragma", "Transaction", "Commit", "Rollback",
    )
    if hasattr(exp, name)
)
_DENIED_FUNCTIONS = frozenset(
    {
        "pg_sleep", "pg_sleep_for", "pg_sleep_until", "set_config", "nextval", "setval", "pg_terminate_backend",
        "pg_cancel_backend", "pg_reload_conf", "pg_rotate_logfile", "pg_advisory_lock", "pg_advisory_xact_lock",
        "dblink", "dblink_exec", "lo_import", "lo_export", "lo_unlink", "pg_read_file", "pg_read_binary_file",
        "pg_ls_dir", "load_extension", "readfile", "writefile", "randomblob", "pg_stat_file", "current_setting", "ts_stat", "ts_rewrite",
    }
)
# families that run a query given as text, or dump a whole table, and so see what the parser did not
_DENIED_PREFIXES = ("pg_", "lo_", "query_to_", "table_to_", "schema_to_", "database_to_", "cursor_to_", "pg_advisory", "pg_try_advisory", "dblink")


class QueryRefused(Exception):
    """The guard did not let the query through (the message says why)."""


class QueryTimeout(Exception):
    """The database stopped the query at the timeout."""


def guard_sql(sql: str) -> str | None:
    """None when the query may run, else why not."""
    text = sql.strip().rstrip(";").strip()
    if not text:
        return "Query cannot be empty"
    try:
        statements = [s for s in sqlglot.parse(text, read="postgres" if _is_postgres() else "sqlite") if s is not None]
    except sqlglot.errors.SqlglotError as err:
        return f"Not valid SQL: {str(err).splitlines()[0][:160]}"
    if len(statements) != 1:
        return "Only one statement is allowed"
    root = statements[0]
    if isinstance(root, exp.Subquery):
        root = root.this
    if not isinstance(root, (exp.Select, exp.SetOperation)):
        return "Only SELECT queries are allowed (WITH ... SELECT is fine)"
    aliases = {}
    derived = {a.alias.lower() for a in root.find_all(exp.Subquery, exp.CTE) if a.alias}  # subquery and CTE names
    for node in root.walk():
        if isinstance(node, _WRITING):
            return f"{type(node).__name__.upper()} is not allowed in the viewer (it is read-only)"
        if isinstance(node, exp.Func):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if name in _DENIED_FUNCTIONS or name.startswith(_DENIED_PREFIXES):
                return f"The function {name}() is not allowed in the viewer"
        if isinstance(node, exp.Table):
            if node.name.lower().startswith("pg_") or (node.args.get("db") and node.args["db"].name.lower() == "pg_catalog"):
                return "The system catalogs are not available in the viewer (use the schema browser)"
            if node.name.lower() in CREDENTIAL_NAMES:
                return f"'{node.name.lower()}' is not available in the viewer (it holds credentials)"
            key, table = (node.alias_or_name or "").lower(), node.name.lower()
            # one alias for two tables (in different scopes): keep the sensitive one, never the one that hides it
            if aliases.get(key) not in (None, table) and (table in credential_tables() or aliases[key] in credential_tables()):
                table = table if table in credential_tables() else aliases[key]
            aliases[key] = table
        if isinstance(node, exp.Column) and node.name.lower() in CREDENTIAL_NAMES:
            return f"'{node.name.lower()}' is not available in the viewer (it holds credentials)"
    sensitive = credential_tables()
    touches_credentials = any(t in sensitive for t in aliases.values())
    if touches_credentials and any(isinstance(n, exp.SetOperation) for n in root.walk()):
        return "UNION, INTERSECT and EXCEPT are not allowed when the query reads a table that holds credentials"
    if touches_credentials and any(a.args.get("columns") for a in root.find_all(exp.TableAlias)):
        # t(a, b, c) renames the columns, and the redaction of the result goes by the name of the column
        return "Renaming columns in a table alias is not allowed when the query reads a table that holds credentials"
    for node in root.find_all(exp.Column):
        # a whole row of a derived table (row_to_json(s) FROM (SELECT * FROM "user") s) carries the credentials too
        if not node.table and touches_credentials and node.name.lower() in derived:
            return f"'{node.name}' is a whole row of a derived table over a table that holds credentials: name the columns you want"
        # a bare name that is the alias of a table: the whole row (row_to_json(u), u::text), credentials included
        if not node.table and aliases.get(node.name.lower()) in sensitive:
            return (
                f"'{node.name}' is a whole row of '{aliases[node.name.lower()]}', which holds credentials: "
                "name the columns you want"
            )
    return None


def _bind(sql: str, params: dict | None) -> tuple[str, dict | None]:
    """Report SQL names its parameters ``:name`` and may contain a literal percent sign, on both engines."""
    if params is None:
        return sql, None  # no parameters: the driver does not read the percent sign at all
    if _is_postgres():
        sql = re.sub(r"(?<!:):([A-Za-z_]\w*)", r"%(\1)s", sql.replace("%", "%%"))
    return sql, params


def _plain(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value) if value == value.to_integral_value() and abs(value) < 10**15 else str(value)
    return str(value)


def _rows(description, rows) -> tuple[list[str], list[dict]]:
    columns = [d[0] for d in description] if description else []
    out = []
    for row in rows:
        item = {}
        for idx, name in enumerate(columns):
            value = row[idx]
            item[name] = "[redacted]" if value is not None and name.lower() in CREDENTIAL_NAMES else _plain(value)
        out.append(item)
    return columns, out


def run_readonly(
    sql: str,
    params: dict | None = None,
    *,
    max_rows: int = DISPLAY_ROWS,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    explain: bool = False,
    check: bool = True,
) -> dict:
    """Run ``sql``; ``check=False`` skips the guard (a test of the database's own layers)."""
    if check and (why := guard_sql(sql)):
        raise QueryRefused(why)
    started = time.perf_counter()
    bound, values = _bind(sql.strip().rstrip(";"), params)
    conn = current_db().connection()
    if _is_postgres():
        import psycopg2.errors

        was_autocommit = conn.autocommit
        conn.autocommit = False  # the driver runs in autocommit; a read-only transaction and a named cursor need a real one
        conn.rollback()  # SET TRANSACTION must be the first statement of its transaction
        try:
            setup = conn.cursor()
            setup.execute("SET TRANSACTION READ ONLY")
            setup.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            setup.execute("SET LOCAL lock_timeout = 2000")
            if explain:
                cur = conn.cursor()
                cur.execute("EXPLAIN " + bound, values)
                plan = [r[0] for r in cur.fetchall()]
                return {"plan": plan, "elapsed_ms": round((time.perf_counter() - started) * 1000)}
            cur = conn.cursor(name="admin_query")  # a server-side cursor: the rows come when asked for
            cur.execute(bound, values)
            fetched = cur.fetchmany(max_rows + 1)
            columns, rows = _rows(cur.description, fetched[:max_rows])
        except psycopg2.errors.QueryCanceled as err:
            raise QueryTimeout(f"The database stopped the query after {timeout_ms // 1000} s") from err
        finally:
            conn.rollback()
            conn.autocommit = was_autocommit
    else:
        deadline = time.perf_counter() + timeout_ms / 1000
        conn.execute("PRAGMA query_only = ON")
        conn.set_progress_handler(lambda: 1 if time.perf_counter() > deadline else 0, 10_000)
        try:
            if explain:
                cur = conn.execute("EXPLAIN QUERY PLAN " + bound, values or {})
                return {"plan": [str(r[-1]) for r in cur.fetchall()], "elapsed_ms": round((time.perf_counter() - started) * 1000)}
            cur = conn.execute(bound, values or {})
            fetched = cur.fetchmany(max_rows + 1)
            columns, rows = _rows(cur.description, fetched[:max_rows])
        except sqlite3.OperationalError as err:
            if "interrupt" in str(err).lower():
                raise QueryTimeout(f"The database stopped the query after {timeout_ms // 1000} s") from err
            raise
        finally:
            conn.set_progress_handler(None, 0)
            conn.execute("PRAGMA query_only = OFF")
    return {
        "columns": columns,
        "rows": rows,
        "count": len(rows),
        "column_count": len(columns),
        "truncated": len(fetched) > max_rows,
        "row_limit": max_rows,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
    }
