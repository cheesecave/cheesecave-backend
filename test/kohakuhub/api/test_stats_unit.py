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
async def test_get_trending_repositories_skips_stats_of_repositories_of_another_type():
    # The stats aggregate is not filtered by repo type, so a model's stats row
    # reaches the Repository lookup for "dataset", finds no row, and is skipped.
    owner = make_user("owner")
    model = make_repo(owner, "model-demo")
    dataset = make_repo(owner, "data-demo", repo_type="dataset")
    make_daily_stats(model, _today(), download_sessions=9)
    make_daily_stats(dataset, _today(), download_sessions=2)

    response = await stats_api.get_trending_repositories(
        repo_type="dataset",
        days=7,
        limit=10,
        user=None,
    )

    assert [item["id"] for item in response["trending"]] == ["owner/data-demo"]


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


@pytest.fixture
def statements(db_scope, monkeypatch):
    """Record every SQL statement the route runs on the shared scope."""
    recorded: list[str] = []
    real_execute = db_scope.execute_sql

    def recording_execute(sql, params=None, commit=None):
        recorded.append(sql)
        return real_execute(sql, params)

    monkeypatch.setattr(db_scope, "execute_sql", recording_execute)
    return recorded


def _repository_reads(recorded):
    return sum(1 for sql in recorded if 'FROM "repository" AS' in sql)


@pytest.mark.asyncio
async def test_trending_fetches_every_ranked_repository_in_one_query(statements):
    owner = make_user("owner")
    today = _today()
    for name in ("one", "two", "three"):
        make_daily_stats(make_repo(owner, name), today, download_sessions=5)
    statements.clear()

    response = await stats_api.get_trending_repositories(
        repo_type="model", days=7, limit=10, user=None
    )

    assert len(response["trending"]) == 3
    # One candidate read is not counted here: the aggregation is one query and the
    # ranked repositories are fetched together, not once per row.
    assert _repository_reads(statements) == 1


@pytest.mark.asyncio
async def test_trending_skips_ranked_rows_whose_repository_is_another_type(statements):
    owner = make_user("owner")
    today = _today()
    dataset = make_repo(owner, "data", repo_type="dataset")
    make_daily_stats(dataset, today, download_sessions=9)
    make_daily_stats(make_repo(owner, "model"), today, download_sessions=1)

    response = await stats_api.get_trending_repositories(
        repo_type="model", days=7, limit=10, user=None
    )

    assert [item["id"] for item in response["trending"]] == ["owner/model"]


@pytest.mark.asyncio
async def test_trending_without_any_stats_returns_an_empty_list():
    make_repo(make_user("owner"), "quiet")

    response = await stats_api.get_trending_repositories(
        repo_type="model", days=7, limit=10, user=None
    )

    assert response["trending"] == []
