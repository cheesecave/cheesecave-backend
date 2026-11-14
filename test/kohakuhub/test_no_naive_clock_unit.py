"""No code of the application reads the clock or an epoch in the zone of the server process.

``datetime.fromtimestamp(epoch)`` and ``datetime.now()`` without a zone use the local zone of the process. Put in front of a literal ``Z`` (or
stored in a UTC column) they put the server's local time on the wire or in the database: -8 h on a server in Los Angeles, +8 h in Shanghai.
Production runs in UTC, so nothing shows there; this keeps it from coming back. ``utcnow()`` is UTC and not covered; ``.timestamp()`` of a naive
value (the collections ETag) is an opaque cache key and not covered.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "kohakuhub"
# legacy module (excluded from coverage): the time of day of a log line, not data
ALLOWED = {"old_logger.py"}


def naive_clock_calls(source: str) -> list[tuple[int, str]]:
    """The calls in ``source`` that depend on the zone of the process: (line, what)."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        name, owner = node.func.attr, node.func.value
        zone_given = len(node.args) >= 2 or any(keyword.arg == "tz" for keyword in node.keywords)
        if name == "fromtimestamp" and not zone_given:
            found.append((node.lineno, "fromtimestamp() without tz"))
        elif name == "now" and isinstance(owner, ast.Name) and owner.id == "datetime" and not node.args and not node.keywords:
            found.append((node.lineno, "datetime.now() without tz"))
        elif name == "today" and isinstance(owner, ast.Name) and owner.id in ("datetime", "date"):
            found.append((node.lineno, f"{owner.id}.today()"))
    return found


@pytest.mark.parametrize(
    "source, expected",
    [
        ("datetime.fromtimestamp(1)", ["fromtimestamp() without tz"]),
        ("datetime.fromtimestamp(1).strftime('%Y')", ["fromtimestamp() without tz"]),
        ("datetime.fromtimestamp(1, tz=timezone.utc)", []),
        ("datetime.fromtimestamp(1, timezone.utc)", []),
        ("datetime.now()", ["datetime.now() without tz"]),
        ("datetime.now(timezone.utc)", []),
        ("datetime.now(tz=timezone.utc)", []),
        ("datetime.today()", ["datetime.today()"]),
        ("date.today()", ["date.today()"]),
        ("datetime.utcnow()", []),
        ("time.time()", []),
        ("x = 1", []),
    ],
)
def test_the_scanner_tells_zone_dependent_calls_from_utc_ones(source, expected):
    assert [what for _, what in naive_clock_calls(source)] == expected


def test_no_application_module_reads_the_clock_or_an_epoch_in_the_process_zone():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name in ALLOWED:
            continue
        for line, what in naive_clock_calls(path.read_text()):
            offenders.append(f"{path.relative_to(SRC.parent.parent)}:{line}: {what}")
    assert not offenders, "use tz=timezone.utc (or kohakuhub.utils.datetime_utils.epoch_to_iso_z):\n" + "\n".join(offenders)
