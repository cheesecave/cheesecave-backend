"""The migration runner never prints a credential (issue #11 rollout rehearsal, D6).

The runner runs as a subprocess, as operators run it. Its database is an address
nothing listens on, so every path that prints (the header, the connection
warnings, the crash report and its traceback) runs without a live database.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "run_migrations.py"
SECRET = "rehearsal-password-D6"


def _run_with_url(url: str):
    env = os.environ.copy()
    env.update(KOHAKU_HUB_DB_BACKEND="postgres", KOHAKU_HUB_DATABASE_URL=url)
    result = subprocess.run(
        [sys.executable, str(RUNNER)], env=env, capture_output=True, text=True, timeout=120
    )
    return result, result.stdout + result.stderr


def test_runner_output_never_contains_the_password():
    url = f"postgresql://runner:{SECRET}@127.0.0.1:1/none"
    result, output = _run_with_url(url)
    assert result.returncode == 1, output
    assert SECRET not in output
    assert "Database URL: postgresql://runner:***@127.0.0.1:1/none" in result.stdout
    assert "Migration runner crashed" in output  # the failure path printed, and redacted


def test_runner_output_is_redacted_in_every_stream_and_line():
    """A password in an exception text is masked too (the stream filter), not only the
    header line the runner builds."""
    url = f"postgresql://runner:{SECRET}@127.0.0.1:1/none?password={SECRET}"
    result, output = _run_with_url(url)
    assert SECRET not in output
    assert "***" in result.stdout
