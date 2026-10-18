"""API tests for admin database viewer routes."""

from test.kohakuhub.support.db import postgres_configured


async def test_admin_database_routes_list_tables_templates_and_execute_queries(admin_client):
    tables_response = await admin_client.get("/admin/api/database/tables")
    assert tables_response.status_code == 200
    tables = tables_response.json()["tables"]
    table_names = {table["name"] for table in tables}
    assert "user" in table_names
    assert "repository" in table_names
    assert any(column["name"] == "username" for table in tables if table["name"] == "user" for column in table["columns"])

    templates_response = await admin_client.get("/admin/api/database/templates")
    assert templates_response.status_code == 200
    assert templates_response.json()["templates"]

    query_response = await admin_client.post(
        "/admin/api/database/query",
        json={
            "sql": 'SELECT username, is_org FROM "user" ORDER BY username LIMIT 3;'
        },
    )
    assert query_response.status_code == 200
    payload = query_response.json()
    assert payload["columns"] == ["username", "is_org"]
    assert payload["count"] >= 1
    assert isinstance(payload["rows"][0]["username"], str)
    # PostgreSQL returns the boolean and the console shows "True"/"False"; SQLite returns 0/1
    assert payload["rows"][0]["is_org"] in ({"True", "False"} if postgres_configured() else {0, 1})


async def test_admin_database_query_rejects_invalid_or_failing_sql(admin_client):
    invalid_query = await admin_client.post(
        "/admin/api/database/query",
        json={"sql": "DELETE FROM repository"},
    )
    assert invalid_query.status_code == 400
    assert "Invalid query" in invalid_query.json()["detail"]["error"]

    failing_query = await admin_client.post(
        "/admin/api/database/query",
        json={"sql": "SELECT * FROM missing_table"},
    )
    assert failing_query.status_code == 400
    assert "Query execution failed" in failing_query.json()["detail"]["error"]


async def test_credential_tables_and_columns_are_not_readable_through_the_viewer(admin_client):
    for sql in (
        'SELECT password_hash FROM "user"',
        'SELECT password_hash AS harmless FROM "user"',
        "SELECT secret FROM session",
        'SELECT * FROM "session"',
        "SELECT token_hash FROM token",
        'SELECT * FROM "token"',
        "SELECT encrypted_token FROM userexternaltoken",
        "SELECT token FROM emailverification",
        "SELECT session_id FROM session",
        "SELECT lease_token FROM repository_metadata",
        "SELECT 1 WHERE EXISTS (SELECT 1 FROM Session)",
    ):
        response = await admin_client.post("/admin/api/database/query", json={"sql": sql})
        assert response.status_code == 400, sql
        assert "not available in the viewer" in response.json()["detail"]["error"], sql


async def test_select_star_does_not_return_credential_columns_in_clear(admin_client):
    response = await admin_client.post(
        "/admin/api/database/query",
        json={"sql": 'SELECT * FROM "user" ORDER BY id LIMIT 5'},
    )
    assert response.status_code == 200
    payload = response.json()
    assert "password_hash" in payload["columns"]  # the column is there, its values are not
    hashes = {row["password_hash"] for row in payload["rows"]}
    assert hashes <= {"[redacted]", None}
    assert any(row["password_hash"] == "[redacted]" for row in payload["rows"])
    assert any(row["username"] for row in payload["rows"])  # the rest is untouched


async def test_ordinary_names_that_only_look_similar_are_still_fine(admin_client):
    for sql in (
        'SELECT username FROM "user" LIMIT 1',
        "SELECT count(*) AS sessions_total FROM repository",
        "SELECT 'a token of appreciation' AS note",
    ):
        response = await admin_client.post("/admin/api/database/query", json={"sql": sql})
        assert response.status_code == 200, (sql, response.text)


async def test_the_shipped_query_templates_are_not_refused_by_the_credential_rule(admin_client):
    # Some templates are written for SQLite and fail on PostgreSQL; that is not what this checks. They must pass validation.
    import sys

    validate = sys.modules["kohakuhub.api.admin.utils.validation"].validate_readonly_sql
    templates = (await admin_client.get("/admin/api/database/templates")).json()["templates"]
    assert templates
    for template in templates:
        assert validate(template["sql"]) == (True, None), template["name"]
