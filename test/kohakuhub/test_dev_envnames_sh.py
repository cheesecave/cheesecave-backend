"""scripts/dev/envnames.sh: the same naming rule as kohakuhub.envnames, for the dev shell scripts.

``cheese_cave_default NAME DEFAULT`` leaves CHEESE_CAVE_<NAME> holding the setting: itself when
set (an empty value counts), else KOHAKU_HUB_<NAME> when set, else the default. The dev scripts
then use only the new name.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts" / "dev" / "envnames.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _resolve(env: dict[str, str], default: str = "dflt") -> str:
    script = f'. "{HELPER}"; cheese_cave_default X "{default}"; printf %s "$CHEESE_CAVE_X"'
    result = subprocess.run(
        ["bash", "-c", script],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_the_default_applies_when_neither_prefix_is_set():
    assert _resolve({}) == "dflt"


def test_the_old_prefix_is_the_fallback():
    assert _resolve({"KOHAKU_HUB_X": "old"}) == "old"


def test_the_new_prefix_wins():
    assert _resolve({"KOHAKU_HUB_X": "old", "CHEESE_CAVE_X": "new"}) == "new"


def test_an_empty_value_counts_as_set():
    assert _resolve({"KOHAKU_HUB_X": "old", "CHEESE_CAVE_X": ""}) == ""
    assert _resolve({"KOHAKU_HUB_X": ""}) == ""


def test_the_result_is_exported_to_child_processes():
    script = (
        f'. "{HELPER}"; cheese_cave_default X dflt; '
        "sh -c 'printf %s \"$CHEESE_CAVE_X\"'"
    )
    result = subprocess.run(
        ["bash", "-c", script], env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=30
    )
    assert result.stdout == "dflt"


def test_a_default_with_spaces_and_quotes_survives():
    assert _resolve({}, default="two words") == "two words"
