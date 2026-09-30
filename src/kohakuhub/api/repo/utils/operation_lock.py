"""A history operation holding a repository, so writes do not race it.

Super Squash moves a branch to a commit LakeFS cannot move it to
conditionally (a hard reset): a commit completing in between would be lost.
While an operation holds the repository, writes that have not started are
refused with a retryable 409, and a commit already uploading waits before it
commits (its staged changes then land on top of the operation's commit).

The lock lives on the repository row with an expiry, so a process that dies
holding it does not block the repository for long; a token makes sure only
its holder releases it.
"""

import asyncio
import uuid
from contextlib import contextmanager
from datetime import timedelta

from fastapi import HTTPException

from kohakuhub.db import Repository, utcnow

LOCK_SECONDS = 60  # a crashed holder frees the repository after this long
WAIT_SECONDS = 30  # a commit already uploading waits this long for the lock
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
    """Refuse a write while an operation holds the repository."""
    operation = holder(repo.id)
    if operation:
        raise _refusal(operation)


async def wait_until_free(repo: Repository) -> None:
    """Wait (bounded) while an operation holds the repository: for a write
    that already staged its changes, which then land after the operation."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT_SECONDS
    while operation := holder(repo.id):
        if loop.time() >= deadline:
            raise _refusal(operation)
        await asyncio.sleep(POLL_SECONDS)


@contextmanager
def held(repo: Repository, operation: str):
    """Hold the repository for ``operation``; refuse (409) if another holds it."""
    token = acquire(repo.id, operation)
    if token is None:
        raise _refusal(holder(repo.id) or operation)
    try:
        yield
    finally:
        release(repo.id, token)
