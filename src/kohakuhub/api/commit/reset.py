"""Resetting a branch to a commit: a new commit whose tree equals the target.

No file content passes through this service: LFS files link their global
``lfs/<sha256>`` object again, regular files are copied inside the object
store, and files the target lacks are deleted in batches. The tree is built on
a scratch branch and squash-merged onto the branch in one step, so readers
never see a half-done reset. The result must equal the target exactly: when a
concurrent commit got in, the reset runs again from the new head (#99);
where the concurrent commit changed the same paths, the merge takes the
target's version, so only paths the reset did not touch need another round.

The database is brought up to date before the request returns, from what the
branch holds for the paths the reset changed; the versions it replaced are
left to garbage collection. Every commit merged is recorded, also when the
reset gives up or fails afterwards.
"""

import asyncio
import uuid
from datetime import datetime, timezone

import httpx
from peewee import EXCLUDED

from kohakuhub.api.commit import availability
from kohakuhub.api.commit.routers.operations import calculate_git_blob_sha1
from kohakuhub.api.quota.util import update_namespace_storage, update_repository_storage
from kohakuhub.config import cfg
from kohakuhub.db import File, LFSObjectHistory, LfsHeadRef, LfsObjectTombstone, Repository, User
from kohakuhub.db_operations import create_commit, get_organization, should_use_lfs
from kohakuhub.lfs_gc import (
    DELETED,
    LfsObjectUnavailable,
    claim_for_commit,
    drop_head_refs,
    lfs_key,
    lfs_oid,
    record_evicted_versions,
)
from kohakuhub.logger import get_logger
from kohakuhub.storage_cleanup import (
    SCRATCH_BRANCH_PREFIX,
    enqueue_lfs_collection,
    enqueue_lfs_reconciliation,
    record_head_change,
)
from kohakuhub.utils.s3 import object_exists

logger = get_logger("RESET")

ATTEMPTS = 3  # rounds of building and merging before giving up on a branch that keeps changing
DIRTY_WAITS = 3  # merges tried while the branch has uncommitted changes (an upload in flight)
RETRY_DELAY = 0.5  # seconds, times the try
DELETE_BATCH = 1000  # LakeFS bulk delete maximum
ROW_BATCH = 500
CLAIM_YIELD = 100  # claims between letting other requests in: each is a transaction


class ResetRefused(Exception):
    """The reset cannot go on; ``status`` and ``detail`` make the HTTP answer.

    ``rounds`` are the ``(commit, target entries)`` it merged before, which
    are on the branch and recorded like a finished reset's.
    """

    def __init__(self, status: int, detail: dict, rounds=None):
        super().__init__(detail["error"])
        self.status, self.detail, self.rounds = status, detail, rounds or []


def _merge_refusal(error: httpx.HTTPStatusError) -> str:
    """``conflict`` with a concurrent commit, ``dirty`` (an upload in flight),
    ``unchanged`` (the branch already holds the tree), or ``other``."""
    status, text = error.response.status_code, error.response.text
    if status == 409:
        return "conflict"
    if status == 400 and "dirty" in text:
        return "dirty"
    if status == 400 and "no changes" in text:
        return "unchanged"
    return "other"


async def _claim(wanted: dict[str, dict | None], target: str) -> None:
    """Every LFS object the target needs must be stored, and stays so while
    the reset links it (claimed like a commit's, see ``lfs_gc``)."""
    needed = {path: lfs_oid(e.get("physical_address")) for path, e in wanted.items() if e}
    needed = {path: oid for path, oid in needed.items() if oid}
    status = await availability.lfs_statuses(needed.values())
    lost = {oid for oid in set(needed.values()) if status[oid] != "available"}
    # Collected, then uploaded again: the claim revives it, as a commit's does
    for oid in sorted(oid for oid in lost if status[oid] == "collected"):
        if await object_exists(cfg.s3.bucket, lfs_key(oid)):
            lost.discard(oid)
    for n, oid in enumerate(sorted(set(needed.values()) - lost)):
        try:
            if claim_for_commit(oid, True) and not await object_exists(cfg.s3.bucket, lfs_key(oid)):
                # Revived, but gone since the check: it is collected after all
                LfsObjectTombstone.get_or_create(sha256=oid, defaults={"state": DELETED})
                raise LfsObjectUnavailable(oid)
        except LfsObjectUnavailable:
            lost.add(oid)
        if n % CLAIM_YIELD == CLAIM_YIELD - 1:
            await asyncio.sleep(0)
    missing = sorted(path for path, oid in needed.items() if oid in lost)
    if missing:
        raise ResetRefused(
            400,
            {
                "error": f"Cannot reset to commit {target[:8]}: {len(missing)} LFS file(s) are "
                f"no longer stored (garbage collected or missing): {', '.join(missing[:5])}"
                + (f" and {len(missing) - 5} more" if len(missing) > 5 else ""),
                "missing_files": missing,
                "recoverable": False,
            },
        )


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
        for attempt in range(DIRTY_WAITS):
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
                refusal = _merge_refusal(e)
                if refusal in ("conflict", "unchanged"):
                    logger.info(f"Reset of {lakefs_repo}@{branch} meets a concurrent change")
                    return None
                if refusal == "other":
                    if e.response.status_code < 500:  # protected branch, hooks, ...
                        raise ResetRefused(
                            e.response.status_code,
                            {"error": f"LakeFS refused the merge: {e.response.text}"},
                        )
                    raise
                # An upload in flight: the tree built is still right, wait for it
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
        raise ResetRefused(
            409,
            {"error": "The branch has uncommitted changes (an upload in progress?); try again."},
        )
    finally:
        try:
            await client.delete_branch(repository=lakefs_repo, branch=scratch)
        except Exception as e:
            logger.warning(f"Could not delete the scratch branch {scratch}: {e}")
        drop_head_refs(repo, scratch)  # in case a reconciliation listed it meanwhile


async def reset_branch(
    client, repo: Repository, lakefs_repo: str, branch: str, target: str, message: str
) -> tuple[str, list[tuple[str, dict]]]:
    """Make ``branch``'s tree equal ``target``'s with new commits on top of it.

    Returns the head and the rounds merged, ``(commit, target entries of the
    paths it changed)``. Raises ``ResetRefused``, carrying the rounds merged
    before, whatever went wrong after one.
    """
    head = (await client.get_branch(repository=lakefs_repo, branch=branch))["commit_id"]
    paths = await availability.changed_paths(client, lakefs_repo, head, target)
    if not paths:
        raise ResetRefused(400, {"error": "Branch is already at the target state"})
    rounds: list[tuple[str, dict]] = []
    try:
        for attempt in range(ATTEMPTS):
            wanted = await availability.entries(client, lakefs_repo, target, paths)
            await _claim(wanted, target)
            new = await _merge(client, repo, lakefs_repo, branch, head, target, wanted, message)
            if new is None:
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                head = (await client.get_branch(repository=lakefs_repo, branch=branch))["commit_id"]
            else:
                rounds.append((new, wanted))
                head = new
            # A concurrent commit may have got in: whatever differs, reset again
            paths = await availability.changed_paths(client, lakefs_repo, head, target)
            if not paths:
                return head, rounds
    except ResetRefused as e:
        if rounds:
            e.rounds = rounds
            e.detail["commits"] = [commit for commit, _ in rounds]
        raise
    except Exception as e:
        if not rounds:
            raise
        raise ResetRefused(
            500,
            {"error": f"Reset failed: {e}", "commits": [commit for commit, _ in rounds]},
            rounds,
        ) from e
    error = "The branch kept changing during the reset; try again."
    if rounds:
        error += " It holds the reset commit(s) made so far, plus the concurrent changes."
    raise ResetRefused(409, {"error": error, "commits": [c for c, _ in rounds]}, rounds)


async def _regular_ids(client, lakefs_repo: str, ref: str, paths: list[str]) -> dict:
    """Git blob ids of regular files, as commits record them (small by
    definition: bigger files are LFS). A file that cannot be read is left out."""
    limit = asyncio.Semaphore(cfg.lakefs.operation_concurrency)
    ids = {}

    async def blob(path):
        async with limit:
            try:
                content = await client.get_object(repository=lakefs_repo, ref=ref, path=path)
            except Exception as e:
                logger.warning(f"Could not read {path} to record its id: {e}")
                return
            ids[path] = calculate_git_blob_sha1(content)

    await asyncio.gather(*(blob(path) for path in paths))
    return ids


def _batches(items: list, size: int = ROW_BATCH):
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def record_reset(
    client,
    lakefs_repo: str,
    repo: Repository,
    branch: str,
    target: str,
    rounds: list[tuple[str, dict]],
    user: User,
    message: str,
) -> None:
    """Bring the database up to date for the paths the reset changed.

    Recorded from what the branch holds now, so a concurrent commit's changes
    to the same paths are not overwritten: head references first (what
    garbage collection must keep), commit rows, File rows, then each round's
    LFS history. What the head no longer links, and versions pushed out of a
    keep window, become collection candidates; nothing waits for the
    collection. A failure is logged and queues a reconciliation.
    """
    if not rounds:
        return  # the branch came to equal the target on its own
    try:
        changed = sorted(set().union(*(wanted for _, wanted in rounds)))
        head = (await client.get_branch(repository=lakefs_repo, branch=branch))["commit_id"]
        actual = await availability.entries(client, lakefs_repo, head, changed)
        oids = {path: lfs_oid(e.get("physical_address")) for path, e in actual.items() if e}
        lfs_now = {path: oid for path, oid in oids.items() if oid}
        R = LfsHeadRef
        replaced = set()  # LFS paths whose version changed: keep windows to review
        for batch in _batches(list(lfs_now)):
            replaced.update(
                path
                for (path,) in R.select(R.path_in_repo)
                .where((R.repository == repo) & (R.branch == branch) & R.path_in_repo.in_(batch))
                .tuples()
            )
        record_head_change(repo, branch, {path: lfs_now.get(path) for path in changed}, [])
        for commit_id, _ in rounds:
            create_commit(
                commit_id=commit_id,
                repository=repo,
                repo_type=repo.repo_type,
                branch=branch,
                author=user,
                username=user.username,
                message=message,
                description=f"Reset to {target}",
            )

        present = {path: e for path, e in actual.items() if e is not None}
        regular = [
            path
            for path, e in present.items()
            if path not in lfs_now and not should_use_lfs(repo, path, e.get("size_bytes", 0))
        ]
        blob_ids = await _regular_ids(client, lakefs_repo, head, regular)
        now = datetime.now(timezone.utc)
        rows = []
        for path, e in present.items():
            if path in regular and path not in blob_ids:
                continue  # unreadable: its row stays as it was
            rows.append(
                {
                    "repository": repo,
                    "path_in_repo": path,
                    "size": e.get("size_bytes", 0),
                    # LFS: the object; regular: the git blob; big files kept
                    # outside lfs/ (older resets): the recorded checksum
                    "sha256": lfs_now.get(path)
                    or blob_ids.get(path)
                    or e.get("checksum", "").split(":", 1)[-1],
                    "lfs": path not in regular,
                    "is_deleted": False,
                    "owner": repo.owner,
                }
            )
        for batch in _batches(rows):
            File.insert_many(batch).on_conflict(
                conflict_target=(File.repository, File.path_in_repo),
                update={
                    File.sha256: EXCLUDED.sha256,
                    File.size: EXCLUDED.size,
                    File.lfs: EXCLUDED.lfs,
                    File.is_deleted: False,
                    File.updated_at: now,
                },
            ).execute()
            await asyncio.sleep(0)
        for batch in _batches([path for path, e in actual.items() if e is None]):
            File.update(is_deleted=True, updated_at=now).where(
                (File.repository == repo) & File.path_in_repo.in_(batch)
            ).execute()
            await asyncio.sleep(0)

        # Each round's LFS versions, attributed to the commit that merged them
        file_ids = {}
        for batch in _batches(list(lfs_now)):
            file_ids.update(
                File.select(File.path_in_repo, File.id)
                .where((File.repository == repo) & File.path_in_repo.in_(batch))
                .tuples()
            )
        history = [
            {
                "repository": repo,
                "path_in_repo": path,
                "sha256": lfs_oid(e["physical_address"]),
                "size": e.get("size_bytes", 0),
                "commit_id": commit_id,
                "file": file_ids.get(path),
            }
            for commit_id, wanted in rounds
            for path, e in wanted.items()
            if e is not None and lfs_oid(e.get("physical_address"))
        ]
        for batch in _batches(history):
            LFSObjectHistory.insert_many(batch).execute()
            await asyncio.sleep(0)
        if record_evicted_versions(repo, sorted(replaced)):
            enqueue_lfs_collection()
    except Exception as e:
        logger.exception(f"Could not record the reset of {repo.full_id}@{branch}", e)
        enqueue_lfs_reconciliation()
        return
    try:
        await update_repository_storage(repo)
        namespace = repo.namespace
        await update_namespace_storage(namespace, get_organization(namespace) is not None)
    except Exception as e:
        logger.warning(f"Failed to update storage usage for {repo.full_id}: {e}")
