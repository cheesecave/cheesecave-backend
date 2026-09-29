"""Resetting a branch to a commit: a new commit whose tree equals the target.

No file content passes through this service: LFS files link their global
``lfs/<sha256>`` object again, regular files are copied inside the object
store, and files the target lacks are deleted in batches. The tree is built on
a scratch branch and squash-merged onto the branch in one step, so readers
never see a half-done reset. The result must equal the target exactly: when a
concurrent commit got in, the reset runs again from the new head (#99);
where the concurrent commit changed the same paths, the merge takes the
target's version, so only paths the reset did not touch need another round.

Every commit merged is recorded (``records.record_commits``), also when the
reset gives up or fails afterwards.
"""

import asyncio
import uuid

import httpx

from kohakuhub.api.commit import availability, records
from kohakuhub.api.commit.records import OperationRefused
from kohakuhub.config import cfg
from kohakuhub.db import Repository
from kohakuhub.lfs_gc import drop_head_refs, lfs_oid
from kohakuhub.logger import get_logger
from kohakuhub.storage_cleanup import SCRATCH_BRANCH_PREFIX, enqueue_lfs_reconciliation

logger = get_logger("RESET")

ATTEMPTS = 3  # rounds of building and merging before giving up on a branch that keeps changing
DELETE_BATCH = 1000  # LakeFS bulk delete maximum


async def _gather(coroutines) -> None:
    """Run all to the end, then raise the first failure: nothing may still be
    writing to the scratch branch once it is dropped."""
    for result in await asyncio.gather(*coroutines, return_exceptions=True):
        if isinstance(result, BaseException):
            raise result


async def _build(client, lakefs_repo: str, scratch: str, target: str, wanted: dict) -> None:
    limit = asyncio.Semaphore(cfg.lakefs.operation_concurrency)

    async def place(path, entry):
        async with limit:
            if lfs_oid(entry.get("physical_address")):
                # As commits link it: the global object, its checksum and size
                metadata = {
                    "staging": {"physical_address": entry["physical_address"]},
                    "checksum": entry["checksum"],
                    "size_bytes": entry["size_bytes"],
                }
                await client.link_physical_address(lakefs_repo, scratch, path, metadata)
            else:
                await client.copy_object(lakefs_repo, scratch, path, target, path)

    await _gather(place(path, entry) for path, entry in wanted.items() if entry is not None)
    removed = sorted(path for path, entry in wanted.items() if entry is None)
    for start in range(0, len(removed), DELETE_BATCH):
        await client.delete_objects(lakefs_repo, scratch, removed[start : start + DELETE_BATCH])


async def _merge(
    client,
    repo: Repository,
    lakefs_repo: str,
    branch: str,
    head: str,
    target: str,
    wanted: dict,
    message: str,
) -> str | None:
    """Build the target's tree on a scratch branch from ``head`` and squash-merge
    it onto ``branch``; the new commit, or ``None`` to build again from the
    branch's new head (a conflict, or the branch holds the tree already)."""
    scratch = f"{SCRATCH_BRANCH_PREFIX}{uuid.uuid4().hex[:16]}"
    await client.create_branch(repository=lakefs_repo, name=scratch, source=head)
    try:
        await _build(client, lakefs_repo, scratch, target, wanted)
        metadata = {"reset_to": target}
        await client.commit(lakefs_repo, scratch, message=message, metadata=metadata)
        for attempt in range(records.DIRTY_WAITS):
            try:
                merged = await client.merge_into_branch(
                    repository=lakefs_repo,
                    source_ref=scratch,
                    destination_branch=branch,
                    message=message,
                    metadata=metadata,
                    squash_merge=True,
                    # A path a concurrent commit changed too takes the
                    # target's version: the reset is to equal the target
                    strategy="source-wins",
                )
                return merged["reference"]
            except httpx.HTTPStatusError as e:
                why = records.refusal(e)
                if why in ("conflict", "unchanged"):
                    logger.info(f"Reset of {lakefs_repo}@{branch} meets a concurrent change")
                    return None
                if why == "other":
                    records.refused_by_lakefs(e, "merge")
                # An upload in flight: the tree built is still right, wait for it
                if attempt < records.DIRTY_WAITS - 1:
                    await asyncio.sleep(records.RETRY_DELAY * (attempt + 1))
        raise OperationRefused(
            409,
            {"error": "The branch has uncommitted changes (an upload in progress?); try again."},
        )
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


async def reset_branch(
    client, repo: Repository, lakefs_repo: str, branch: str, target: str, message: str
) -> tuple[str, list[tuple[str, dict]]]:
    """Make ``branch``'s tree equal ``target``'s with new commits on top of it.

    Returns the head and the rounds merged, ``(commit, target entries of the
    paths it changed)``. Raises ``OperationRefused``, carrying the rounds merged
    before, whatever went wrong after one.
    """
    head = (await client.get_branch(repository=lakefs_repo, branch=branch))["commit_id"]
    paths = await availability.changed_paths(client, lakefs_repo, head, target)
    if not paths:
        raise OperationRefused(400, {"error": "Branch is already at the target state"})
    rounds: list[tuple[str, dict]] = []
    try:
        for attempt in range(ATTEMPTS):
            wanted = await availability.entries(client, lakefs_repo, target, paths)
            await records.claim_objects(wanted, f"reset to commit {target[:8]}")
            new = await _merge(client, repo, lakefs_repo, branch, head, target, wanted, message)
            if new is None:
                await asyncio.sleep(records.RETRY_DELAY * (attempt + 1))
                head = (await client.get_branch(repository=lakefs_repo, branch=branch))["commit_id"]
            else:
                rounds.append((new, wanted))
                head = new
            # A concurrent commit may have got in: whatever differs, reset again
            paths = await availability.changed_paths(client, lakefs_repo, head, target)
            if not paths:
                return head, rounds
    except OperationRefused as e:
        if rounds:
            e.rounds = rounds
            e.detail["commits"] = [commit for commit, _ in rounds]
        raise
    except Exception as e:
        if not rounds:
            raise
        logger.exception(f"Reset of {lakefs_repo}@{branch} failed after merging", e)
        raise OperationRefused(
            500,
            {"error": f"Reset failed: {e}", "commits": [commit for commit, _ in rounds]},
            rounds,
        ) from e
    error = "The branch kept changing during the reset; try again."
    if rounds:
        error += " It holds the reset commit(s) made so far, plus the concurrent changes."
    raise OperationRefused(409, {"error": error, "commits": [c for c, _ in rounds]}, rounds)
