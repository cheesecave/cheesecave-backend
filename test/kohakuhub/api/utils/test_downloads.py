"""Tests for download tracking helpers, on real repository, session and statistics rows."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest

import kohakuhub.api.utils.downloads as download_utils
from kohakuhub.db import DailyRepoStats, DownloadSession, Repository
from test.kohakuhub.support.db import table_missing
from test.kohakuhub.support.factories import (
    make_daily_stats,
    make_download_session,
    make_repo,
    make_user,
)

pytestmark = pytest.mark.usefixtures("db_scope")


def _day(days_ago: int):
    return datetime.now(timezone.utc).date() - timedelta(days=days_ago)


def _session_on(repo, day, session_id, **overrides):
    """A session first downloaded at noon UTC on ``day``."""
    at = datetime.combine(day, time(12), tzinfo=timezone.utc)
    return make_download_session(repo, session_id, first_download_at=at, **overrides)


def _count_transactions(monkeypatch, seen):
    """Count the transactions download tracking opens; the writes inside stay real."""
    real_db = download_utils.db

    @contextmanager
    def counted():
        seen["entered"] = seen.get("entered", 0) + 1
        try:
            with real_db.atomic():
                yield
        finally:
            seen["exited"] = seen.get("exited", 0) + 1

    monkeypatch.setattr(download_utils, "db", SimpleNamespace(atomic=counted))


def test_get_or_create_tracking_cookie_reuses_existing_and_sets_new_cookie(monkeypatch):
    response_cookies = {}

    assert (
        download_utils.get_or_create_tracking_cookie(
            {"hf_download_session": "existing"}, response_cookies
        )
        == "existing"
    )
    assert response_cookies == {}

    monkeypatch.setattr(download_utils.uuid, "uuid4", lambda: SimpleNamespace(hex="newsession"))
    created = download_utils.get_or_create_tracking_cookie({}, response_cookies)

    assert created == "newsession"
    assert response_cookies["hf_download_session"]["httponly"] is True


@pytest.mark.asyncio
async def test_track_download_async_updates_existing_session(monkeypatch):
    owner = make_user("owner")
    repo = make_repo(owner, "repo")
    existing = make_download_session(repo, "session-1", time_bucket=12, file_count=2)
    today_stats = make_daily_stats(repo, datetime.now(timezone.utc).date(), download_sessions=1, total_files=2)

    monkeypatch.setattr(download_utils.time, "time", lambda: 120)
    monkeypatch.setattr(download_utils.cfg.app, "download_time_bucket_seconds", 10)

    await download_utils.track_download_async(repo, "README.md", "session-1", user=None)

    assert DownloadSession.get_by_id(existing.id).file_count == 3
    assert DailyRepoStats.get_by_id(today_stats.id).total_files == 3
    assert DownloadSession.select().count() == 1


@pytest.mark.asyncio
async def test_track_download_async_creates_new_session_and_schedules_cleanup(monkeypatch):
    owner = make_user("owner")
    repo = make_repo(owner, "repo", downloads=4)
    for index in range(3):
        make_download_session(repo, f"older-{index}", time_bucket=1)
    transactions = {}
    scheduled = []

    def fake_create_task(coro):
        scheduled.append(coro.cr_code.co_name)
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(download_utils.time, "time", lambda: 250)
    monkeypatch.setattr(download_utils.cfg.app, "download_time_bucket_seconds", 5)
    monkeypatch.setattr(download_utils.cfg.app, "download_session_cleanup_threshold", 3)
    monkeypatch.setattr(download_utils.asyncio, "create_task", fake_create_task)
    _count_transactions(monkeypatch, transactions)

    await download_utils.track_download_async(repo, "weights.bin", "session-2", user=owner)

    assert transactions == {"entered": 1, "exited": 1}
    created = DownloadSession.get(DownloadSession.session_id == "session-2")
    assert created.first_file == "weights.bin" and created.user_id == owner.id
    assert created.time_bucket == 50
    assert Repository.get_by_id(repo.id).downloads == 5
    today_stats = DailyRepoStats.get(DailyRepoStats.repository == repo)
    assert today_stats.download_sessions == 1
    assert today_stats.authenticated_downloads == 1
    assert today_stats.total_files == 1
    assert scheduled == ["aggregate_old_sessions"]


@pytest.mark.asyncio
async def test_new_session_below_cleanup_threshold_schedules_nothing(monkeypatch):
    repo = make_repo(make_user("owner"), "repo")
    scheduled = []

    monkeypatch.setattr(download_utils.time, "time", lambda: 250)
    monkeypatch.setattr(download_utils.cfg.app, "download_time_bucket_seconds", 5)
    monkeypatch.setattr(download_utils.cfg.app, "download_session_cleanup_threshold", 3)

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(download_utils.asyncio, "create_task", fake_create_task)

    await download_utils.track_download_async(repo, "weights.bin", "session-3", user=None)

    assert DownloadSession.select().count() == 1
    assert scheduled == []


@pytest.mark.asyncio
async def test_new_session_is_rolled_back_when_the_statistics_write_fails(db_scope, monkeypatch):
    repo = make_repo(make_user("owner"), "repo", downloads=4)
    logged = []
    monkeypatch.setattr(download_utils.time, "time", lambda: 250)
    monkeypatch.setattr(download_utils.cfg.app, "download_time_bucket_seconds", 5)
    monkeypatch.setattr(download_utils.logger, "exception", lambda message, error: logged.append(message))

    with table_missing(db_scope, DailyRepoStats):
        await download_utils.track_download_async(repo, "weights.bin", "session-4", user=None)

    # The failed statistics write takes the session and the counter with it.
    assert DownloadSession.select().count() == 0
    assert Repository.get_by_id(repo.id).downloads == 4
    assert logged == [f"Failed to track download for {repo.full_id}"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("latest_days_ago", "session_days_ago", "aggregated_days_ago"),
    [
        (None, [5, 1, 0], [5, 1]),
        (3, [5, 2, 1, 0], [2, 1]),
        (1, [2, 1, 0], []),
    ],
)
async def test_ensure_stats_up_to_date_dispatches_aggregation_ranges(
    latest_days_ago, session_days_ago, aggregated_days_ago
):
    repo = make_repo(make_user("owner"), "repo")
    stored_days = set()
    if latest_days_ago is not None:
        make_daily_stats(repo, _day(latest_days_ago))
        stored_days.add(_day(latest_days_ago))
    for days_ago in session_days_ago:
        _session_on(repo, _day(days_ago), f"s-{days_ago}")

    await download_utils.ensure_stats_up_to_date(repo)

    dates = {row.date for row in DailyRepoStats.select().where(DailyRepoStats.repository == repo)}
    # Today is never aggregated: it is updated in real time.
    assert dates == stored_days | {_day(days_ago) for days_ago in aggregated_days_ago}


@pytest.mark.asyncio
async def test_aggregate_sessions_to_daily_groups_sessions_and_upserts(monkeypatch):
    owner = make_user("owner")
    repo = make_repo(owner, "repo")
    _session_on(repo, _day(2), "a", user=owner, file_count=2)
    _session_on(repo, _day(2), "b", file_count=1)
    _session_on(repo, _day(1), "c", file_count=4)
    transactions = {}
    _count_transactions(monkeypatch, transactions)

    await download_utils.aggregate_sessions_to_daily(
        repo,
        start_date=_day(3),
        end_date=_day(1),
    )

    assert transactions == {"entered": 1, "exited": 1}
    assert DailyRepoStats.select().where(DailyRepoStats.repository == repo).count() == 2
    older_stats = DailyRepoStats.get((DailyRepoStats.repository == repo) & (DailyRepoStats.date == _day(2)))
    assert older_stats.download_sessions == 2
    assert older_stats.authenticated_downloads == 1
    assert older_stats.anonymous_downloads == 1
    assert older_stats.total_files == 3


@pytest.mark.asyncio
async def test_aggregate_sessions_to_daily_without_sessions_writes_nothing():
    repo = make_repo(make_user("owner"), "repo")

    await download_utils.aggregate_sessions_to_daily(repo, start_date=None, end_date=_day(1))

    assert DailyRepoStats.select().count() == 0


@pytest.mark.asyncio
async def test_aggregate_old_sessions_keeps_recent_sessions_when_none_are_old(monkeypatch):
    repo = make_repo(make_user("owner"), "repo")
    recent = _session_on(repo, _day(1), "recent")
    monkeypatch.setattr(download_utils.cfg.app, "download_keep_sessions_days", 7)

    await download_utils.aggregate_old_sessions(repo)

    assert [session.id for session in DownloadSession.select()] == [recent.id]


@pytest.mark.asyncio
async def test_aggregate_old_sessions_cleans_up_and_handles_errors(db_scope, monkeypatch):
    owner = make_user("owner")
    repo = make_repo(owner, "repo")
    old = _session_on(repo, _day(10), "old")
    recent = _session_on(repo, _day(1), "recent")
    monkeypatch.setattr(download_utils.cfg.app, "download_keep_sessions_days", 7)

    await download_utils.aggregate_old_sessions(repo)

    # The historical day was aggregated before the old session was deleted.
    assert DailyRepoStats.get(DailyRepoStats.repository == repo).date == _day(10)
    assert [session.id for session in DownloadSession.select()] == [recent.id]
    assert not DownloadSession.select().where(DownloadSession.id == old.id).exists()

    errors = []
    monkeypatch.setattr(download_utils.logger, "exception", lambda message, error: errors.append((message, str(error))))

    with table_missing(db_scope, DownloadSession):
        await download_utils.aggregate_old_sessions(repo)

    assert len(errors) == 1
    assert errors[0][0] == "Failed to aggregate old sessions for owner/repo"
    # The real database error names the missing table.
    assert "downloadsession" in errors[0][1].lower()
