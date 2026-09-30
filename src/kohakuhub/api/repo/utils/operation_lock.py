"""A history operation holding a repository, so writes do not race it.

Super Squash moves a branch to a commit LakeFS cannot move it to
conditionally (a hard reset): a write completing in between would be lost.
So the two sides wait for each other through the database:

- a write about to move a branch in LakeFS (a commit, a merge, a revert, a
  reset's merge, a branch or tag change) registers itself first, then
  checks the lock (``writing``); if it is held, it withdraws, waits until
  the lock is free, and tries again;
- the operation takes the lock first, then waits until no write is
  registered (``drain``).

Each side writes before it reads what the other wrote, so at least one of
them sees the other: a write never lands inside the operation. Writes that
have not started their work are refused at once with a retryable 409
(``ensure_free``). A commit already uploading waits; LakeFS keeps its staged
changes across the operation, so they land on top of it.

The lock and the registrations expire, so a process that dies holding one
does not block the repository for long; a token makes sure only its holder
releases or renews the lock.
"""

import asyncio
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta

from fastapi import HTTPException

from kohakuhub.db import Repository, RepositoryWrite, utcnow

LOCK_SECONDS = 60  # a crashed holder frees the repository after this long
WRITE_SECONDS = 600  # a crashed writer's registration lapses after this long
WAIT_SECONDS = LOCK_SECONDS + 30  # a write waits this long for the lock at most
DRAIN_SECONDS = 120  # an operation waits this long for registered writes at most
POLL_SECONDS = 0.2
RETRY_AFTER = "2"  # seconds, for clients refused while the lock is held


def _held():
    R = Repository
    return R.operation_until.is_null(False) & (R.operation_until > utcnow())


def acquire(repo_id: int, operation: str) -> str | None:
    """Take the lock for ``operation``; its token, or ``None`` if it is held."""
    token = f"{operation}:{uuid.uuid4().hex}"
    R = Repository
    taken = (
        R.update(
            operation=token, operation_until=utcnow() + timedelta(seconds=LOCK_SECONDS)
        )
        .where((R.id == repo_id) & ~_held())
        .execute()
    )
    return token if taken else None


def renew(repo_id: int, token: str) -> bool:
    """Extend the lock; whether ``token`` still holds it."""
    R = Repository
    return bool(
        R.update(operation_until=utcnow() + timedelta(seconds=LOCK_SECONDS))
        .where((R.id == repo_id) & (R.operation == token) & _held())
        .execute()
    )


def release(repo_id: int, token: str) -> None:
    R = Repository
    R.update(operation=None, operation_until=None).where(
        (R.id == repo_id) & (R.operation == token)
    ).execute()


def holder(repo_id: int) -> str | None:
    """The operation holding the repository, if any."""
    R = Repository
    token = R.select(R.operation).where((R.id == repo_id) & _held()).scalar()
    return token.split(":", 1)[0] if token else None


def _refusal(operation: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": f"The repository is busy ({operation} in progress); try again in a few seconds.",
            "operation": operation,
        },
        headers={"Retry-After": RETRY_AFTER},
    )


def ensure_free(repo: Repository) -> None:
    """Refuse a write that has not started while an operation holds the repository."""
    operation = holder(repo.id)
    if operation:
        raise _refusal(operation)


async def _poll(done, seconds: float, refusal) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not done():
        if loop.time() >= deadline:
            raise refusal()
        await asyncio.sleep(POLL_SECONDS)


@asynccontextmanager
async def writing(repo: Repository):
    """Around a write that moves a branch in LakeFS (see the module docstring)."""
    W = RepositoryWrite
    while True:
        registration = W.create(
            repository=repo.id, until=utcnow() + timedelta(seconds=WRITE_SECONDS)
        )
        operation = holder(repo.id)
        if operation is None:
            break
        registration.delete_instance()
        await _poll(
            lambda: holder(repo.id) is None, WAIT_SECONDS, lambda: _refusal(operation)
        )
    try:
        yield
    finally:
        W.delete().where(W.id == registration.id).execute()


async def drain(repo: Repository, operation: str) -> None:
    """For the lock's holder: wait until no write is registered."""
    W = RepositoryWrite

    def idle():
        return (
            not W.select()
            .where((W.repository == repo.id) & (W.until > utcnow()))
            .exists()
        )

    await _poll(idle, DRAIN_SECONDS, lambda: _refusal(f"writes before {operation}"))


@contextmanager
def held(repo: Repository, operation: str):
    """Hold the repository for ``operation`` (yields the token); refuse (409)
    if another holds it."""
    token = acquire(repo.id, operation)
    if token is None:
        raise _refusal(holder(repo.id) or operation)
    try:
        yield token
    finally:
        release(repo.id, token)
