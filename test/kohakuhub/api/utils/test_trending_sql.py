"""get_trending_repositories against a real SQLite database.

These tests run the actual SQL, so they check what the database returns and how many
times it is asked for repository rows, not only what a mock was told to return.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import kohakuhub.api.utils.trending as trending
from kohakuhub.db import DailyRepoStats, Repository, User
REPOSITORY_ROW_READ = re.compile(r'FROM "repository" AS')


@pytest.fixture
def db(db_scope, monkeypatch):
    """The shared scope, with every statement recorded so tests can count repository reads."""
    statements: list[str] = []
    real_execute = db_scope.execute_sql

    def counting_execute(sql, params=None, commit=None):
        statements.append(sql)
        return real_execute(sql, params)

    monkeypatch.setattr(db_scope, "execute_sql", counting_execute)
    return SimpleNamespace(database=db_scope, statements=statements)


def _repo(name, private=False):
    owner = User.get_or_none(User.username == "owner") or User.create(
        username="owner", normalized_name="owner", email="owner@example.com"
    )
    return Repository.create(
        repo_type="model",
        namespace="owner",
        name=name,
        full_id=f"owner/{name}",
        owner=owner,
        private=private,
        created_at=datetime(2025, 1, 1),
    )


def _score(repo, sessions):
    today = datetime.now(timezone.utc).date()
    DailyRepoStats.create(repository=repo, date=today, download_sessions=sessions)


def _repository_reads(statements):
    return sum(1 for sql in statements if REPOSITORY_ROW_READ.search(sql))


def test_ranked_repositories_come_back_in_score_order_from_real_rows(db):
    low = _repo("low")
    high = _repo("high")
    mid = _repo("mid")
    for repo, sessions in ((low, 2), (high, 50), (mid, 9)):
        _score(repo, sessions)

    result = trending.get_trending_repositories("model", limit=5, days=7)

    assert [repo.full_id for repo in result] == ["owner/high", "owner/mid", "owner/low"]


def test_one_candidate_read_and_one_ranked_read_regardless_of_limit(db):
    repos = [_repo(f"r{i}") for i in range(6)]
    for index, repo in enumerate(repos):
        _score(repo, 10 + index)
    db.statements.clear()

    result = trending.get_trending_repositories("model", limit=4, days=7)

    assert len(result) == 4
    # The candidate lookup (eligible ids) and the single fetch of the ranked rows.
    assert _repository_reads(db.statements) == 2


def test_private_repositories_are_never_returned_even_when_ranked_first(db):
    secret = _repo("secret", private=True)
    open_repo = _repo("open")
    _score(secret, 500)
    _score(open_repo, 5)

    result = trending.get_trending_repositories("model", limit=5, days=7)

    assert [repo.full_id for repo in result] == ["owner/open"]


def test_only_private_ranked_repositories_means_no_ranked_read(db):
    secret = _repo("secret", private=True)
    _score(secret, 500)
    db.statements.clear()

    assert trending.get_trending_repositories("model", limit=5, days=7) == []
    assert _repository_reads(db.statements) == 1  # the candidate lookup only


def test_no_trending_data_falls_back_to_recent_public_repositories(db):
    _repo("older")
    newer = _repo("newer")
    result = trending.get_trending_repositories("model", limit=1, days=7)
    assert [repo.id for repo in result] == [newer.id]
