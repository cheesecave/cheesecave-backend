"""Resetting a branch to a commit: a new commit whose tree is the target's.

LakeFS commits the target's own metarange onto the branch (``source_metarange``):
one commit on top of the head, applied atomically, and no object is copied or
linked, so it works whatever the object store's addressing (#133). LFS
objects the target needs and the head lacks are claimed first, so garbage
collection cannot delete them meanwhile. A commit that lands concurrently
becomes the reset commit's parent: the result still equals the target, and
the paths that commit changed are claimed and recorded too.

The initial commit has no metarange (an empty tree): a scratch branch with
every file deleted provides one.

Every commit made is recorded (``records.record_commits``), also when the
reset fails afterwards.
"""

import asyncio
import uuid

import httpx

from kohakuhub.api.commit import availability, records
from kohakuhub.api.commit.records import OperationRefused
from kohakuhub.db import Repository
from kohakuhub.lfs_gc import drop_head_refs
from kohakuhub.logger import get_logger
from kohakuhub.storage_cleanup import SCRATCH_BRANCH_PREFIX, enqueue_lfs_reconciliation

logger = get_logger("RESET")

DELETE_BATCH = 1000  # LakeFS bulk delete maximum


async def _empty_metarange(
    client, repo: Repository, lakefs_repo: str, head: str, paths: list[str]
) -> str:
    """The metarange of an empty tree, which the initial commit lacks: every
    file of ``head`` (``paths``) deleted on a scratch branch and committed."""
    scratch = f"{SCRATCH_BRANCH_PREFIX}{uuid.uuid4().hex[:16]}"
    await client.create_branch(repository=lakefs_repo, name=scratch, source=head)
    try:
        for start in range(0, len(paths), DELETE_BATCH):
            await client.delete_objects(lakefs_repo, scratch, paths[start : start + DELETE_BATCH])
        empty = await client.commit(lakefs_repo, scratch, message="Empty tree for a reset")
        return empty["meta_range_id"]
    finally:
        try:
            await client.delete_branch(repository=lakefs_repo, branch=scratch)
        except Exception as e:
            logger.warning(f"Could not delete the scratch branch {scratch}: {e}")
        try:  # in case a reconciliation listed it meanwhile
            drop_head_refs(repo, scratch)
        except Exception as e:
            logger.warning(f"Could not drop the scratch branch's references: {e}")
            enqueue_lfs_reconciliation()


async def _commit_tree(
    client, lakefs_repo: str, branch: str, metarange: str, target: str, message: str
) -> dict:
    """Commit ``metarange`` onto ``branch``, waiting out an upload in flight."""
    for attempt in range(records.DIRTY_WAITS):
        try:
            return await client.commit(
                lakefs_repo,
                branch,
                message=message,
                metadata={"reset_to": target},
                source_metarange=metarange,
            )
        except httpx.HTTPStatusError as e:
            if records.refusal(e) != "dirty":
                records.refused_by_lakefs(e, "commit")
            if attempt < records.DIRTY_WAITS - 1:
                await asyncio.sleep(records.RETRY_DELAY * (attempt + 1))
    raise OperationRefused(
        409,
        {"error": "The branch has uncommitted changes (an upload in progress?); try again."},
    )


async def reset_branch(
    client, repo: Repository, lakefs_repo: str, branch: str, target: str, message: str
) -> tuple[str, list[tuple[str, dict]]]:
    """Make ``branch``'s tree equal ``target``'s with one new commit on top.

    Returns that commit and ``[(commit, target entries of the paths it
    changed)]``. Raises ``OperationRefused``, carrying the commit once it is
    on the branch, whatever went wrong after it.
    """
    head = (await client.get_branch(repository=lakefs_repo, branch=branch))["commit_id"]
    paths = await availability.changed_paths(client, lakefs_repo, head, target)
    if not paths:
        raise OperationRefused(400, {"error": "Branch is already at the target state"})
    action = f"reset to commit {target[:8]}"
    wanted = await availability.entries(client, lakefs_repo, target, paths)
    await records.claim_objects(wanted, action)
    # Only ever the target's own metarange: LakeFS commits any id it is given,
    # and a branch committed with a missing one cannot be read any more
    metarange = (await client.get_commit(repository=lakefs_repo, commit_id=target))["meta_range_id"]
    if not metarange:
        metarange = await _empty_metarange(client, repo, lakefs_repo, head, paths)
    made = await _commit_tree(client, lakefs_repo, branch, metarange, target, message)
    rounds = [(made["id"], wanted)]
    parent = made["parents"][0]
    if parent == head:
        return made["id"], rounds
    # A concurrent commit landed first: the reset changed its paths as well
    logger.info(f"Reset of {lakefs_repo}@{branch} committed on top of a concurrent change")
    try:
        paths = await availability.changed_paths(client, lakefs_repo, parent, target)
        rounds = [(made["id"], await availability.entries(client, lakefs_repo, target, paths))]
        await records.claim_objects(rounds[0][1], action)
    except OperationRefused as e:
        e.rounds, e.detail["commits"] = rounds, [made["id"]]
        raise
    except Exception as e:
        logger.exception(f"Reset of {lakefs_repo}@{branch} failed after committing", e)
        # The concurrent commit's paths are not known: record what branches link
        enqueue_lfs_reconciliation()
        raise OperationRefused(
            500, {"error": f"Reset failed: {e}", "commits": [made["id"]]}, rounds
        ) from e
    return made["id"], rounds
