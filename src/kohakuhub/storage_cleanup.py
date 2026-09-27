"""Storage cleanup for repository rows deleted without the regular delete path.

``DELETE /api/repos/delete`` cleans LakeFS and S3 inline before it deletes the
row. Deleting a user or an organization cascades every repository row away in
one transaction instead, and with them the file and LFS history rows that say
which LFS objects only those repositories used. So the deleting transaction
records what to clean (``schedule_repository_purge``) and two re-runnable
background tasks do it (issue #109):

- ``storage.purge_repository``, one per LakeFS repository, deletes its S3
  prefix and then the LakeFS repository itself;
- ``storage.collect_lfs`` deletes the recorded LFS objects nothing relies on
  any more, using the retention decision and tombstones in ``kohakuhub.lfs_gc``;
- ``storage.expire_recent_lfs`` (hourly) turns uploads whose grace period
  ended into candidates, and ``storage.review_lfs_window`` records the
  versions a lowered keep count pushed out of a repository's windows.

``find_orphan_lakefs_repositories`` lists LakeFS repositories no row points at
(left by deletions before this module existed, or by crashed creates), so an
admin can review them and schedule the same purge.

Keep this module free of ``kohakuhub.api`` imports: the worker imports it to
register the handlers (see docs/development/background-tasks.md).
"""

import json
from datetime import timedelta
from typing import Any

import httpx

from kohakuhub import lfs_gc, tasks
from kohakuhub.async_utils import run_in_s3_executor
from kohakuhub.config import cfg
from kohakuhub.db import BackgroundTask, File, LFSObjectHistory, LfsGcCandidate, Repository
from kohakuhub.lfs_gc import lfs_key, record_candidates
from kohakuhub.logger import get_logger
from kohakuhub.utils.lakefs import get_lakefs_client, resolve_lakefs_repo
from kohakuhub.utils.s3 import get_s3_client

logger = get_logger("STORAGE_CLEANUP")

PURGE_KIND = "storage.purge_repository"
COLLECT_LFS_KIND = "storage.collect_lfs"
EXPIRE_RECENT_LFS_KIND = "storage.expire_recent_lfs"
REVIEW_LFS_WINDOW_KIND = "storage.review_lfs_window"
S3_DELETE_BATCH = 1000  # the S3 DeleteObjects maximum
LFS_BATCH = 500
LAKEFS_LIST_PAGE = 1000


def _referenced_lakefs_repos() -> set[str]:
    """Every LakeFS repository id a repository row points at.

    Rows written before migration 016 store no id and derive it; there are
    few, and none are created any more.
    """
    R = Repository
    referenced = {
        lakefs_repo
        for (lakefs_repo,) in R.select(R.lakefs_repo).where(R.lakefs_repo.is_null(False)).tuples()
    }
    referenced.update(resolve_lakefs_repo(row) for row in R.select().where(R.lakefs_repo.is_null()))
    return referenced


def lakefs_repo_in_use(lakefs_repo: str) -> bool:
    R = Repository
    if R.select().where(R.lakefs_repo == lakefs_repo).exists():
        return True
    return any(
        resolve_lakefs_repo(row) == lakefs_repo for row in R.select().where(R.lakefs_repo.is_null())
    )


def enqueue_lfs_collection() -> int | None:
    return tasks.enqueue(COLLECT_LFS_KIND, dedupe_key=COLLECT_LFS_KIND)


def enqueue_lfs_window_review(repo: Repository) -> int | None:
    """Review ``repo``'s keep windows after its keep count was lowered."""
    return tasks.enqueue(
        REVIEW_LFS_WINDOW_KIND, {"repo_id": repo.id}, dedupe_key=f"review-lfs:{repo.id}"
    )


def record_repository_lfs(repo: Repository) -> int:
    """Record every LFS object ``repo`` used as a collection candidate.

    Call it in the transaction that deletes the row: the cascade removes the
    file and history rows read here, and the candidates must not be decided
    before the row is gone. Returns how many objects were recorded.
    """
    shas = {
        sha
        for (sha,) in File.select(File.sha256)
        .where((File.repository == repo) & (File.lfs == True))
        .tuples()
    }
    shas.update(
        sha
        for (sha,) in LFSObjectHistory.select(LFSObjectHistory.sha256)
        .where(LFSObjectHistory.repository == repo)
        .tuples()
    )
    if shas:
        record_candidates(shas)
        enqueue_lfs_collection()
    return len(shas)


def enqueue_purge(lakefs_repo: str, label: str) -> int | None:
    """Schedule the purge of one LakeFS repository; ``None`` if already pending."""
    return tasks.enqueue(
        PURGE_KIND, {"lakefs_repo": lakefs_repo, "repo": label}, dedupe_key=f"purge:{lakefs_repo}"
    )


def schedule_repository_purge(repo: Repository) -> str:
    """Record everything the cleanup of ``repo``'s storage needs.

    Call it inside the transaction that deletes the row, before the delete:
    the cascade removes the file and LFS history rows read here, and the
    tasks enqueued here roll back together with the deletion. Returns the
    LakeFS repository id that will be purged.
    """
    lakefs_repo = resolve_lakefs_repo(repo)
    record_repository_lfs(repo)
    enqueue_purge(lakefs_repo, f"{repo.repo_type}:{repo.full_id}")
    return lakefs_repo


def _check_delete_errors(response: dict[str, Any]) -> None:
    errors = response.get("Errors") or []
    if errors:
        first = errors[0]
        raise RuntimeError(
            f"S3 refused to delete {len(errors)} object(s), e.g. {first.get('Key')}: "
            f"{first.get('Code')} {first.get('Message')}"
        )


def _delete_prefix_batch(bucket: str, prefix: str) -> int:
    """Delete up to one DeleteObjects batch under ``prefix``; return how many."""
    client = get_s3_client()
    listing = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=S3_DELETE_BATCH)
    keys = [{"Key": item["Key"]} for item in listing.get("Contents", [])]
    if keys:
        _check_delete_errors(
            client.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
        )
    return len(keys)


def _delete_keys(bucket: str, keys: list[str]) -> None:
    client = get_s3_client()
    _check_delete_errors(
        client.delete_objects(
            Bucket=bucket, Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True}
        )
    )


@tasks.task(PURGE_KIND, timeout=6 * 3600, max_attempts=10)
async def purge_repository(payload: dict[str, Any], ctx: tasks.TaskContext) -> None:
    """Delete one LakeFS repository and everything under its S3 prefix.

    Re-runnable: a rerun deletes whatever is left and treats a LakeFS
    repository that is already gone as done.
    """
    lakefs_repo = payload["lakefs_repo"]
    if lakefs_repo_in_use(lakefs_repo):
        # The id was taken again, e.g. by the same repository name under a
        # re-registered username; that data belongs to the new row.
        raise tasks.PermanentTaskError(f"LakeFS repository {lakefs_repo} is in use by a repository")

    # Objects go first: while the LakeFS repository exists, nobody can create
    # a new repository with this id whose objects this loop would delete.
    ctx.stage("deleting objects")
    prefix = f"{lakefs_repo}/"
    deleted = 0
    while batch := await run_in_s3_executor(_delete_prefix_batch, cfg.s3.bucket, prefix):
        deleted += batch
        ctx.progress(deleted)
        logger.info(f"Deleted {deleted} object(s) under s3://{cfg.s3.bucket}/{prefix}")

    ctx.stage("deleting LakeFS repository")
    try:
        await get_lakefs_client().delete_repository(repository=lakefs_repo, force=True)
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise
        logger.info(f"LakeFS repository {lakefs_repo} was already gone")
    logger.info(
        f"Purged {payload['repo']}: {deleted} object(s) and LakeFS repository {lakefs_repo}"
    )


@tasks.task(COLLECT_LFS_KIND, timeout=6 * 3600, max_attempts=10)
async def collect_lfs(payload: dict[str, Any], ctx: tasks.TaskContext) -> None:
    """Delete recorded LFS objects that nothing relies on any more.

    Each candidate is decided and tombstoned under its lock
    (``lfs_gc.begin_delete``); the batch's objects are then deleted from
    storage and their tombstones completed. Candidates are consumed only
    after that, so an interrupted run simply continues with what is left.
    """
    C = LfsGcCandidate
    total = C.select().count()
    checked = deleted = 0
    ctx.stage("collecting LFS objects")
    while shas := [
        sha for (sha,) in C.select(C.sha256).order_by(C.sha256).limit(LFS_BATCH).tuples()
    ]:
        doomed = [sha for sha in shas if lfs_gc.begin_delete(sha)]
        if doomed:
            await run_in_s3_executor(_delete_keys, cfg.s3.bucket, [lfs_key(sha) for sha in doomed])
            lfs_gc.finish_delete(doomed)
        C.delete().where(C.sha256.in_(shas)).execute()
        checked += len(shas)
        deleted += len(doomed)
        ctx.progress(checked, max(total, checked))
    logger.info(f"Checked {checked} LFS object(s); deleted {deleted} nothing relies on")


@tasks.task(EXPIRE_RECENT_LFS_KIND, every=timedelta(hours=1), max_attempts=3)
async def expire_recent_lfs(payload: dict[str, Any]) -> None:
    """Hand uploads whose grace period ended to the collection."""
    expired = lfs_gc.expire_recent()
    if expired:
        enqueue_lfs_collection()
        logger.info(f"{expired} recently used LFS object(s) left their grace period")


@tasks.task(REVIEW_LFS_WINDOW_KIND, timeout=3600, max_attempts=5)
async def review_lfs_window(payload: dict[str, Any], ctx: tasks.TaskContext) -> None:
    """Record the versions a lowered keep count pushed out of a repository's paths."""
    repo = Repository.get_or_none(Repository.id == payload["repo_id"])
    if repo is None:
        return  # deleted since; its objects were recorded then
    H = LFSObjectHistory
    paths = [
        path for (path,) in H.select(H.path_in_repo).where(H.repository == repo).distinct().tuples()
    ]
    recorded = 0
    for done, path in enumerate(paths, start=1):
        recorded += lfs_gc.record_evicted_versions(repo, [path])
        ctx.progress(done, len(paths))
    if recorded:
        enqueue_lfs_collection()
    logger.info(f"Reviewed {len(paths)} LFS path(s) of {repo.full_id}; {recorded} candidate(s)")


async def find_orphan_lakefs_repositories() -> list[dict[str, Any]]:
    """LakeFS repositories no repository row points at, oldest first.

    Each entry says whether a purge is already queued or running for it.
    """
    referenced = _referenced_lakefs_repos()
    T = BackgroundTask
    pending = set()
    for (payload,) in (
        T.select(T.payload)
        .where((T.kind == PURGE_KIND) & T.status.in_([tasks.QUEUED, tasks.RUNNING]))
        .tuples()
    ):
        try:
            pending.add(json.loads(payload).get("lakefs_repo"))
        except (ValueError, AttributeError):
            continue  # a corrupt payload cannot name a repository
    client = get_lakefs_client()
    orphans: list[dict[str, Any]] = []
    after = None
    while True:
        page = await client.list_repositories(amount=LAKEFS_LIST_PAGE, after=after)
        for repository in page.get("results", []):
            if repository["id"] in referenced:
                continue
            orphans.append(
                {
                    "id": repository["id"],
                    "created_at": repository.get("creation_date"),
                    "storage_namespace": repository.get("storage_namespace"),
                    "purge_pending": repository["id"] in pending,
                }
            )
        pagination = page.get("pagination") or {}
        if not pagination.get("has_more"):
            break
        after = pagination.get("next_offset")
    return sorted(orphans, key=lambda orphan: (orphan["created_at"] or 0, orphan["id"]))
