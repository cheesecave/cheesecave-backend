"""One commit at a time per branch (#56): the lease the commit route holds.

The database is real (a unique key decides who holds the branch), so these run on SQLite and on
PostgreSQL.
"""

import asyncio
from datetime import timedelta

import pytest
from fastapi import HTTPException

from kohakuhub.api.repo.utils import branch_lock
from kohakuhub.db import BranchCommitLock, Repository, utcnow
from test.kohakuhub.support.factories import make_repo, make_user


@pytest.fixture
def repo(db_dual):
    return make_repo(make_user("owner"), "repo")


def _row(repo, branch="main"):
    return BranchCommitLock.get_or_none(
        (BranchCommitLock.repository == repo) & (BranchCommitLock.branch == branch)
    )


def test_a_branch_has_one_holder_at_a_time(repo):
    token = branch_lock.acquire(repo.id, "main")
    assert token
    assert branch_lock.acquire(repo.id, "main") is None


def test_other_branches_and_other_repositories_are_free(repo):
    other = make_repo(repo.owner, "other")
    assert branch_lock.acquire(repo.id, "main")
    assert branch_lock.acquire(repo.id, "dev")
    assert branch_lock.acquire(other.id, "main")


def test_only_the_holder_releases(repo):
    token = branch_lock.acquire(repo.id, "main")
    branch_lock.release(repo.id, "main", "someone-else")
    assert branch_lock.acquire(repo.id, "main") is None
    branch_lock.release(repo.id, "main", token)
    assert branch_lock.acquire(repo.id, "main")


def test_an_expired_lease_is_free_and_its_holder_is_out(repo):
    stale = branch_lock.acquire(repo.id, "main")
    BranchCommitLock.update(until=utcnow() - timedelta(seconds=1)).execute()
    assert not branch_lock.renew(repo.id, "main", stale)
    assert not branch_lock.is_held(repo.id, "main", stale)
    fresh = branch_lock.acquire(repo.id, "main")
    assert fresh and fresh != stale
    assert not branch_lock.is_held(repo.id, "main", stale)
    assert branch_lock.is_held(repo.id, "main", fresh)


def test_renewing_extends_only_the_holders_live_lease(repo):
    token = branch_lock.acquire(repo.id, "main")
    soon = utcnow() + timedelta(seconds=5)
    BranchCommitLock.update(until=soon).execute()
    assert branch_lock.renew(repo.id, "main", token)
    assert _row(repo).until > soon + timedelta(seconds=30)
    assert not branch_lock.renew(repo.id, "main", "someone-else")


def test_the_lease_goes_with_its_repository(repo):
    branch_lock.acquire(repo.id, "main")
    Repository.delete().where(Repository.id == repo.id).execute()
    assert BranchCommitLock.select().count() == 0


async def test_holding_takes_the_lease_and_gives_it_back(repo):
    async with branch_lock.holding(repo, "main") as lease:
        assert _row(repo) is not None
        lease.check()
    assert _row(repo) is None


async def test_holding_gives_the_lease_back_when_the_block_fails(repo):
    with pytest.raises(RuntimeError):
        async with branch_lock.holding(repo, "main"):
            raise RuntimeError("the commit failed")
    assert _row(repo) is None


async def test_a_busy_branch_is_refused_the_same_way_whoever_asks(repo):
    async with branch_lock.holding(repo, "main"):
        for _ in range(3):
            with pytest.raises(HTTPException) as refused:
                async with branch_lock.holding(repo, "main"):
                    raise AssertionError("must not get in")
            error = refused.value
            assert error.status_code == 409
            assert error.headers == {"Retry-After": branch_lock.RETRY_AFTER}
            assert "main" in error.detail["error"] and "busy" in error.detail["error"]
        # the refused askers did not take the holder's lease away
        assert _row(repo) is not None
    assert _row(repo) is None


async def test_check_fails_once_the_lease_is_lost(repo):
    async with branch_lock.holding(repo, "main") as lease:
        BranchCommitLock.delete().execute()  # expired and taken, or removed
        with pytest.raises(HTTPException) as lost:
            lease.check()
        assert lost.value.status_code == 409
        assert lost.value.headers == {"Retry-After": branch_lock.RETRY_AFTER}
        assert "lost" in lost.value.detail["error"]
    # leaving must not disturb a lease that is somebody else's now
    other = branch_lock.acquire(repo.id, "main")
    assert other


async def test_a_lost_lease_is_not_released_over_its_new_holder(repo):
    async with branch_lock.holding(repo, "main"):
        BranchCommitLock.delete().execute()
        taken = branch_lock.acquire(repo.id, "main")
    assert _row(repo).token == taken


async def test_the_lease_is_renewed_while_held(repo, monkeypatch):
    monkeypatch.setattr(branch_lock, "RENEW_SECONDS", 0.05)
    async with branch_lock.holding(repo, "main"):
        BranchCommitLock.update(until=utcnow() + timedelta(seconds=1)).execute()
        await asyncio.sleep(0.4)
        assert _row(repo).until > utcnow() + timedelta(seconds=30)


async def test_a_lost_lease_is_not_renewed_over_its_new_holder(repo, monkeypatch):
    monkeypatch.setattr(branch_lock, "RENEW_SECONDS", 0.05)
    async with branch_lock.holding(repo, "main"):
        BranchCommitLock.delete().execute()
        taken = branch_lock.acquire(repo.id, "main")
        until = _row(repo).until
        await asyncio.sleep(0.3)
        assert _row(repo).token == taken and _row(repo).until == until


async def test_a_failed_renewal_is_tried_again(repo, monkeypatch):
    # A deliberate database failure (an outage during a renewal), so the one targeted mock
    monkeypatch.setattr(branch_lock, "RENEW_SECONDS", 0.05)
    real = branch_lock.renew
    calls = []

    def flaky(*args):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("database unavailable")
        return real(*args)

    monkeypatch.setattr(branch_lock, "renew", flaky)
    async with branch_lock.holding(repo, "main"):
        BranchCommitLock.update(until=utcnow() + timedelta(seconds=1)).execute()
        await asyncio.sleep(0.4)
        assert len(calls) >= 2
        assert _row(repo).until > utcnow() + timedelta(seconds=30)


async def test_a_lease_that_cannot_be_given_back_hides_nothing(repo, monkeypatch):
    # A deliberate database failure (an outage while releasing), so the one targeted mock
    def unavailable(*args):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(branch_lock, "release", unavailable)
    with pytest.raises(ValueError, match="the commit failed"):
        async with branch_lock.holding(repo, "main"):
            raise ValueError("the commit failed")  # the error that matters, not the outage

    async with branch_lock.holding(repo, "other"):  # and a block that succeeded still succeeds
        pass
    monkeypatch.undo()
    # the leases lapse by themselves
    BranchCommitLock.update(until=utcnow() - timedelta(seconds=1)).execute()
    assert branch_lock.acquire(repo.id, "main") and branch_lock.acquire(repo.id, "other")
