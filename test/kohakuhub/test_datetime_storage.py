"""Datetimes come back naive UTC on every backend, as PostgreSQL returns them."""

from datetime import datetime, timedelta, timezone

from kohakuhub.db import BackgroundTask


def test_aware_datetimes_read_back_naive_utc(db_fresh):
    aware = datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
    task = BackgroundTask.create(kind="test.datetime", run_after=aware, created_at=aware)

    stored = BackgroundTask.get_by_id(task.id)

    assert stored.created_at == datetime(2026, 1, 2, 3, 4, 5, 6)
    assert stored.created_at.tzinfo is None
    # Comparable with a naive datetime, as the code above and the list endpoints do
    assert stored.created_at < datetime(2026, 1, 3)


def test_non_utc_aware_datetimes_are_stored_as_utc(db_fresh):
    local = datetime(2026, 1, 2, 12, tzinfo=timezone(timedelta(hours=8)))
    task = BackgroundTask.create(kind="test.datetime.offset", run_after=local, created_at=local)

    assert BackgroundTask.get_by_id(task.id).created_at == datetime(2026, 1, 2, 4)


def test_naive_and_null_datetimes_are_unchanged(db_fresh):
    naive = datetime(2026, 1, 2, 3, 4, 5)
    task = BackgroundTask.create(kind="test.datetime.naive", run_after=naive, created_at=naive)

    stored = BackgroundTask.get_by_id(task.id)

    assert stored.created_at == naive
    assert stored.started_at is None


# ---- epoch seconds on the wire: UTC whatever the zone of the server process -----------------------------------------

import pytest

from kohakuhub.utils.datetime_utils import epoch_to_http_date, epoch_to_iso_z
from test.kohakuhub.support.zones import ZONES, in_zone

EPOCH = 1700000000  # 2023-11-14T22:13:20Z


@pytest.mark.parametrize("zone", ZONES)
def test_epoch_seconds_are_written_as_utc_in_any_server_zone(zone):
    with in_zone(zone):
        assert epoch_to_iso_z(EPOCH) == "2023-11-14T22:13:20.000000Z"
        assert epoch_to_iso_z(EPOCH + 0.25) == "2023-11-14T22:13:20.250000Z"
        assert epoch_to_iso_z(1) == "1970-01-01T00:00:01.000000Z"
        assert epoch_to_http_date(EPOCH) == "Tue, 14 Nov 2023 22:13:20 GMT"
        assert epoch_to_http_date(1) == "Thu, 01 Jan 1970 00:00:01 GMT"


def test_the_http_date_does_not_depend_on_the_locale(monkeypatch):
    import locale

    # day and month names stay English whatever LC_TIME says (a header is not translated)
    try:
        locale.setlocale(locale.LC_TIME, "de_DE.UTF-8")
    except locale.Error:
        pytest.skip("no German locale on this machine")
    try:
        assert epoch_to_http_date(EPOCH) == "Tue, 14 Nov 2023 22:13:20 GMT"
    finally:
        locale.setlocale(locale.LC_TIME, "")
