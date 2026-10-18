"""The server-side SQL sandbox: what a query can do, and every door it must not be able to open.

The worker needs DuckDB, which the application image does not carry; the tests run it with SANDBOX_PYTHON (a venv that has it)
and skip when none is configured.
"""
import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from kohakuhub.api.viewer import sandbox

PY = os.environ.get("SANDBOX_PYTHON")
pytestmark = pytest.mark.skipif(not PY, reason="set SANDBOX_PYTHON to a Python with duckdb")


@pytest.fixture
def tables(tmp_path):
    path = tmp_path / "t.parquet"
    pq.write_table(pa.table({"id": list(range(1000)), "label": [i % 5 for i in range(1000)]}), path)
    secret = tmp_path / "secret.csv"
    secret.write_text("password\nhunter2\n")
    return {"t": str(path), "secret": str(secret)}


def run(sql, tables, **kw):
    return sandbox.run(sql, {"t": tables["t"]}, python=PY, **kw)


def test_a_query_over_the_staged_file_works(tables):
    out = run("SELECT label, count(*) AS n FROM t GROUP BY label ORDER BY label", tables)
    assert out["columns"] == ["label", "n"] and out["rows"] == [[i, 200] for i in range(5)] and out["truncated"] is False


def test_the_row_cap_truncates_and_says_so(tables):
    out = run("SELECT * FROM t", tables, max_rows=10)
    assert len(out["rows"]) == 10 and out["truncated"] is True


def test_a_sql_error_is_reported_as_one(tables):
    with pytest.raises(sandbox.Sandboxed) as e:
        run("SELECT nope FROM t", tables)
    assert e.value.kind == "sql"


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_csv('{secret}')",                                  # another local file
    "SELECT * FROM read_parquet('/etc/passwd')",
    "SELECT * FROM read_parquet('http://127.0.0.1:1/x.parquet')",           # the network
    "SELECT * FROM read_text('/etc/hostname')",
    "COPY t TO '{out}/leak.csv'",                                          # writing a file
    "COPY (SELECT 1) TO '/tmp/sandbox-should-not-exist.csv'",
    "ATTACH '/tmp/sandbox-should-not-exist.db' AS x",
    "INSTALL httpfs",
    "LOAD httpfs",
    "SET enable_external_access=true",                                     # undoing the sandbox
    "SET allowed_paths=['/etc/passwd']",
    "SET memory_limit='100GB'",
    "SET lock_configuration=false",
    "PRAGMA enable_external_access",
    "SELECT * FROM glob('/*')",
])
def test_every_door_is_closed(tables, tmp_path, sql):
    with pytest.raises(sandbox.Sandboxed) as e:
        run(sql.format(secret=tables["secret"], out=tmp_path), tables)
    assert e.value.kind in ("blocked", "sql"), str(e.value)
    assert not (tmp_path / "leak.csv").exists() and not os.path.exists("/tmp/sandbox-should-not-exist.csv") and not os.path.exists("/tmp/sandbox-should-not-exist.db")


def test_the_worker_has_no_environment_to_leak(tables, monkeypatch):
    monkeypatch.setenv("SANDBOX_SECRET_PROBE", "leaked")
    with pytest.raises(sandbox.Sandboxed):
        run("SELECT getenv('SANDBOX_SECRET_PROBE')", tables)      # no such function when external access is off, and the env is empty anyway


def test_a_runaway_query_is_killed_at_the_timeout(tables):
    with pytest.raises(sandbox.Sandboxed) as e:
        run("SELECT count(*) FROM range(10000000000) a", tables, timeout=2)
    assert e.value.kind == "timeout"


def test_a_query_that_wants_more_memory_than_allowed_fails_cleanly(tables):
    with pytest.raises(sandbox.Sandboxed) as e:
        run("SELECT list(range(2000000000))", tables, memory="64MB", address_space=3 << 30, timeout=60)
    assert e.value.kind in ("sql", "memory", "failed")     # ended by the limit, not by the 60 s timeout
