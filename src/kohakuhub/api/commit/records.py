"""What branch operations (reset, revert, merge) share: claiming the LFS
objects a new commit links, telling LakeFS's refusals apart, and recording
the commits they make.

Records are brought up to date before the request returns, from what the
branch holds for the paths the commits changed; the versions they replaced
are left to garbage collection, which runs in the background.
"""

import asyncio
from datetime import datetime, timezone

import httpx
from peewee import EXCLUDED

from kohakuhub import usage
from kohakuhub.api.commit import availability
from kohakuhub.api.commit.routers.operations import calculate_git_blob_sha1
from kohakuhub.config import cfg
from kohakuhub.db import File, LFSObjectHistory, LfsHeadRef, Repository, User, db
from kohakuhub.db_operations import create_commit, should_use_lfs
from kohakuhub.lfs_gc import (
    LfsObjectUnavailable,
    claim_for_commit,
    collected_after_all,
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

logger = get_logger("RECORDS")

DIRTY_WAITS = 3  # tries while the branch has uncommitted changes (an upload in flight)
RETRY_DELAY = 0.5  # seconds, times the try
ROW_BATCH = 500
CLAIM_YIELD = 100  # claims between letting other requests in: each is a transaction


class OperationRefused(Exception):
    """The operation cannot go on; ``status`` and ``detail`` make the HTTP answer.

    ``rounds`` are the ``(commit, entries)`` it committed before, which are
    on the branch and recorded like a finished operation's.
    """

    def __init__(self, status: int, detail: dict, rounds=None):
        super().__init__(detail["error"])
        self.status, self.detail, self.rounds = status, detail, rounds or []


def refusal(error: httpx.HTTPStatusError) -> str:
    """Why LakeFS refused a revert or a reset's commit: ``conflict`` with a
    concurrent commit, ``dirty`` (an upload in flight), ``unchanged`` (nothing
    to change), or ``other``."""
    status, text = error.response.status_code, error.response.text
    if status == 409:
        return "conflict"
    # "uncommitted changes": a commit of a given metarange (a reset)
    if status == 400 and ("dirty" in text or "uncommitted changes" in text):
        return "dirty"
    if status == 400 and "no changes" in text:
        return "unchanged"
    return "other"


def refused_by_lakefs(error: httpx.HTTPStatusError, what: str) -> None:
    """Raise what LakeFS refused (protected branch, hooks, ...) with its own
    status; a server error is raised as it is."""
    if error.response.status_code < 500:
        raise OperationRefused(
            error.response.status_code,
            {"error": f"LakeFS refused the {what}: {error.response.text}"},
        )
    raise error


def shown(paths: list[str]) -> str:
    return ", ".join(paths[:5]) + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")


async def claim_objects(required: dict[str, dict | None], action: str, optional=()) -> None:
    """Every LFS object of ``required`` (path: entry) must be stored, and
    stays so while the operation links it (claimed like a commit's, see
    ``lfs_gc``); ``optional`` entries are claimed too where they are stored.
    Raises ``OperationRefused`` naming the missing paths: "Cannot {action}"."""
    needed = {path: lfs_oid(e.get("physical_address")) for path, e in required.items() if e}
    needed = {path: oid for path, oid in needed.items() if oid}
    extra = {lfs_oid(e.get("physical_address")) for e in optional if e} - {None}
    status = await availability.lfs_statuses(set(needed.values()) | extra)
    lost = {oid for oid in set(needed.values()) if status[oid] != "available"}
    # Collected, then uploaded again: the claim revives it, as a commit's does
    for oid in sorted(oid for oid in lost if status[oid] == "collected"):
        if await object_exists(cfg.s3.bucket, lfs_key(oid)):
            lost.discard(oid)
    wanted = (set(needed.values()) - lost) | {oid for oid in extra if status[oid] == "available"}
    for n, oid in enumerate(sorted(wanted)):
        try:
            if claim_for_commit(oid, True) and not await object_exists(cfg.s3.bucket, lfs_key(oid)):
                # Revived, but gone since the check: it is collected after all
                collected_after_all(oid)
                raise LfsObjectUnavailable(oid)
        except LfsObjectUnavailable:
            lost.add(oid)
        if n % CLAIM_YIELD == CLAIM_YIELD - 1:
            await asyncio.sleep(0)
    missing = sorted(path for path, oid in needed.items() if oid in lost)
    if missing:
        raise OperationRefused(
            400,
            {
                "error": f"Cannot {action}: {len(missing)} LFS file(s) are no longer stored "
                f"(garbage collected or missing): {shown(missing)}",
                "missing_files": missing,
                "recoverable": False,
            },
        )


async def commit_changes(
    client, lakefs_repo: str, commit_id: str, base: str | None = None
) -> tuple[str, dict]:
    """``(commit, its entries of the paths it changed)`` against ``base``
    (default: its first parent), read to the end (``None``: deleted), for
    ``record_commits``."""
    if base is None:
        parents = (await client.get_commit(repository=lakefs_repo, commit_id=commit_id)).get(
            "parents"
        ) or []
        base = parents[0] if parents else None
    paths = await availability.changed_paths(client, lakefs_repo, base, commit_id) if base else []
    return commit_id, await availability.entries(client, lakefs_repo, commit_id, paths)


async def _regular_ids(client, lakefs_repo: str, ref: str, paths: list[str]) -> dict:
    """Git blob ids of regular files, as commits record them (small by
    definition: bigger files are LFS). A file that cannot be read is left out.

    ponytail: reads every changed regular file inside the request; thousands
    of them would want the id stored as object metadata at upload instead.
    """
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


def _regular_bytes(entries: dict[str, dict | None]) -> int:
    return sum(
        e.get("size_bytes") or 0
        for e in entries.values()
        if e is not None and lfs_oid(e.get("physical_address")) is None
    )


def _recount(repo: Repository) -> None:
    """Have the repository's usage recounted; best effort (logged)."""
    try:
        usage.enqueue_repository_recount(repo.id)
    except Exception as e:
        logger.warning(f"Could not schedule a usage recount of {repo.full_id}: {e}")


async def count_main_move(client, lakefs_repo: str, repo: Repository, commit: str) -> None:
    """Count main's move to ``commit`` from its first parent in the
    repository's usage (``usage.main_moved``), from the regular files it
    changed. Anything unexpected recounts the repository instead: the
    commit stands either way."""
    try:
        parents = (await client.get_commit(repository=lakefs_repo, commit_id=commit)).get(
            "parents"
        ) or []
        if not parents:
            raise ValueError("an initial commit")
        diff = await availability.changes(client, lakefs_repo, parents[0], commit)
        before, after = await asyncio.gather(
            availability.entries(
                client, lakefs_repo, parents[0], [e["path"] for e in diff if e["type"] != "added"]
            ),
            availability.entries(
                client, lakefs_repo, commit, [e["path"] for e in diff if e["type"] != "removed"]
            ),
        )
        usage.main_moved(repo.id, parents[0], commit, _regular_bytes(after) - _regular_bytes(before))
    except Exception as e:
        logger.warning(f"Could not count {commit[:8]} in the usage of {repo.full_id}: {e}")
        _recount(repo)


def outcome_unknown(repo: Repository, branch: str) -> None:
    """What a failed operation did to ``branch`` is unknown: have the
    reconciliation record what it links (collection waits for it), and
    main's usage recounted."""
    enqueue_lfs_reconciliation()
    if branch == usage.MAIN:
        _recount(repo)


def _batches(items: list, size: int = ROW_BATCH):
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def record_commits(
    client,
    lakefs_repo: str,
    repo: Repository,
    branch: str,
    rounds: list[tuple[str, dict]],
    user: User,
    message: str,
    description: str,
) -> None:
    """Bring the database up to date for the paths ``rounds`` changed.

    ``rounds`` are ``(commit, entries it brought for the paths it changed)``.
    Recorded from what the branch holds now, so a concurrent commit's changes
    to the same paths are not overwritten: commit rows first (nothing else
    repairs one), head references (what garbage collection must keep), File
    rows, then each round's LFS history. What the head no longer links, and
    versions pushed out of a keep window, become collection candidates;
    nothing waits for the collection. A failure is logged and queues a
    reconciliation.
    """
    if not rounds:
        return  # no commit of its own
    for commit_id, _ in rounds:
        try:
            create_commit(
                commit_id=commit_id,
                repository=repo,
                repo_type=repo.repo_type,
                branch=branch,
                author=user,
                username=user.username,
                message=message,
                description=description,
            )
        except Exception as e:
            logger.warning(f"Could not record the commit {commit_id[:8]}: {e}")
    try:
        changed = sorted(set().union(*(wanted for _, wanted in rounds)))
        head = (await client.get_branch(repository=lakefs_repo, branch=branch))["commit_id"]
        actual = await availability.entries(client, lakefs_repo, head, changed)
        oids = {path: lfs_oid(e.get("physical_address")) for path, e in actual.items() if e}
        lfs_now = {path: oid for path, oid in oids.items() if oid}
        R = LfsHeadRef
        # LFS paths whose version changed, to review their keep windows. A path
        # the head did not link as LFS before is not reviewed: a version it
        # pushes out is kept (a leak at worst, never a loss)
        replaced = set()
        for batch in _batches(list(lfs_now)):
            replaced.update(
                path
                for (path,) in R.select(R.path_in_repo)
                .where((R.repository == repo) & (R.branch == branch) & R.path_in_repo.in_(batch))
                .tuples()
            )
        record_head_change(repo, branch, {path: lfs_now.get(path) for path in changed}, [])

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

        # Each round's LFS versions, attributed to the commit that brought them
        # and dated when it was made: a commit that landed after it stays newer
        made_at = {}
        for commit_id, _ in rounds:
            created = (await client.get_commit(repository=lakefs_repo, commit_id=commit_id))[
                "creation_date"
            ]
            made_at[commit_id] = datetime.fromtimestamp(created, tz=timezone.utc)
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
                "created_at": made_at[commit_id],
            }
            for commit_id, wanted in rounds
            for path, e in wanted.items()
            if e is not None and lfs_oid(e.get("physical_address"))
        ]
        for batch in _batches(history):
            with db.atomic():
                usage.lfs_linked(repo.id, {row["sha256"]: row["size"] for row in batch})
                LFSObjectHistory.insert_many(batch).execute()
            await asyncio.sleep(0)
        if record_evicted_versions(repo, sorted(replaced)):
            enqueue_lfs_collection()
    except Exception as e:
        logger.exception(f"Could not record the commits on {repo.full_id}@{branch}", e)
        outcome_unknown(repo, branch)
        return
    if branch == usage.MAIN:
        for commit_id, _ in rounds:
            await count_main_move(client, lakefs_repo, repo, commit_id)
