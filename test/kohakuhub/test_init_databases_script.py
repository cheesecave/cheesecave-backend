"""docker/lakefs/init-databases.sh reads the database URL under either prefix.

The script runs in the LakeFS image, which has a shell and psql but no Python, so it cannot
use ``kohakuhub.envnames``; it spells the same rule in POSIX sh: CHEESE_CAVE_DATABASE_URL
first, KOHAKU_HUB_DATABASE_URL as the fallback, an empty value counting as set. The tests run
it with a fake ``psql`` that records how it was called.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "docker" / "lakefs" / "init-databases.sh"

FAKE_PSQL = """#!/bin/sh
echo "$PGPASSWORD|$*" >> "$PSQL_LOG"
# 'SELECT 1 FROM pg_database' answers 1 (the database exists), so nothing is created
case "$*" in *pg_database*) echo 1;; esac
exit 0
"""


@pytest.fixture
def run_script(tmp_path):
    if shutil.which("sh") is None:  # pragma: no cover - every CI image has one
        pytest.skip("needs a POSIX sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    psql = bin_dir / "psql"
    psql.write_text(FAKE_PSQL)
    psql.chmod(0o755)
    log = tmp_path / "psql.log"

    def run(**env):
        clean = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("CHEESE_CAVE_", "KOHAKU_HUB_", "POSTGRES_"))
        }
        clean.update(env, PATH=f"{bin_dir}:{os.environ['PATH']}", PSQL_LOG=str(log))
        result = subprocess.run(
            ["sh", str(SCRIPT)], env=clean, capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stderr
        calls = log.read_text().splitlines() if log.exists() else []
        return result.stdout, calls

    return run


def test_the_database_url_is_read_from_the_old_prefix(run_script):
    out, calls = run_script(KOHAKU_HUB_DATABASE_URL="postgresql://olduser:oldpw@oldhost:5440/olddb")

    assert "PostgreSQL Server: oldhost:5440" in out
    assert "Hub-API Database: olddb" in out
    assert calls[0].startswith("oldpw|-h oldhost -p 5440 -U olduser")


def test_the_database_url_is_read_from_the_new_prefix(run_script):
    out, calls = run_script(CHEESE_CAVE_DATABASE_URL="postgresql://newuser:newpw@newhost:5441/newdb")

    assert "PostgreSQL Server: newhost:5441" in out
    assert "Hub-API Database: newdb" in out
    assert calls[0].startswith("newpw|-h newhost -p 5441 -U newuser")


def test_the_new_prefix_wins_when_both_are_set(run_script):
    out, calls = run_script(
        KOHAKU_HUB_DATABASE_URL="postgresql://olduser:oldpw@oldhost:5440/olddb",
        CHEESE_CAVE_DATABASE_URL="postgresql://newuser:newpw@newhost:5441/newdb",
    )

    assert "PostgreSQL Server: newhost:5441" in out
    assert all("oldhost" not in call for call in calls)


def test_an_empty_new_value_still_counts_as_set(run_script):
    # The same rule as the application: an empty CHEESE_CAVE_ value is set, so the old URL is
    # not consulted and the script falls back to its separate POSTGRES_* defaults.
    out, _ = run_script(
        KOHAKU_HUB_DATABASE_URL="postgresql://olduser:oldpw@oldhost:5440/olddb",
        CHEESE_CAVE_DATABASE_URL="",
    )

    assert "PostgreSQL Server: postgres:5432" in out


def test_without_either_the_postgres_defaults_are_used(run_script):
    out, calls = run_script()

    assert "PostgreSQL Server: postgres:5432" in out
    assert calls[0].startswith("hubpass|-h postgres -p 5432 -U hub")
