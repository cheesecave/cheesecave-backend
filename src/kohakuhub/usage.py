"""Storage usage, kept up to date as repositories change.

A repository's usage is two counters on its row:

- ``main_regular_bytes``: the regular files on its ``main`` branch. Their
  objects live under the repository's own storage prefix;
- ``lfs_bytes``: the LFS objects its history links (any branch, any version)
  that are still stored, each counted once. Deleting an LFS file frees
  nothing until garbage collection removes the object.

``used_bytes`` is their sum. A namespace's usage is summed from its
repositories when asked (the ``repository(namespace, private)`` index), so
there is one ledger and nothing to drift apart.

Every change applies its difference, in proportion to what it changed:

- a new commit on main: the regular files it changed against its parent
  (``main_moved``). It applies only if the counter counts up to that parent;
  otherwise the repository is recounted, so no change is counted twice or lost;
- LFS history rows: the objects new to the repository (``lfs_linked``), in
  the transaction that inserts them;
- garbage collection removing an object, or one coming back
  (``object_gone`` / ``object_back``), for every repository whose history
  has it, in the transaction that tombstones or revives it.

A recount sets exact values: ``recount_repository`` for one repository, the
``usage.recount`` task for every repository or one namespace's. The admin
portal starts it, and so does migration 023 on an upgraded database.

Keep this module free of ``kohakuhub.api`` imports: the worker imports it.
"""

import json
from collections import Counter
from datetime import timedelta, timezone
from typing import Any, Iterable

import httpx
from peewee import fn

from kohakuhub import tasks
from kohakuhub.config import cfg
from kohakuhub.db import (
    BackgroundTask,
    LFSObjectHistory,
    LfsObjectTombstone,
    Repository,
    User,
    utcnow,
)
from kohakuhub.logger import get_logger
from kohakuhub.utils.lakefs import get_lakefs_client, resolve_lakefs_repo

logger = get_logger("USAGE")

MAIN = "main"  # the branch whose regular files count
RECOUNT_KIND = "usage.recount"
RECOUNT_REPOSITORY_KIND = "usage.recount_repository"
RECOUNT_RETRY = timedelta(
    seconds=30
)  # a busy repository is recounted again this much later
LIST_PAGE = 1000
BATCH = 500
DRIFT_SHOWN = 20  # repositories with the largest drift a recount reports


def _sha256(sha: str) -> bool:
    """LFS objects are sha256; older versions recorded other checksums for
    big files they kept under the repository's prefix (counted as regular)."""
    return len(sha) == 64


def _batches(items: list, size: int = BATCH):
    for start in range(0, len(items), size):
        yield items[start : start + size]


# ----- reading -----


def namespace_usage(namespaces: Iterable[str]) -> dict[str, dict[str, int]]:
    """``{namespace: {"private": bytes, "public": bytes}}``, summed from repositories."""
    usage = {namespace: {"private": 0, "public": 0} for namespace in namespaces}
    R = Repository
    for batch in _batches(list(usage)):
        for namespace, private, total in (
            R.select(R.namespace, R.private, fn.COALESCE(fn.SUM(R.used_bytes), 0))
            .where(R.namespace.in_(batch))
            .group_by(R.namespace, R.private)
            .tuples()
        ):
            usage[namespace]["private" if private else "public"] = int(total)
    return usage


def namespace_used(namespace: str, private: bool) -> int:
    return namespace_usage([namespace])[namespace]["private" if private else "public"]


def users_usage() -> dict[str, int]:
    """``{"private": bytes, "public": bytes}`` of every user's repositories (not organizations')."""
    R = Repository
    by_privacy = dict(
        R.select(R.private, fn.COALESCE(fn.SUM(R.used_bytes), 0))
        .join(User, on=(R.namespace == User.username))
        .where(User.is_org == False)
        .group_by(R.private)
        .tuples()
    )
    return {
        "private": int(by_privacy.get(True, 0)),
        "public": int(by_privacy.get(False, 0)),
    }


def write_namespace_snapshots(namespaces: Iterable[str] | None = None) -> None:
    """Copy the summed usage into ``user.private_used_bytes`` / ``public_used_bytes``.

    Nothing reads those columns any more; they are refreshed for SQL readers
    and older tooling.
    """
    R = Repository

    def total(private: bool):
        return fn.COALESCE(
            R.select(fn.SUM(R.used_bytes)).where(
                (R.namespace == User.username) & (R.private == private)
            ),
            0,
        )

    query = User.update(private_used_bytes=total(True), public_used_bytes=total(False))
    if namespaces is not None:
        query = query.where(User.username.in_(list(namespaces)))
    query.execute()


# ----- applying changes -----


def _add(repo_ids, regular: int = 0, lfs: int = 0) -> None:
    R = Repository
    R.update(
        main_regular_bytes=R.main_regular_bytes + regular,
        lfs_bytes=R.lfs_bytes + lfs,
        used_bytes=R.used_bytes + regular + lfs,
    ).where(R.id.in_(repo_ids)).execute()


def _hold(repo_ids) -> None:
    """Lock repository rows until the transaction ends, in id order (so two
    transactions holding several cannot deadlock); SQLite serializes writers
    already. A change to a repository's LFS count takes its row before reading
    what the history links, so it sees every earlier change's history rows."""
    R = Repository
    held = R.select(R.id).where(R.id.in_(repo_ids)).order_by(R.id)
    if R._meta.database.for_update:
        held = held.for_update()
    held.execute()


def lfs_linked(repo_id: int, objects: dict[str, int]) -> None:
    """Count the objects (sha256: size) new to a repository's history.

    Call it before inserting their history rows, in the same transaction.
    """
    objects = {sha: size for sha, size in objects.items() if _sha256(sha)}
    if not objects:
        return
    _hold([repo_id])
    H, T = LFSObjectHistory, LfsObjectTombstone
    shas = sorted(objects)
    known, gone = set(), set()
    for batch in _batches(shas):
        known.update(
            sha
            for (sha,) in H.select(H.sha256)
            .where((H.repository == repo_id) & H.sha256.in_(batch))
            .distinct()
            .tuples()
        )
        gone.update(
            sha for (sha,) in T.select(T.sha256).where(T.sha256.in_(batch)).tuples()
        )
    added = sum(objects[sha] for sha in shas if sha not in known and sha not in gone)
    if added:
        _add([repo_id], lfs=added)


def _object_moved(sha256: str, sign: int) -> None:
    if not _sha256(sha256):
        return
    H = LFSObjectHistory
    size = H.select(fn.MAX(H.size)).where(H.sha256 == sha256).scalar()
    if size is None:
        return  # no repository's history has it
    holders = [
        r
        for (r,) in H.select(H.repository).where(H.sha256 == sha256).distinct().tuples()
    ]
    _hold(holders)
    _add(holders, lfs=sign * size)


def object_gone(sha256: str) -> None:
    """Garbage collection tombstoned an object: no repository counts it any more.

    History rows being inserted meanwhile are not seen; the claim protocol
    (``lfs_gc``) keeps garbage collection away from an object a commit links.
    """
    _object_moved(sha256, -1)


def object_back(sha256: str) -> None:
    """A tombstoned object is stored again: every repository linking it counts it."""
    _object_moved(sha256, +1)


def main_started(repo_id: int, commit: str) -> None:
    """A new repository's main is at ``commit``, with no regular files."""
    Repository.update(main_regular_bytes=0, main_counted_commit=commit).where(
        Repository.id == repo_id
    ).execute()


def main_moved(repo_id: int, parent: str, commit: str, regular_delta: int) -> bool:
    """Main moved from ``parent`` to ``commit``, changing its regular files by
    ``regular_delta`` bytes. Applied only if the counter counts up to
    ``parent``; otherwise the repository is recounted. Returns whether it applied."""
    R = Repository
    applied = (
        R.update(
            main_regular_bytes=R.main_regular_bytes + regular_delta,
            used_bytes=R.used_bytes + regular_delta,
            main_counted_commit=commit,
        )
        .where((R.id == repo_id) & (R.main_counted_commit == parent))
        .execute()
    )
    if not applied:
        logger.info(
            f"Usage of repository {repo_id} does not count up to {parent[:8]}; recounting"
        )
        enqueue_repository_recount(repo_id)
    return bool(applied)


# ----- recounting -----


def enqueue_repository_recount(
    repo_id: int, delay: timedelta | None = None
) -> int | None:
    return tasks.enqueue(
        RECOUNT_REPOSITORY_KIND,
        {"repo_id": repo_id},
        dedupe_key=f"usage-repo:{repo_id}",
        run_after=(utcnow() + delay) if delay else None,
    )


def enqueue_recount(namespace: str | None = None) -> int | None:
    """Recount every repository (or one namespace's); ``None`` if already pending."""
    return tasks.enqueue(
        RECOUNT_KIND,
        {"namespace": namespace} if namespace else {},
        dedupe_key=f"usage-recount:{namespace or '*'}",
    )


def _stored_lfs(repo_id: int):
    """SQL for the LFS bytes a repository's history links that are still stored."""
    H, T = LFSObjectHistory, LfsObjectTombstone
    distinct = (
        H.select(H.sha256, H.size)
        .where(
            (H.repository == repo_id)
            & (fn.LENGTH(H.sha256) == 64)
            & ~fn.EXISTS(T.select().where(T.sha256 == H.sha256))
        )
        .distinct()
        .alias("stored")
    )
    return H.select(fn.SUM(distinct.c.size)).from_(distinct)


async def _main_regular_bytes(repo: Repository) -> tuple[str | None, int]:
    """Main's head and the size of its regular files, listed from LakeFS."""
    from kohakuhub.lfs_gc import lfs_oid

    client = get_lakefs_client()
    lakefs_repo = resolve_lakefs_repo(repo)
    try:
        head = (await client.get_branch(repository=lakefs_repo, branch=MAIN))[
            "commit_id"
        ]
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise
        return None, 0
    total, after = 0, ""
    while True:
        page = await client.list_objects(
            repository=lakefs_repo, ref=head, after=after, amount=LIST_PAGE
        )
        total += sum(
            obj.get("size_bytes") or 0
            for obj in page["results"]
            if obj.get("path_type", "object") == "object"
            and lfs_oid(obj.get("physical_address")) is None
        )
        if not page["pagination"]["has_more"]:
            return head, total
        after = page["pagination"]["next_offset"]


async def recount_repository(repo_id: int) -> dict[str, Any] | None:
    """Set a repository's exact usage; its drift, or ``None`` when it could
    not be set (gone, or it changed meanwhile: recounted again later)."""
    repo = Repository.get_or_none(Repository.id == repo_id)
    if repo is None:
        return None
    counted, before = repo.main_counted_commit, repo.used_bytes
    head, regular = await _main_regular_bytes(repo)
    R = Repository
    unchanged = (
        R.main_counted_commit.is_null()
        if counted is None
        else R.main_counted_commit == counted
    )
    with R._meta.database.atomic():
        # Hold the row first, so the LFS sum below sees every change already
        # applied to it, and the ones after it wait for this write
        _hold([repo_id])
        lfs = fn.COALESCE(_stored_lfs(repo_id), 0)
        applied = (
            R.update(
                main_regular_bytes=regular,
                main_counted_commit=head,
                lfs_bytes=lfs,
                used_bytes=lfs + regular,
            )
            .where((R.id == repo_id) & unchanged)
            .execute()
        )
    if not applied:
        # A change was applied meanwhile, on the counters the recount replaces
        enqueue_repository_recount(repo_id, RECOUNT_RETRY)
        return None
    after = R.get_by_id(repo_id).used_bytes
    return {
        "repository": f"{repo.repo_type}:{repo.full_id}",
        "before": before,
        "after": after,
    }


@tasks.task(RECOUNT_REPOSITORY_KIND, timeout=3600, max_attempts=10)
async def recount_repository_task(payload: dict[str, Any]) -> None:
    await recount_repository(payload["repo_id"])


@tasks.task(
    RECOUNT_KIND,
    timeout=24 * 3600,
    max_attempts=5,
    every=(
        timedelta(hours=cfg.app.usage_recount_interval_hours)
        if cfg.app.usage_recount_interval_hours > 0
        else None
    ),
)
async def recount(payload: dict[str, Any], ctx: tasks.TaskContext) -> None:
    """Set every repository's exact usage (or one namespace's), and report the drift.

    Repository by repository, in id order, with the position and the running
    report checkpointed after each one, so a retry or another worker resumes
    where it stopped. Finally the namespaces' summed usage is copied to their
    rows (``write_namespace_snapshots``).
    """
    namespace = payload.get("namespace")
    state = ctx.checkpoint_state or {"after": 0, "stats": {}, "drift": []}
    stats = Counter(state["stats"])
    drift = state["drift"]
    R = Repository
    scope = (R.namespace == namespace) if namespace else (R.id > 0)
    total = R.select().where(scope).count()
    done = R.select().where(scope & (R.id <= state["after"])).count()
    for repo_id, repo_type, full_id in (
        R.select(R.id, R.repo_type, R.full_id)
        .where(scope & (R.id > state["after"]))
        .order_by(R.id)
        .tuples()
    ):
        if ctx.cancel_requested:
            raise tasks.TaskCancelled()
        ctx.stage(f"recounting {repo_type}:{full_id}")
        stats["repositories"] += 1
        try:
            result = await recount_repository(repo_id)
        except Exception as e:  # one repository must not stop the others
            logger.warning(f"Could not recount {repo_type}:{full_id}: {e}")
            enqueue_repository_recount(repo_id, RECOUNT_RETRY)
            stats["failed"] += 1
        else:
            if result is None:
                stats["busy"] += 1  # recounted again on its own, later
            elif result["after"] != result["before"]:
                stats["drifted"] += 1
                stats["drift_bytes"] += abs(result["after"] - result["before"])
                drift = sorted(
                    drift + [result],
                    key=lambda r: abs(r["after"] - r["before"]),
                    reverse=True,
                )[:DRIFT_SHOWN]
        done += 1
        ctx.checkpoint({"after": repo_id, "stats": dict(stats), "drift": drift})
        ctx.progress(done, max(total, done))
    write_namespace_snapshots([namespace] if namespace else None)
    ctx.log(
        "INFO",
        f"Recounted {stats['repositories']} repositories: {stats['drifted']} drifted by "
        f"{stats['drift_bytes']} bytes in all, {stats['busy']} busy, {stats['failed']} failed",
    )


def recount_status() -> dict[str, Any]:
    """The newest site-wide recount and its report, for the admin panel.

    A periodic recount's next occurrence, queued for later, is not it.
    """
    T = BackgroundTask
    due = (T.status != tasks.QUEUED) | (T.run_after <= utcnow())
    task = (
        T.select()
        .where((T.kind == RECOUNT_KIND) & (T.payload == "{}") & due)
        .order_by(T.id.desc())
        .first()
    )
    report = (json.loads(task.checkpoint) if task and task.checkpoint else None) or {}

    def iso(value):
        return (
            value and value.replace(tzinfo=timezone.utc).isoformat()
        )  # stored as naive UTC

    return {
        "interval_hours": cfg.app.usage_recount_interval_hours,
        "task": task
        and {
            "id": task.id,
            "status": task.status,
            "progress_done": task.progress_done,
            "progress_total": task.progress_total,
            "stage": task.progress_stage,
            "stats": report.get("stats", {}),
            "drift": report.get("drift", []),
            "created_at": iso(task.created_at),
            "finished_at": iso(task.finished_at),
        },
    }
