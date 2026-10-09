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
