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
  versions a lowered keep count pushed out of a repository's windows;
- ``storage.reconcile_lfs_references`` makes the database account for every
  LFS object a branch head links (started from the admin Storage page, or by
  the first collection with ``lfs_auto_gc`` on, which waits for it).

``find_orphan_lakefs_repositories`` lists LakeFS repositories no row points at
(left by deletions before this module existed, or by crashed creates), so an
admin can review them and schedule the same purge.

Keep this module free of ``kohakuhub.api`` imports: the worker imports it to
register the handlers (see docs/development/background-tasks.md).
"""

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from kohakuhub import lfs_gc, tasks, usage
from kohakuhub.async_utils import run_in_s3_executor
from kohakuhub.config import cfg
from kohakuhub.db import (
    BackgroundTask,
    File,
    LFSObjectHistory,
    LfsGcCandidate,
    LfsHeadRef,
    Repository,
    utcnow,
)
from kohakuhub.lfs_gc import lfs_key, record_candidates
from kohakuhub.logger import get_logger
from kohakuhub.utils.lakefs import get_lakefs_client, resolve_lakefs_repo
from kohakuhub.utils.s3 import get_s3_client

logger = get_logger("STORAGE_CLEANUP")

PURGE_KIND = "storage.purge_repository"
COLLECT_LFS_KIND = "storage.collect_lfs"
EXPIRE_RECENT_LFS_KIND = "storage.expire_recent_lfs"
REVIEW_LFS_WINDOW_KIND = "storage.review_lfs_window"
RECONCILE_LFS_KIND = "storage.reconcile_lfs_references"
RECORD_BRANCH_KIND = "storage.record_branch_links"
FORGET_SQUASHED_KIND = "storage.forget_squashed_history"
# A reset's working branch; it lives for the reset only, so it protects nothing
SCRATCH_BRANCH_PREFIX = "kh-reset-"
RETRY_GATE = timedelta(seconds=30)
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


def enqueue_lfs_reconciliation() -> int | None:
    """Schedule the reconciliation of LFS references; ``None`` if already pending."""
    return tasks.enqueue(RECONCILE_LFS_KIND, dedupe_key=RECONCILE_LFS_KIND)


def _reconciliation_pending() -> bool:
    """Links not recorded yet: a new branch's (``storage.record_branch_links``),
    or a reconciliation queued since the last one completed (after a failure
    to record head links). The one completing, which enqueues the
    collection while still running, predates its own mark and does not count.
    """
    T = BackgroundTask
    pending = T.status.in_([tasks.QUEUED, tasks.RUNNING])
    return (
        T.select()
        .where(
            pending
            & (
                (T.kind == RECORD_BRANCH_KIND)
                | ((T.kind == RECONCILE_LFS_KIND) & (T.created_at > lfs_gc.reconciled_since()))
            )
        )
        .exists()
    )


def enqueue_lfs_window_review(repo: Repository) -> int | None:
    """Review ``repo``'s keep windows after its keep count was lowered."""
    return tasks.enqueue(
        REVIEW_LFS_WINDOW_KIND, {"repo_id": repo.id}, dedupe_key=f"review-lfs:{repo.id}"
    )


def record_repository_lfs(repo: Repository) -> int:
    """Record every LFS object ``repo`` used as a collection candidate.

    Call it in the transaction that deletes the row: the cascade removes the
    file, history and head-link rows read here, and the candidates must not be decided
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
    shas.update(
        sha
        for (sha,) in LfsHeadRef.select(LfsHeadRef.sha256)
        .where(LfsHeadRef.repository == repo)
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
    if cfg.app.lfs_auto_gc and not lfs_gc.references_reconciled():
        # Keep windows decide deletions only once what every branch head
        # links is recorded; the reconciliation enqueues this task when done.
        enqueue_lfs_reconciliation()
        logger.warning("Waiting for the LFS reference reconciliation before collecting")
        return
    if cfg.app.lfs_auto_gc and _reconciliation_pending():
        # Come back once the missing links are recorded; retrying later, not
        # relying on the task recording them, which may still be finishing
        tasks.enqueue(
            COLLECT_LFS_KIND, dedupe_key=COLLECT_LFS_KIND, run_after=utcnow() + RETRY_GATE
        )
        logger.info("Waiting for LFS links to be recorded before collecting")
        return
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


async def _pages(fetch):
    """Every result of a paginated LakeFS listing; ``fetch(after)`` gets a page."""
    after = ""
    while True:
        page = await fetch(after)
        for item in page.get("results", []):
            yield item
        pagination = page.get("pagination") or {}
        if not pagination.get("has_more"):
            return
        after = pagination["next_offset"]


async def lfs_links(
    lakefs_repo: str, ref: str, counts: Counter | None = None, paths: set | None = None
) -> dict:
    """``{path: (sha256, size)}`` for every global LFS object ``ref`` links;
    every object's path is added to ``paths`` when given."""
    client = get_lakefs_client()
    links = {}
    # Without a delimiter the listing holds objects only, recursively.
    objects = [
        obj
        async for obj in _pages(
            lambda after: client.list_objects(
                repository=lakefs_repo, ref=ref, after=after, amount=LAKEFS_LIST_PAGE
            )
        )
    ]
    for obj in objects:
        if counts is not None:
            counts["objects"] += 1
        if paths is not None:
            paths.add(obj["path"])
        oid = lfs_gc.lfs_oid(obj.get("physical_address"))
        if oid is not None:
            links[obj["path"]] = (oid, obj.get("size_bytes", 0))
    return links


async def branch_head_references(lakefs_repo: str):
    """The LFS objects the heads of a LakeFS repository's branches link.

    Returns ``(heads, default_head, default_paths, counts)`` in the shape
    ``lfs_gc.reconcile_references`` takes, or ``None`` when the LakeFS
    repository does not exist.
    """
    client = get_lakefs_client()
    try:
        default_branch = (await client.get_repository(lakefs_repo))["default_branch"]
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise
        return None
    heads: set[tuple[str, str, str]] = set()
    default_head: dict[str, tuple[str, int]] = {}
    counts = Counter()
    listed: dict[str, tuple[dict, set]] = {}
    default_paths: set[str] = set()
    branches = [
        branch
        async for branch in _pages(
            lambda after: client.list_branches(lakefs_repo, after=after, amount=LAKEFS_LIST_PAGE)
        )
    ]
    for branch in branches:
        if branch["id"].startswith(SCRATCH_BRANCH_PREFIX):
            continue
        counts["branches"] += 1
        commit_id = branch["commit_id"]
        if commit_id not in listed:  # branches at the same commit link the same objects
            paths: set[str] = set()
            listed[commit_id] = (await lfs_links(lakefs_repo, commit_id, counts, paths), paths)
        links, paths = listed[commit_id]
        heads.update((branch["id"], path, sha) for path, (sha, _) in links.items())
        if branch["id"] == default_branch:
            default_head, default_paths = links, paths
    counts["lfs_references"] = len(heads)
    return heads, default_head, default_paths, counts


async def refresh_head_refs(
    repo: Repository, branch: str, *, exact: bool, whole_repository: bool = False
) -> None:
    """Record what ``branch``'s head links now, after a branch operation.

    ``exact`` replaces the branch's recorded links (the head was rewritten:
    revert, merge, reset), or every branch's with ``whole_repository`` (a
    move or squash leaves only this branch); otherwise links are only added
    (a new branch, whose commits may already be recording their own).
    Objects no longer linked become collection candidates. A failure queues
    a reconciliation, which collection waits for.
    """
    try:
        links = await lfs_links(resolve_lakefs_repo(repo), branch)
        refs = {(branch, path, sha) for path, (sha, _) in links.items()}
        if exact:
            scope = None if whole_repository else branch
            release_objects(lfs_gc.replace_head_refs(repo, scope, refs))
        else:
            lfs_gc.add_head_refs(repo, refs)
    except Exception as e:
        logger.warning(f"Could not record the LFS links of {repo.full_id}@{branch}: {e}")
        enqueue_lfs_reconciliation()


def record_head_change(
    repo: Repository, branch: str, paths: dict[str, str | None], folders: list[str]
) -> None:
    """Record a commit's changes to what ``branch`` links (see ``lfs_gc.update_head_refs``).

    A failure queues a reconciliation, which collection waits for.
    """
    try:
        release_objects(lfs_gc.update_head_refs(repo, branch, paths, folders))
    except Exception as e:
        logger.warning(f"Could not record the LFS links of {repo.full_id}@{branch}: {e}")
        enqueue_lfs_reconciliation()


def enqueue_branch_links(repo: Repository, branch: str) -> int | None:
    """Record a new branch's links in the background (``record_branch_links``).

    Listing a big head takes time the branch creation should not wait for;
    collection waits for it instead.
    """
    return tasks.enqueue(
        RECORD_BRANCH_KIND,
        {"repo_id": repo.id, "branch": branch},
        dedupe_key=f"branch-links:{repo.id}:{branch}",
    )


@tasks.task(RECORD_BRANCH_KIND, timeout=3600, max_attempts=5)
async def record_branch_links(payload: dict[str, Any], ctx: tasks.TaskContext) -> None:
    """Record what a new branch's head links (only adds: its commits since
    may already be recording their own), then let the collection run."""
    repo = Repository.get_or_none(Repository.id == payload["repo_id"])
    if repo is None:
        return  # deleted since; its objects were recorded then
    ctx.stage(f"listing {repo.full_id}@{payload['branch']}")
    await refresh_head_refs(repo, payload["branch"], exact=False)
    enqueue_lfs_collection()


def forget_branch(repo: Repository, branch: str) -> None:
    """A deleted branch links nothing any more."""
    try:
        if lfs_gc.drop_head_refs(repo, branch):
            enqueue_lfs_collection()
    except Exception as e:
        logger.warning(f"Could not forget the LFS links of {repo.full_id}@{branch}: {e}")
        enqueue_lfs_reconciliation()


def release_objects(shas: set[str]) -> None:
    """Hand objects no branch head may link any more to the collection."""
    if shas:
        record_candidates(shas)
        enqueue_lfs_collection()


@tasks.task(RECONCILE_LFS_KIND, timeout=24 * 3600, max_attempts=5)
async def reconcile_lfs_references(payload: dict[str, Any], ctx: tasks.TaskContext) -> None:
    """Make the database account for the LFS objects every branch head links.

    Repository by repository, in id order, with the position and running
    counts checkpointed after each one, so a retry or another worker resumes
    where it stopped. Only adds or corrects rows (``reconcile_references``),
    so running it again, or over data that already accounts, changes nothing.
    """
    state = ctx.checkpoint_state or {"after": 0, "stats": {}}
    stats = Counter(state["stats"])
    total = Repository.select().count()
    done = Repository.select().where(Repository.id <= state["after"]).count()
    remaining = Repository.select().where(Repository.id > state["after"]).order_by(Repository.id)
    for repo in remaining:
        if ctx.cancel_requested:
            raise tasks.TaskCancelled()
        ctx.stage(f"reconciling {repo.repo_type}:{repo.full_id}")
        found = await branch_head_references(resolve_lakefs_repo(repo))
        if found is None:
            stats["repositories_without_lakefs"] += 1
        else:
            heads, default_head, default_paths, counts = found
            stats.update(counts)
            stats.update(lfs_gc.reconcile_references(repo, heads, default_head, default_paths))
        stats["repositories"] += 1
        done += 1
        ctx.checkpoint({"after": repo.id, "stats": dict(stats)})
        ctx.progress(done, max(total, done))
    lfs_gc.mark_references_reconciled()
    logger.info(
        "LFS references reconciled: "
        + ", ".join(f"{key} {value}" for key, value in sorted(stats.items()))
    )
    enqueue_lfs_collection()


async def _tree(client, lakefs_repo: str, ref: str) -> list[dict]:
    """Every object ``ref`` has (its committed tree, or a branch with its staging)."""
    listing = _pages(
        lambda after: client.list_objects(
            repository=lakefs_repo, ref=ref, after=after, amount=LAKEFS_LIST_PAGE
        )
    )
    return [obj async for obj in listing]


async def _linked_since(
    client, lakefs_repo: str, root: str, root_tree: list[dict]
) -> set[str]:
    """The physical addresses the history a squash left links: the squash
    commit's tree, every branch (with its staged changes) and tag, and what
    each commit since the squash brought (its changes to its first parent),
    walking back from every ref until a parentless commit."""
    addresses = {o["physical_address"] for o in root_tree}
    branches = [
        branch
        async for branch in _pages(
            lambda after: client.list_branches(
                repository=lakefs_repo, after=after, amount=LAKEFS_LIST_PAGE
            )
        )
    ]
    tags = [
        tag
        async for tag in _pages(
            lambda after: client.list_tags(
                repository=lakefs_repo, after=after, amount=LAKEFS_LIST_PAGE
            )
        )
    ]
    for ref in [b["id"] for b in branches] + [t["commit_id"] for t in tags]:
        # A branch ref lists its staged changes too
        tree = await _tree(client, lakefs_repo, ref)
        addresses.update(o["physical_address"] for o in tree)
    seen = {root}
    for head in {r["commit_id"] for r in branches + tags}:
        log = _pages(
            lambda after: client.log_commits(
                repository=lakefs_repo, ref=head, after=after, amount=LAKEFS_LIST_PAGE
            )
        )
        async for commit in log:
            if commit["id"] in seen or not commit["parents"]:
                continue
            seen.add(commit["id"])
            changes = _pages(
                lambda after: client.diff_refs(
                    repository=lakefs_repo,
                    left_ref=commit["parents"][0],
                    right_ref=commit["id"],
                    after=after,
                    amount=LAKEFS_LIST_PAGE,
                    diff_type="two_dot",
                )
            )
            async for change in changes:
                if change.get("path_type", "object") != "object" or change["type"] == "removed":
                    continue
                stat = await client.stat_object(
                    repository=lakefs_repo, ref=commit["id"], path=change["path"]
                )
                addresses.add(stat["physical_address"])
    return addresses


def _stale_objects(bucket: str, prefix: str, before, keep: set[str]) -> list[str]:
    """Keys under ``prefix`` last written before ``before`` that nothing links."""
    s3, stale, token = get_s3_client(), [], None
    while True:
        page = s3.list_objects_v2(
            Bucket=bucket,
            Prefix=prefix,
            MaxKeys=S3_DELETE_BATCH,
            **({"ContinuationToken": token} if token else {}),
        )
        stale += [
            item["Key"]
            for item in page.get("Contents", [])
            if item["LastModified"] < before and f"s3://{bucket}/{item['Key']}" not in keep
        ]
        if not page.get("IsTruncated"):
            return stale
        token = page["NextContinuationToken"]


@tasks.task(FORGET_SQUASHED_KIND, timeout=6 * 3600, max_attempts=10)
async def forget_squashed_history(payload: dict[str, Any]) -> None:
    """Forget what a repository squash made unreachable.

    Runs a few minutes after the squash, when the uploads under way then are
    committed or staged. Of what was written before the squash (``at``):

    - history rows (``id <= through``) for an LFS version the squash commit
      does not link are deleted, and their objects become collection
      candidates;
    - file rows for a path neither the squash commit nor main has now are
      marked deleted (rows are per repository, not per branch, so the
      dropped branches' files were still there);
    - regular file objects under the repository's ``data/`` prefix that the
      history the squash left does not link (``_linked_since``) are deleted:
      only the old history had them, and nothing may read it any more.

    Rows and objects written after the squash are left alone. The
    repository's LFS usage is then set from the history left. Re-runnable:
    a rerun finds nothing more to change.
    """
    repo = Repository.get_or_none(Repository.id == payload["repo_id"])
    if repo is None:
        return
    client = get_lakefs_client()
    lakefs_repo = resolve_lakefs_repo(repo)
    at = datetime.fromisoformat(payload["at"])
    squashed = await _tree(client, lakefs_repo, payload["commit"])
    linked = {
        (o["path"], oid) for o in squashed if (oid := lfs_gc.lfs_oid(o.get("physical_address")))
    }
    main = await _tree(client, lakefs_repo, "main")
    paths = {o["path"] for o in squashed} | {o["path"] for o in main}

    H = LFSObjectHistory
    gone, shas = [], set()
    for row_id, path, sha in (
        H.select(H.id, H.path_in_repo, H.sha256)
        .where((H.repository == repo) & (H.id <= payload["through"]))
        .tuples()
    ):
        if (path, sha) not in linked:
            gone.append(row_id)
            shas.add(sha)
    stale = [
        file_id
        for file_id, path in File.select(File.id, File.path_in_repo)
        .where((File.repository == repo) & (File.is_deleted == False) & (File.updated_at <= at))
        .tuples()
        if path not in paths
    ]
    candidates = 0
    for start in range(0, max(len(gone), len(stale)), LFS_BATCH):
        with Repository._meta.database.atomic():
            H.delete().where(H.id.in_(gone[start : start + LFS_BATCH])).execute()
            File.update(is_deleted=True, updated_at=datetime.now(timezone.utc)).where(
                File.id.in_(stale[start : start + LFS_BATCH]) & (File.updated_at <= at)
            ).execute()
    with Repository._meta.database.atomic():
        usage.lfs_recounted(repo.id)
        candidates = record_candidates(sha for sha in shas if len(sha) == 64)
    if candidates:
        enqueue_lfs_collection()

    # The regular file objects only the old history had
    namespace = (await client.get_repository(repository=lakefs_repo))["storage_namespace"]
    bucket, _, prefix = namespace.removeprefix("s3://").partition("/")
    purged = 0
    if bucket == cfg.s3.bucket:
        keep = await _linked_since(client, lakefs_repo, payload["commit"], squashed)
        keys = await run_in_s3_executor(
            _stale_objects, bucket, f"{prefix.rstrip('/')}/data/", at, keep
        )
        for start in range(0, len(keys), S3_DELETE_BATCH):
            await run_in_s3_executor(_delete_keys, bucket, keys[start : start + S3_DELETE_BATCH])
        purged = len(keys)
    logger.info(
        f"Forgot what a squash of {repo.repo_type}:{repo.full_id} made unreachable: "
        f"{len(gone)} history row(s), {len(stale)} file row(s), {purged} regular object(s); "
        f"{candidates} LFS object(s) left to the collection"
    )


def _iso(value) -> str:
    return value.replace(tzinfo=timezone.utc).isoformat()  # stored as naive UTC


def lfs_reconciliation_status() -> dict[str, Any]:
    """The reconciliation marker and the latest reconciliation task, for the admin panel."""
    T = BackgroundTask
    task = T.select().where(T.kind == RECONCILE_LFS_KIND).order_by(T.id.desc()).first()
    return {
        "reconciled_at": lfs_gc.reconciled_at(),
        "auto_gc": cfg.app.lfs_auto_gc,
        "task": task
        and {
            "id": task.id,
            "status": task.status,
            "progress_done": task.progress_done,
            "progress_total": task.progress_total,
            "stage": task.progress_stage,
            "stats": (json.loads(task.checkpoint) if task.checkpoint else {}).get("stats", {}),
            "created_at": _iso(task.created_at),
            "finished_at": task.finished_at and _iso(task.finished_at),
        },
    }


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
