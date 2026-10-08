"""Unit tests for statistics routes, on real repository and daily-stats rows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import kohakuhub.api.stats as stats_api
from test.kohakuhub.support.factories import make_daily_stats, make_repo, make_user

pytestmark = pytest.mark.usefixtures("db_scope")


def _today():
    return datetime.now(timezone.utc).date()


@pytest.mark.asyncio
async def test_get_recent_stats_returns_hf_not_found_when_repository_is_missing():
    response = await stats_api.get_recent_stats("model", "owner", "missing", days=14, user=None)

    assert response.status_code == 404
    assert response.headers["X-Error-Code"] == "RepoNotFound"


@pytest.mark.asyncio
async def test_get_recent_stats_lists_the_requested_window_in_date_order():
    owner = make_user("owner")
    repo = make_repo(owner, "demo")
    today = _today()
    make_daily_stats(repo, today - timedelta(days=10), download_sessions=1)  # outside the window
    make_daily_stats(repo, today - timedelta(days=2), download_sessions=4)
    make_daily_stats(repo, today, download_sessions=7)

    response = await stats_api.get_recent_stats("model", "owner", "demo", days=3, user=None)

    assert response["period"] == {
        "start": str(today - timedelta(days=2)),
        "end": str(today),
        "days": 3,
    }
    assert [item["downloads"] for item in response["stats"]] == [4, 7]
    assert [item["date"] for item in response["stats"]] == [
        str(today - timedelta(days=2)),
        str(today),
    ]


@pytest.mark.asyncio
async def test_get_trending_repositories_hides_private_repos_from_anonymous_readers():
    owner = make_user("owner")
    public = make_repo(owner, "public")
    private = make_repo(owner, "private", private=True)
    today = _today()
    make_daily_stats(public, today, download_sessions=7)
    make_daily_stats(public, today - timedelta(days=1), download_sessions=3)
    make_daily_stats(private, today, download_sessions=5)

    response = await stats_api.get_trending_repositories(
        repo_type="model",
        days=7,
        limit=10,
        user=None,
    )

    assert response["trending"] == [
        {
            "id": "owner/public",
            "type": "model",
            "downloads": public.downloads,
            "likes": public.likes_count,
            "recent_downloads": 10,
            "private": False,
        }
    ]
    assert response["period"]["days"] == 7


@pytest.mark.asyncio
async def test_get_trending_repositories_shows_private_repos_to_their_owner():
    owner = make_user("owner")
    private = make_repo(owner, "private", private=True)
    make_daily_stats(private, _today(), download_sessions=5)

    response = await stats_api.get_trending_repositories(
        repo_type="model",
        days=7,
        limit=10,
        user=owner,
    )

    assert [item["id"] for item in response["trending"]] == ["owner/private"]
    assert response["trending"][0]["private"] is True


@pytest.mark.asyncio
async def test_get_repository_stats_reports_the_row_counters_and_404s_unknown_repos():
    owner = make_user("owner")
    repo = make_repo(owner, "demo")
    repo.downloads = 42
    repo.likes_count = 3
    repo.save()

    response = await stats_api.get_repository_stats("model", "owner", "demo", user=None)
    assert response == {"downloads": 42, "likes": 3}

    missing = await stats_api.get_repository_stats("model", "owner", "missing", user=None)
    assert missing.status_code == 404
