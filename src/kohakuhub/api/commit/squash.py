"""Super Squash: a branch's history becomes one commit, in place.

The new commit holds the branch head's tree (its metarange) and has no
parent. It is stored with LakeFS's commit record API and the branch is
hard-reset to it. Nothing is copied or moved: the LakeFS repository, its
objects and the repository's name all stay, so a squash costs the same for
any repository size and the repository never disappears meanwhile.

A hard reset is not conditional, so the repository is held while it runs
(``operation_lock``): writes wait for the squash and the squash waits for
the writes already under way.

Squashing a repository (``whole_repository``) also deletes its other
branches and tags, as it always has: only the current state is kept, and
the commits the squash commit does not reach are out of the history
(``Repository.history_root``; nothing reads or restores them). In the
background, ``storage.forget_squashed_history`` then forgets their history
and file rows, deletes the regular file objects only they had, and leaves
their LFS objects to garbage collection. Squashing one branch (Hugging
Face's ``super_squash_history``) keeps the other branches and tags, and
the history they reach.
"""

import hashlib
import struct
import time
from datetime import datetime, timedelta, timezone

import httpx

from kohakuhub import tasks, usage
from kohakuhub.api.commit.records import OperationRefused, refused_by_lakefs
from kohakuhub.api.repo.utils import operation_lock
from kohakuhub.db import (
    Commit,
    LFSObjectHistory,
    LfsHeadRef,
    Repository,
    User,
    db,
    utcnow,
)
from kohakuhub.db_operations import create_commit
from kohakuhub.logger import get_logger
from kohakuhub.storage_cleanup import FORGET_SQUASHED_KIND, release_objects

logger = get_logger("SQUASH")

SQUASH_MESSAGE = "Squash history"
FORGET_DELAY = timedelta(minutes=5)
MOVE_TRIES = 3  # the head moved between reading it and the reset
PAGE = 1000


def _int64(h, value: int) -> None:
    h.update(bytes([2, 8]) + struct.pack(">q", value))


def _string(h, value: str) -> None:
    data = value.encode()
    h.update(bytes([1]))
    _int64(h, len(data))
    h.update(data)


def commit_address(
    committer: str,
    message: str,
    metarange_id: str,
    creation_date: int,
    metadata: dict[str, str],
    parents: list[str],
) -> str:
    """The id LakeFS gives a commit: its content address.

    Mirrors ``Commit.Identity`` in LakeFS's pkg/graveler with the typed
    encoding of pkg/ident; LakeFS refuses a commit record under another id.
    """
    parents_hash = hashlib.sha256()
    parents_hash.update(bytes([3]))  # a string slice
    _int64(parents_hash, len(parents))
    for parent in parents:
        _string(parents_hash, parent)
    h = hashlib.sha256()
    for value in ("commit:v1", committer, message, metarange_id):
        _string(h, value)
    _int64(h, creation_date)
    h.update(bytes([4]))  # a string map, sorted by key
    _int64(h, len(metadata))
    for key in sorted(metadata):
        _string(h, key)
        _string(h, metadata[key])
    parents_identity = parents_hash.digest()
    h.update(bytes([5, 0]))  # an embedded identity, as bytes
    _int64(h, len(parents_identity))
    h.update(parents_identity)
    return h.hexdigest()


async def _head(client, lakefs_repo: str, branch: str) -> str:
    try:
        return (await client.get_branch(repository=lakefs_repo, branch=branch))[
            "commit_id"
        ]
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise OperationRefused(404, {"error": f"Branch not found: {branch}"})
        refused_by_lakefs(e, "squash")


async def _move(client, lakefs_repo: str, branch: str, committer: str, message: str):
    """Point ``branch`` at a parentless commit of its head's tree:
    ``(commit, head it replaced)``."""
    for _ in range(MOVE_TRIES):
        head = await _head(client, lakefs_repo, branch)
        metarange = (await client.get_commit(repository=lakefs_repo, commit_id=head))[
            "meta_range_id"
        ]
        created = int(time.time())
        metadata = {"kh_operation": "squash", "kh_squashed": head}
        commit = commit_address(committer, message, metarange, created, metadata, [])
        try:
            await client.create_commit_record(
                repository=lakefs_repo,
                commit_id=commit,
                committer=committer,
                message=message,
                metarange_id=metarange,
                creation_date=created,
                parents=[],
                metadata=metadata,
                generation=1,
            )
            if await _head(client, lakefs_repo, branch) != head:
                continue  # it moved meanwhile: squash the new head instead
            await client.hard_reset_branch(
                repository=lakefs_repo, branch=branch, ref=commit
            )
        except httpx.HTTPStatusError as e:
            refused_by_lakefs(e, "squash")
        return commit, head
    raise OperationRefused(409, {"error": f"{branch} kept changing; try again."})


async def _names(list_page, key: str = "id") -> list[str]:
    names, after = [], None
    while True:
        page = await list_page(after=after, amount=PAGE)
        names += [item[key] for item in page["results"]]
        if not page["pagination"]["has_more"]:
            return names
        after = page["pagination"]["next_offset"]


async def _drop_other_refs(
    client, lakefs_repo: str, branch: str, renew, dropped: list[str]
) -> None:
    """Delete every branch but ``branch``, and every tag, adding their names
    to ``dropped`` as they go; the lock is renewed after each one."""
    branches = await _names(
        lambda **kw: client.list_branches(repository=lakefs_repo, **kw)
    )
    tags = await _names(lambda **kw: client.list_tags(repository=lakefs_repo, **kw))
    for name in branches:
        if name != branch:
            await client.delete_branch(repository=lakefs_repo, branch=name)
            dropped.append(name)
            renew()
    for name in tags:
        await client.delete_tag(repository=lakefs_repo, tag=name)
        dropped.append(f"tag:{name}")
        renew()


def _record(
    repo: Repository,
    branch: str,
    commit: str,
    head: str,
    author: User,
    message: str,
    dropped: list[str],
    whole_repository: bool,
) -> None:
    """The database, as the squash left the repository (one transaction).

    ``whole_repository`` only when every other ref is gone: the commits the
    squash commit does not reach are then out of the history.
    """
    with db.atomic():
        gone = [name for name in dropped if not name.startswith("tag:")]
        if gone:
            H = LfsHeadRef
            refs = (H.repository == repo) & H.branch.in_(gone)
            released = {sha for (sha,) in H.select(H.sha256).where(refs).tuples()}
            H.delete().where(refs).execute()
            release_objects(released)  # what only the dropped branches linked
        if whole_repository:
            Commit.delete().where(Commit.repository == repo).execute()
            Repository.update(history_root=commit).where(
                Repository.id == repo.id
            ).execute()
            through = (
                LFSObjectHistory.select(LFSObjectHistory.id)
                .where(LFSObjectHistory.repository == repo)
                .order_by(LFSObjectHistory.id.desc())
                .scalar()
            )
            tasks.enqueue(
                FORGET_SQUASHED_KIND,
                {
                    "repo_id": repo.id,
                    "commit": commit,
                    "through": through or 0,
                    "at": datetime.now(
                        timezone.utc
                    ).isoformat(),  # as file rows are dated
                },
                # Uploads in flight at the squash are committed or staged by then
                run_after=utcnow() + FORGET_DELAY,
            )
        create_commit(
            commit_id=commit,
            repository=repo,
            repo_type=repo.repo_type,
            branch=branch,
            author=author,
            username=author.username,
            message=message,
            description=f"Squashed {head}",
        )
        if branch == usage.MAIN:
            usage.main_moved(repo.id, head, commit, 0)  # the same tree


async def squash(
    client,
    repo: Repository,
    lakefs_repo: str,
    branch: str,
    author: User,
    message: str,
    *,
    whole_repository: bool,
) -> str:
    """Squash ``branch`` (and drop the other refs if ``whole_repository``);
    the new commit. Raises ``OperationRefused``."""
    with operation_lock.held(repo, "squash") as token:
        await operation_lock.drain(repo, "squash")
        commit, head = await _move(
            client, lakefs_repo, branch, author.username, message
        )
        dropped, failure = [], None
        if whole_repository:
            try:
                await _drop_other_refs(
                    client,
                    lakefs_repo,
                    branch,
                    lambda: operation_lock.renew(repo.id, token),
                    dropped,
                )
            except httpx.HTTPStatusError as e:
                failure = e
        _record(
            repo,
            branch,
            commit,
            head,
            author,
            message,
            dropped,
            whole_repository and failure is None,
        )
    if failure is not None:
        logger.warning(
            f"Squashed {repo.full_id}@{branch}, but not all refs went: {failure}"
        )
        raise OperationRefused(
            502,
            {
                "error": f"{branch} was squashed, but deleting the other branches and tags "
                f"failed ({failure.response.status_code}); squash again to finish.",
                "dropped": dropped,
            },
        )
    logger.success(
        f"Squashed {repo.full_id}@{branch}: {head[:8]} -> {commit[:8]}"
        + (f", dropped {len(dropped)} ref(s)" if dropped else "")
    )
    return commit
