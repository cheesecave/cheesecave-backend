"""The admin database viewer's schema, guard, executor and reports (prototype)."""

import pytest

from kohakuhub.api.admin.utils import db_exec, db_reports, db_schema
from kohakuhub.api.admin.utils.db_exec import QueryRefused, QueryTimeout, guard_sql, run_readonly


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM repository",
        "SELECT 1; SELECT 2",
        "WITH x AS (DELETE FROM repository RETURNING id) SELECT * FROM x",
        "SELECT * INTO copy_of_users FROM repository",
        "SELECT pg_sleep(100)",
        "SELECT set_config('x', 'y', false)",
        "SELECT lo_import('/etc/passwd')",
        "SELECT password_hash FROM \"user\"",
        "SELECT * FROM session",
        "SELECT row_to_json(u) FROM \"user\" u",
        "SELECT u::text FROM \"user\" u",
        "SELECT query_to_xml('select * from \"user\"', true, false, '')",
        "SELECT table_to_xml('user', true, false, '')",
        "SELECT current_setting('server_version')",
        "SELECT row_to_json(s) FROM (SELECT * FROM \"user\") s",
        "WITH c AS (SELECT * FROM \"user\") SELECT row_to_json(c) FROM c",
        "SELECT * FROM \"user\" AS u(a, b, c)",
        "SELECT * FROM pg_authid",
        "SELECT * FROM pg_catalog.pg_shadow",
        "SELECT pg_try_advisory_lock(1)",
        "SELECT pg_stat_get_activity(NULL)",
        "SELECT row_to_json(x) FROM (SELECT 1 FROM repository x) y, \"user\" x",
        "SELECT ts_stat('select 1')",
        "SELECT id FROM \"user\" UNION ALL SELECT id FROM repository",
        "SELECT * FROM",
        "   ",
    ],
)
def test_guard_refuses(sql):
    assert guard_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select username from \"user\" where username like '%.%' limit 5;",
        "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
        "SELECT 1 UNION ALL SELECT 2",
    ],
)
def test_guard_allows(sql):
    assert guard_sql(sql) is None


def test_every_table_is_in_a_group_and_has_a_model(db_dual):
    schema = db_schema.build_schema()
    assert schema["drift"]["tables_without_group"] == []
    assert schema["drift"]["tables_without_model"] == []
    assert schema["drift"]["models_without_table"] == []
    user = next(t for t in schema["tables"] if t["name"] == "user")
    assert user["group"] == "Accounts"
    assert any(c["name"] == "password_hash" and c["kind"] == "credential" for c in user["columns"])
    assert any(c["name"] == "email" and c["kind"] == "pii" for c in user["columns"])


def test_a_table_nobody_wrote_down_shows_as_drift(db_dual):
    schema = db_schema._schema() if db_schema._is_postgres() else "main"
    db_dual.execute_sql(f'CREATE TABLE "{schema}".brand_new_thing (id INTEGER PRIMARY KEY)')
    try:
        drift = db_schema.build_schema()["drift"]
    finally:  # the fixture only drops the tables of the models
        db_dual.execute_sql(f'DROP TABLE "{schema}".brand_new_thing')
    assert drift["tables_without_model"] == ["brand_new_thing"]
    assert drift["tables_without_group"] == ["brand_new_thing"]


def test_percent_sign_and_parameters_run_on_both_engines(db_dual):
    out = run_readonly("SELECT 'a.b' LIKE '%.%' AS has_dot")
    assert out["rows"][0]["has_dot"] in (True, 1)
    out = run_readonly("SELECT :n AS n, 'x' LIKE '%x' AS m", {"n": 7})
    assert out["rows"][0]["n"] == 7


def test_truncated_is_true_only_when_there_were_more_rows(db_dual):
    sql = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < {}) SELECT i FROM n"
    assert run_readonly(sql.format(5), max_rows=5)["truncated"] is False
    out = run_readonly(sql.format(6), max_rows=5)
    assert (out["count"], out["truncated"]) == (5, True)


def test_writes_fail_in_the_database_itself_even_without_the_guard(db_dual):
    # a statement that is valid inside a cursor but writes: a sequence step on PostgreSQL, an INSERT on SQLite
    write = (
        "SELECT nextval(pg_get_serial_sequence('\"user\"', 'id'))"
        if db_schema._is_postgres()
        else "INSERT INTO \"user\" (username, normalized_name) VALUES ('x', 'x')"
    )
    with pytest.raises(Exception, match="(?i)read-only|readonly"):
        run_readonly(write, params=None, check=False)


def test_a_slow_query_is_stopped_by_the_database(db_dual):
    slow = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 100000000) SELECT count(*) FROM n"
    with pytest.raises(QueryTimeout):
        run_readonly(slow, timeout_ms=300)


def test_explain_returns_a_plan(db_dual):
    assert run_readonly('SELECT * FROM "user"', explain=True)["plan"]


def test_credential_value_is_redacted_even_when_renamed_out_of_sight(db_dual):
    assert db_exec._rows([("token",)], [("secret",)])[1] == [{"token": "[redacted]"}]


def _reports():
    return [r for r in db_reports.load_reports()]


def test_every_report_has_a_known_category_and_unique_id():
    ids = [r["id"] for r in _reports()]
    assert len(ids) == len(set(ids)) >= 20
    assert all(r["category"] in db_reports.CATEGORIES for r in _reports())


def test_every_report_runs_on_every_engine_it_names(db_dual):
    engine = "postgresql" if db_schema._is_postgres() else "sqlite"
    for report in _reports():
        if engine not in report["dialects"]:
            continue
        run_readonly(report["sql"], db_reports.bind_parameters(report, {}), check=False)  # reports are trusted, as in the router


@pytest.mark.parametrize(
    ("given", "message"),
    [({"days": 0}, "between"), ({"days": "x"}, "whole"), ({"nope": 1}, "Unknown")],
)
def test_report_parameters_are_checked(given, message):
    report = db_reports.get_report("recent-users")
    with pytest.raises(db_reports.ReportError, match=message):
        db_reports.bind_parameters(report, given)
