"""Resetting a branch to a commit: a new commit whose tree equals the target.

No file content passes through this service: LFS files link their global
``lfs/<sha256>`` object again, regular files are copied inside the object
store, and files the target lacks are deleted in batches. The tree is built on
a scratch branch and squash-merged onto the branch in one step, so readers
never see a half-done reset. The result must equal the target exactly: when a
concurrent commit got in, the reset runs again from the new head (#99).

The database is brought up to date before the request returns, for the paths
the reset changed; the versions it replaced are left to garbage collection.
"""

import asyncio
import uuid
from datetime import datetime, timezone

import httpx
from peewee import EXCLUDED

from kohakuhub.api.commit import availability
from kohakuhub.api.commit.routers.operations import calculate_git_blob_sha1
from kohakuhub.config import cfg
from kohakuhub.db import File, LFSObjectHistory, Repository, User
from kohakuhub.db_operations import create_commit, should_use_lfs
from kohakuhub.lfs_gc import (
    LfsObjectUnavailable,
    claim_for_commit,
    lfs_key,
    lfs_oid,
    record_evicted_versions,
)
from kohakuhub.logger import get_logger
from kohakuhub.storage_cleanup import (
    enqueue_lfs_collection,
    enqueue_lfs_reconciliation,
    record_head_change,
)
from kohakuhub.utils.s3 import object_exists

logger = get_logger("RESET")

ATTEMPTS = 3  # merges before giving up on a branch that keeps changing
RETRY_DELAY = 0.5  # seconds, times the attempt: an upload in flight finishing
DELETE_BATCH = 1000  # LakeFS bulk delete maximum
ROW_BATCH = 500


class ResetRefused(Exception):
    """The reset cannot go on; ``status`` and ``detail`` make the HTTP answer.

    ``changed`` and ``commits`` are what it merged before giving up, to be
    recorded like a finished reset's.
    """

    def __init__(self, status: int, detail: dict, changed=None, commits=None):
        super().__init__(detail["error"])
        self.status, self.detail = status, detail
        self.changed, self.commits = changed or {}, commits or []


def _retry_merge(error: httpx.HTTPStatusError) -> bool:
    """A conflict with a concurrent commit, or an upload in flight on the branch."""
    response = error.response
    return response.status_code == 409 or (response.status_code == 400 and "dirty" in response.text)


async def _claim(wanted: dict[str, dict | None], target: str) -> None:
    """Every LFS object the target needs must be stored, and stays so while
    the reset links it (claimed like a commit's, see ``lfs_gc``)."""
    needed = {path: lfs_oid(e.get("physical_address")) for path, e in wanted.items() if e}
    needed = {path: oid for path, oid in needed.items() if oid}
    status = await availability.lfs_statuses(needed.values())
    lost = {oid for oid in set(needed.values()) if status[oid] != "available"}
    for oid in sorted(set(needed.values()) - lost):
        try:
            # Revived from a tombstone: the bucket may have lost it since the check
            if claim_for_commit(oid, True) and not await object_exists(cfg.s3.bucket, lfs_key(oid)):
                raise LfsObjectUnavailable(oid)
        except LfsObjectUnavailable:
            lost.add(oid)
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
    client, lakefs_repo: str, branch: str, head: str, target: str, wanted: dict, message: str
) -> str | None:
    """Build the target's tree on a scratch branch from ``head`` and squash-merge
    it onto ``branch``; the new commit, or ``None`` when the merge must be retried."""
    scratch = f"kh-reset-{uuid.uuid4().hex[:16]}"
    await client.create_branch(repository=lakefs_repo, name=scratch, source=head)
    try:
        await _build(client, lakefs_repo, scratch, target, wanted)
        metadata = {"reset_to": target}
        await client.commit(lakefs_repo, scratch, message=message, metadata=metadata)
        try:
            merged = await client.merge_into_branch(
                repository=lakefs_repo,
                source_ref=scratch,
                destination_branch=branch,
                message=message,
                metadata=metadata,
                squash_merge=True,
            )
        except httpx.HTTPStatusError as e:
            if not _retry_merge(e):
                raise
            logger.info(f"Reset of {lakefs_repo}@{branch} meets a concurrent change; again")
            return None
        return merged["reference"]
    finally:
        try:
            await client.delete_branch(repository=lakefs_repo, branch=scratch)
        except Exception as e:
            logger.warning(f"Could not delete the scratch branch {scratch}: {e}")


async def reset_branch(
    client, lakefs_repo: str, branch: str, target: str, message: str
) -> tuple[str, dict[str, dict | None], list[str]]:
    """Make ``branch``'s tree equal ``target``'s with new commits on top of it.

    Returns the head, every changed path with the target's entry (``None``:
    deleted), and the commits made. Raises ``ResetRefused``.
    """
    head = (await client.get_branch(repository=lakefs_repo, branch=branch))["commit_id"]
    paths = await availability.changed_paths(client, lakefs_repo, head, target)
    if not paths:
        raise ResetRefused(400, {"error": "Branch is already at the target state"})
    changed: dict[str, dict | None] = {}
    commits: list[str] = []
    for attempt in range(ATTEMPTS):
        wanted = await availability.entries(client, lakefs_repo, target, paths)
        await _claim(wanted, target)
        new = await _merge(client, lakefs_repo, branch, head, target, wanted, message)
        if new is None:
            await asyncio.sleep(RETRY_DELAY * (attempt + 1))
            head = (await client.get_branch(repository=lakefs_repo, branch=branch))["commit_id"]
        else:
            changed.update(wanted)
            commits.append(new)
            head = new
        # A concurrent commit may have got in: whatever differs, reset again
        paths = await availability.changed_paths(client, lakefs_repo, head, target)
        if not paths:
            return head, changed, commits
    error = "The branch kept changing during the reset; try again."
    if commits:
        error += " It holds the reset commit(s) made so far, plus the concurrent changes."
    raise ResetRefused(409, {"error": error, "commits": commits}, changed, commits)


async def _regular_ids(client, lakefs_repo: str, target: str, paths: list[str]) -> dict:
    """Git blob ids of regular files, as commits record them (small by
    definition: bigger files are LFS)."""
    limit = asyncio.Semaphore(cfg.lakefs.operation_concurrency)
    ids = {}

    async def blob(path):
        async with limit:
            content = await client.get_object(repository=lakefs_repo, ref=target, path=path)
            ids[path] = calculate_git_blob_sha1(content)

    await _gather(blob(path) for path in paths)
    return ids


async def record_reset(
    client,
    lakefs_repo: str,
    repo: Repository,
    branch: str,
    target: str,
    changed: dict[str, dict | None],
    commits: list[str],
    user: User,
    message: str,
) -> None:
    """Bring the database up to date for the paths the reset changed.

    What the head no longer links, and the versions pushed out of a keep
    window, become collection candidates; nothing waits for the collection.
    A failure is logged and queues a reconciliation: the reset happened.
    """
    if not commits:
        return  # the branch came to equal the target on its own
    try:
        head = commits[-1]
        present = {path: e for path, e in changed.items() if e is not None}
        oids = {path: lfs_oid(e.get("physical_address")) for path, e in present.items()}
        regular = [
            path
            for path, e in present.items()
            if not oids[path] and not should_use_lfs(repo, path, e.get("size_bytes", 0))
        ]
        blob_ids = await _regular_ids(client, lakefs_repo, target, regular)
        now = datetime.now(timezone.utc)
        rows = []
        for path, e in present.items():
            size = e.get("size_bytes", 0)
            checksum = e.get("checksum", "")
            identity = oids[path] or blob_ids.get(path) or checksum.split(":", 1)[-1]
            is_lfs = bool(oids[path]) or path not in blob_ids
            rows.append(
                {
                    "repository": repo,
                    "path_in_repo": path,
                    "size": size,
                    "sha256": identity,
                    "lfs": is_lfs,
                    "is_deleted": False,
                    "owner": repo.owner,
                }
            )
        for start in range(0, len(rows), ROW_BATCH):
            File.insert_many(rows[start : start + ROW_BATCH]).on_conflict(
                conflict_target=(File.repository, File.path_in_repo),
                update={
                    File.sha256: EXCLUDED.sha256,
                    File.size: EXCLUDED.size,
                    File.lfs: EXCLUDED.lfs,
                    File.is_deleted: False,
                    File.updated_at: now,
                },
            ).execute()
        removed = [path for path, e in changed.items() if e is None]
        for start in range(0, len(removed), ROW_BATCH):
            File.update(is_deleted=True, updated_at=now).where(
                (File.repository == repo)
                & File.path_in_repo.in_(removed[start : start + ROW_BATCH])
            ).execute()
        lfs_paths = {path: oid for path, oid in oids.items() if oid}
        history = [
            {
                "repository": repo,
                "path_in_repo": path,
                "sha256": oid,
                "size": present[path].get("size_bytes", 0),
                "commit_id": head,
            }
            for path, oid in lfs_paths.items()
        ]
        for start in range(0, len(history), ROW_BATCH):
            LFSObjectHistory.insert_many(history[start : start + ROW_BATCH]).execute()
        if record_evicted_versions(repo, list(lfs_paths)):
            enqueue_lfs_collection()
        record_head_change(repo, branch, {path: lfs_paths.get(path) for path in changed}, [])
        for commit_id in commits:
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
    except Exception as e:
        logger.exception(f"Could not record the reset of {repo.full_id}@{branch}", e)
        enqueue_lfs_reconciliation()
