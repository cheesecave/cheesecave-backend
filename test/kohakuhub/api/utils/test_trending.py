"""Unit tests for trending helpers."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from types import SimpleNamespace

import kohakuhub.api.utils.trending as trending


class _Expr:
    def __and__(self, other):
        return self


class _Field:
    def in_(self, values):
        return _Expr()

    def __eq__(self, other):
        return _Expr()

    def __ge__(self, other):
        return _Expr()

    def __le__(self, other):
        return _Expr()

    def desc(self):
        return self


class _Query:
    def __init__(self, items):
        self.items = list(items)

    def join(self, model):
        return self

    def select(self, *fields):
        return self

    def scalars(self):
        return iter(item.id for item in self.items)

    def where(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, value):
        self.items = self.items[:value]
        return self

    def __iter__(self):
        return iter(self.items)


class _FrozenDatetime:
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 4, 20, tzinfo=timezone.utc)


def test_calculate_trending_scores_applies_decay_and_aggregates_multiple_days(monkeypatch):
    stats = [
        SimpleNamespace(
            repository_id=1,
            date=datetime(2026, 4, 20, tzinfo=timezone.utc).date(),
            download_sessions=10,
        ),
        SimpleNamespace(
            repository_id=1,
            date=datetime(2026, 4, 19, tzinfo=timezone.utc).date(),
            download_sessions=5,
        ),
        SimpleNamespace(
            repository_id=2,
            date=datetime(2026, 4, 18, tzinfo=timezone.utc).date(),
            download_sessions=1,
        ),
    ]

    fake_daily_stats = SimpleNamespace(
        repository=_Field(),
        date=_Field(),
        download_sessions=_Field(),
        select=lambda *args: _Query(stats),
    )
    fake_repo_model = SimpleNamespace(repo_type=_Field())

    monkeypatch.setattr(trending, "datetime", _FrozenDatetime)
    monkeypatch.setattr(trending, "DailyRepoStats", fake_daily_stats)
    monkeypatch.setattr(trending, "Repository", fake_repo_model)

    scores = trending.calculate_trending_scores("model", days=7)

    expected_repo_1 = math.log(11) + (math.log(6) * 0.8)
    expected_repo_2 = math.log(2) * (0.8**2)
    assert math.isclose(scores[1], expected_repo_1, rel_tol=1e-6)
    assert math.isclose(scores[2], expected_repo_2, rel_tol=1e-6)
    assert scores[1] > scores[2]


def test_get_trending_repositories_falls_back_to_recent_public_repos(monkeypatch):
    repos = [
        SimpleNamespace(full_id="owner/one", private=False),
        SimpleNamespace(full_id="owner/two", private=False),
        SimpleNamespace(full_id="owner/three", private=False),
    ]

    fake_repo_model = SimpleNamespace(
        repo_type=_Field(),
        private=_Field(),
        created_at=_Field(),
        select=lambda: _Query(repos),
    )

    monkeypatch.setattr(trending, "Repository", fake_repo_model)
    monkeypatch.setattr(trending, "calculate_trending_scores", lambda repo_type, days: {})

    result = trending.get_trending_repositories("model", limit=2, days=30)

    assert [repo.full_id for repo in result] == ["owner/one", "owner/two"]


class _CountingRepositoryModel:
    """Repository stand-in that records every select() and what it is asked to fetch."""

    id = _Field()
    repo_type = _Field()
    private = _Field()
    created_at = _Field()

    def __init__(self, rows):
        self.rows = list(rows)
        self.select_calls = 0
        self.get_or_none_calls = 0
        self.fetched_ids = None

    def select(self, *fields):
        self.select_calls += 1
        owner = self

        class _ByIds(_Query):
            def where(self, *args, **kwargs):
                owner.fetched_ids = "in_" if args else None
                self.items = [row for row in owner.rows]
                return self

        return _ByIds(self.rows)

    def get_or_none(self, expr):
        self.get_or_none_calls += 1
        return None


def test_get_trending_repositories_fetches_all_ranked_rows_in_one_query(monkeypatch):
    public_repo = SimpleNamespace(id=1, full_id="owner/public", private=False)
    other_public = SimpleNamespace(id=3, full_id="owner/other", private=False)
    private_repo = SimpleNamespace(id=2, full_id="owner/private", private=True)
    fake = _CountingRepositoryModel([private_repo, other_public, public_repo])

    monkeypatch.setattr(trending, "Repository", fake)
    monkeypatch.setattr(
        trending,
        "calculate_trending_scores",
        lambda repo_type, days: {1: 9.0, 2: 7.0, 3: 6.0},
    )

    repos = trending.get_trending_repositories("model", limit=5, days=7)

    assert repos == [public_repo, other_public]
    assert fake.get_or_none_calls == 0
    assert fake.select_calls == 2  # one candidate query for eligible ids, one fetch for ranked rows


def test_get_trending_repositories_keeps_score_order_and_drops_missing_rows(monkeypatch):
    first = SimpleNamespace(id=10, full_id="owner/first", private=False)
    third = SimpleNamespace(id=30, full_id="owner/third", private=False)
    fake = _CountingRepositoryModel([third, first])  # database order is not score order

    monkeypatch.setattr(trending, "Repository", fake)
    monkeypatch.setattr(
        trending,
        "calculate_trending_scores",
        lambda repo_type, days: {10: 9.0, 20: 8.0, 30: 7.0},  # 20 has no row
    )

    repos = trending.get_trending_repositories("model", limit=5, days=7)

    assert [repo.full_id for repo in repos] == ["owner/first", "owner/third"]


def test_get_trending_repositories_with_no_eligible_ranked_ids_skips_the_fetch(monkeypatch):
    fake = _CountingRepositoryModel([])

    monkeypatch.setattr(trending, "Repository", fake)
    monkeypatch.setattr(trending, "calculate_trending_scores", lambda repo_type, days: {99: 1.0})

    assert trending.get_trending_repositories("model", limit=5, days=7) == []
    assert fake.select_calls == 1  # only the candidate query; the ranked fetch is skipped
