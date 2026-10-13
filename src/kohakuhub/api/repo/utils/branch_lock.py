"""One commit at a time per branch (#56).

LakeFS commits whatever is staged on a branch. Two commit requests staging on
the same branch at once therefore made one LakeFS commit carry both requests'
files: the other found nothing of its own to commit ("no changes", or a
refused update, answered 500), its File rows were undone although its files
were committed, and with no row linking them the LFS collector deleted their
objects a day later.

So the commit route takes this lease before it stages anything and gives it
back after everything is recorded. The unique key on ``(repository, branch)``
decides who holds the branch. Like the Hub's concurrency queue, a commit that
finds the branch busy waits for its turn (``QUEUE_SECONDS``); one that waits
too long gets what the Hub answers then: ``429 maximum time in concurrency
queue reached`` (huggingface_hub issue 3071), which huggingface_hub retries
with backoff, honouring ``Retry-After``. Whoever it is, the answer is the same,
and the request has staged nothing. A client that names the ``parent_commit``
it expects is refused with 412 once the branch has moved, as the Hub does.

The lease is in the database, not in the process, because the API runs several
workers. It expires, so a process that dies holding it frees the branch within
``LEASE_SECONDS``; a holder renews it every ``RENEW_SECONDS`` however long it
works, and checks it still holds it right before LakeFS commits (``Lease.check``).
"""

import asyncio
import random
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
QUEUE_SECONDS = 20.0  # a commit waits this long for the branch before it is refused
QUEUE_POLL_SECONDS = 0.05  # how often a waiting commit looks again (with some jitter)
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


def _refusal(status: int, message: str) -> HTTPException:
    """``X-Error-Message`` is what ``huggingface_hub`` shows the user: it does not read the
    ``detail`` object FastAPI wraps the error in. ``Retry-After`` is honoured by its backoff."""
    return HTTPException(
        status_code=status,
        detail={"error": message},
        headers={"Retry-After": RETRY_AFTER, "X-Error-Message": message},
    )


def _busy() -> HTTPException:
    # The Hub's own words for a commit that waited too long in its queue
    return _refusal(429, "maximum time in concurrency queue reached")


def _lost(branch: str) -> HTTPException:
    # Nothing was committed, so a client may retry: 503 is retried with backoff
    return _refusal(
        503,
        f"The lease on branch '{branch}' was lost before the commit finished. "
        "Nothing was committed; try again.",
    )


async def _wait_for(repo_id: int, branch: str) -> str:
    """Take the branch, waiting up to ``QUEUE_SECONDS`` for its turn."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + QUEUE_SECONDS
    while True:
        token = acquire(repo_id, branch)
        if token is not None:
            return token
        if loop.time() >= deadline:
            raise _busy()
        await asyncio.sleep(QUEUE_POLL_SECONDS * (0.5 + random.random()))


class Lease:
    """What ``holding`` yields: the way to ask whether the branch is still ours."""

    def __init__(self, repo_id: int, branch: str, token: str):
        self.repo_id, self.branch, self.token = repo_id, branch, token

    def check(self) -> None:
        """Refuse (503) if the lease lapsed or was taken since: LakeFS must not
        commit on a branch somebody else may be staging on."""
        if not is_held(self.repo_id, self.branch, self.token):
            raise _lost(self.branch)


@asynccontextmanager
async def holding(repo: Repository, branch: str):
    """Hold the branch for the length of the block; wait for it if it is held, and refuse
    (429) once the wait is over."""
    token = await _wait_for(repo.id, branch)

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
