"""Run code as if the server process were in a given time zone (``TZ`` plus ``time.tzset``)."""

from __future__ import annotations

import contextlib
import os
import time

# UTC, one zone each side of it (whole hours), a half-hour zone and the farthest one (UTC+14)
ZONES = ("UTC", "America/Los_Angeles", "Asia/Shanghai", "Asia/Kolkata", "Pacific/Kiritimati")


@contextlib.contextmanager
def in_zone(zone: str):
    """The process local time zone is ``zone`` inside the block and as it was after it."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = zone
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()
