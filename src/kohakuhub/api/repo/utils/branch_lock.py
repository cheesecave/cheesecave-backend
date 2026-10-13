"""One commit at a time per branch (#56).

LakeFS commits whatever is staged on a branch. Two commit requests staging on
the same branch at once therefore made one LakeFS commit carry both requests'
files: the other found nothing of its own to commit ("no changes", or a
refused update, answered 500), its File rows were undone although its files
were committed, and with no row linking them the LFS collector deleted their
objects a day later.

So the commit route takes this lease before it stages anything and gives it
back after everything is recorded. The unique key on ``(repository, branch)``
decides who holds the branch; a request that cannot take it is refused at once
with the same answer whoever it is (409, retryable) and has staged nothing.
A client that wants its commit after another's tries again, or names the
``parent_commit`` it expects (refused with 412 once the branch has moved, as
the Hub answers).

The lease is in the database, not in the process, because the API runs several
workers. It expires, so a process that dies holding it frees the branch within
``LEASE_SECONDS``; a holder renews it every ``RENEW_SECONDS`` however long it
works, and checks it still holds it right before LakeFS commits (``Lease.check``).
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import HTTPException
from peewee import IntegrityError

from kohakuhub.db import BranchCommitLock, Repository, utcnow
from kohakuhub.logger import get_logger

logger = get_logger("BRANCH_LOCK")

LEASE_SECONDS = 60  # a crashed holder frees the branch after this long
RENEW_SECONDS = LEASE_SECONDS / 3  # renewed this often while the commit runs
RETRY_AFTER = "2"  # seconds, for requests refused while the branch is held


def _same(repo_id: int, branch: str):
    L = BranchCommitLock
    return (L.repository == repo_id) & (L.branch == branch)


def acquire(repo_id: int, branch: str) -> str | None:
    """Take the branch; its token, or ``None`` if somebody holds it.

    An expired lease is removed first, so the holder that died is not waited for.
    """
    token = uuid.uuid4().hex
    now = utcnow()
    L = BranchCommitLock
    try:
        with L._meta.database.atomic():
            L.delete().where(_same(repo_id, branch) & (L.until <= now)).execute()
            L.create(
                repository=repo_id,
                branch=branch,
                token=token,
                until=now + timedelta(seconds=LEASE_SECONDS),
            )
    except IntegrityError:
        return None
    return token


def renew(repo_id: int, branch: str, token: str) -> bool:
    """Extend the lease; whether ``token`` still holds the branch."""
    now = utcnow()
    L = BranchCommitLock
    return bool(
        L.update(until=now + timedelta(seconds=LEASE_SECONDS))
        .where(_same(repo_id, branch) & (L.token == token) & (L.until > now))
        .execute()
    )


def is_held(repo_id: int, branch: str, token: str) -> bool:
    """Whether ``token`` holds the branch right now (a lapsed lease does not)."""
    L = BranchCommitLock
    return (
        L.select()
        .where(_same(repo_id, branch) & (L.token == token) & (L.until > utcnow()))
        .exists()
    )


def release(repo_id: int, branch: str, token: str) -> None:
    """Give the branch back; a lease that is somebody else's by now stays theirs."""
    BranchCommitLock.delete().where(
        _same(repo_id, branch) & (BranchCommitLock.token == token)
    ).execute()


def _busy(branch: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": (
                f"Branch '{branch}' is busy: another commit to it is in progress. "
                "Nothing was committed; try again in a few seconds."
            )
        },
        headers={"Retry-After": RETRY_AFTER},
    )


def _lost(branch: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": (
                f"The lease on branch '{branch}' was lost before the commit finished. "
                "Nothing was committed; try again."
            )
        },
        headers={"Retry-After": RETRY_AFTER},
    )


class Lease:
    """What ``holding`` yields: the way to ask whether the branch is still ours."""

    def __init__(self, repo_id: int, branch: str, token: str):
        self.repo_id, self.branch, self.token = repo_id, branch, token

    def check(self) -> None:
        """Refuse (409) if the lease lapsed or was taken since: LakeFS must not
        commit on a branch somebody else may be staging on."""
        if not is_held(self.repo_id, self.branch, self.token):
            raise _lost(self.branch)


@asynccontextmanager
async def holding(repo: Repository, branch: str):
    """Hold the branch for the length of the block; refuse (409) if it is held."""
    token = acquire(repo.id, branch)
    if token is None:
        raise _busy(branch)

    async def keep_renewing():
        while True:
            await asyncio.sleep(RENEW_SECONDS)
            try:
                if not renew(repo.id, branch, token):
                    logger.warning(f"The lease on {repo.full_id}@{branch} was lost")
                    return
            except Exception as e:  # tried again next time
                logger.warning(f"Could not renew the lease on {repo.full_id}@{branch}: {e}")

    renewing = asyncio.create_task(keep_renewing())
    try:
        yield Lease(repo.id, branch, token)
    finally:
        renewing.cancel()
        try:
            release(repo.id, branch, token)
        except Exception as e:  # the lease lapses by itself; what failed in the block matters more
            logger.warning(f"Could not give back the lease on {repo.full_id}@{branch}: {e}")
